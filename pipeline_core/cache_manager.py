"""
统一缓存管理层 — 消除项目中 5 处分散缓存，提供单一接口。

支持三种后端：
  - memory:  进程内 LRU + TTL（默认，零依赖）
  - file:    磁盘 JSON 持久化 + TTL（跨进程共享）
  - multi:   memory → file 两级缓存（先查内存，miss 后查文件并回填内存）

增强特性：
  - 批量操作 get_many / set_many（减少锁竞争）
  - 定期清理过期项（后台守护线程，可配置间隔）
  - 异步接口 aget / aset / aget_many（兼容 async I/O 链路）
  - 线程安全，可选指标上报
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CacheStats:
    """缓存命中/未命中统计（线程安全）"""
    __slots__ = ("hits", "misses", "sets", "evictions", "_lock")

    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.evictions = 0
        self._lock = threading.Lock()

    def record_hit(self):
        with self._lock:
            self.hits += 1

    def record_miss(self):
        with self._lock:
            self.misses += 1

    def record_set(self):
        with self._lock:
            self.sets += 1

    def record_eviction(self):
        with self._lock:
            self.evictions += 1

    def snapshot(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "sets": self.sets,
                "evictions": self.evictions,
                "hit_rate": self.hits / total if total > 0 else 0.0,
            }


class CacheManager:
    """
    统一缓存管理器。

    用法:
        cm = CacheManager("search", max_size=1000, ttl=86400)
        cm.set("key", data)
        val = cm.get("key")

        # 文件持久化
        cm = CacheManager("persistent", backend="file", cache_dir="cache/persistent", ttl=3600)
    """

    def __init__(
        self,
        name: str = "default",
        max_size: int = 1000,
        ttl: int = 0,
        backend: str = "memory",       # "memory" | "file" | "multi"
        cache_dir: str | None = None,
        metrics_callback: Callable[[str, dict], None] | None = None,
        cleanup_interval: int = 0,     # >0 时启动后台定期清理（秒）
    ):
        self.name = name
        self.max_size = max_size
        self.ttl = ttl
        self.backend = backend
        self._metrics_cb = metrics_callback
        self.stats = CacheStats()

        # multi 后端: memory + file 两级
        if backend in ("file", "multi"):
            self._cache_dir = Path(cache_dir or "cache") / name
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._cache_dir = None  # type: ignore[assignment]

        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

        # 后台定期清理
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_stop = threading.Event()
        if cleanup_interval > 0 and self.ttl > 0:
            self._start_cleanup(cleanup_interval)

    def _start_cleanup(self, interval: int):
        """启动后台守护线程定期清理过期项。"""
        def _cleanup_loop():
            while not self._cleanup_stop.wait(interval):
                try:
                    removed = self._purge_expired()
                    if removed > 0:
                        logger.debug("缓存 [%s] 清理 %d 个过期项", self.name, removed)
                except Exception as e:
                    logger.debug("缓存清理异常 [%s]: %s", self.name, e)

        self._cleanup_thread = threading.Thread(
            target=_cleanup_loop, name=f"cache-cleanup-{self.name}", daemon=True)
        self._cleanup_thread.start()

    def _purge_expired(self) -> int:
        """清理所有过期项，返回清理数量。"""
        if self.ttl <= 0:
            return 0
        now = time.time()
        removed = 0
        if self.backend in ("memory", "multi"):
            with self._lock:
                expired_keys = [k for k, v in self._cache.items()
                                if now - v["ts"] > self.ttl]
                for k in expired_keys:
                    self._cache.pop(k, None)
                    removed += 1
        if self.backend in ("file", "multi") and self._cache_dir:
            for f in self._cache_dir.glob("*.json"):
                try:
                    with open(f, encoding="utf-8") as fh:
                        entry = json.load(fh)
                    if now - entry.get("ts", 0) > self.ttl:
                        os.remove(f)
                        removed += 1
                except (json.JSONDecodeError, OSError):
                    pass
        return removed

    # ── 核心接口 ──────────────────────────────

    def get(self, key: str) -> Any | None:
        """读取缓存。命中返回数据，未命中/过期返回 None。"""
        if self.ttl < 0:
            return None

        # multi: 先查内存，miss 后查文件并回填
        if self.backend == "multi":
            val = self._memory_get(key)
            if val is not None:
                return val
            # P0 修复: 回填内存时必须使用文件原始 ts，而非 time.time()
            # （原先用 time.time() 会重置 TTL 起始时间，导致过期项被回填后"续命"，
            #   内存与文件层 TTL 不一致）
            val, file_ts = self._file_get_entry(key)
            if val is not None:
                # 回填内存层
                with self._lock:
                    while len(self._cache) >= self.max_size:
                        self._cache.popitem(last=False)
                        self.stats.record_eviction()
                    self._cache[key] = {"data": val, "ts": file_ts}
            return val

        if self.backend == "file":
            return self._file_get(key)

        return self._memory_get(key)

    def _memory_get(self, key: str) -> Any | None:
        """内存层读取。"""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.stats.record_miss()
                return None
            if self.ttl > 0 and time.time() - entry["ts"] > self.ttl:
                self._cache.pop(key, None)
                self.stats.record_miss()
                self._emit("miss_expired", {"key": key})
                return None
            self._cache.move_to_end(key)
            self.stats.record_hit()
            return entry["data"]

    def set(self, key: str, data: Any):
        """写入缓存。"""
        if self.ttl < 0:
            return

        if self.backend in ("file", "multi"):
            self._file_set(key, data)  # _file_set 内部会 record_set

        if self.backend in ("memory", "multi"):
            with self._lock:
                while len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)
                    self.stats.record_eviction()
                self._cache[key] = {"data": data, "ts": time.time()}
                # P1 修复: 仅 memory-only 后端在此记录 set
                # file 后端已在 _file_set 中记录；multi 后端的 set 也已在 _file_set 中记录，不重复
                if self.backend == "memory":
                    self.stats.record_set()

    def remove(self, key: str):
        """删除单个缓存项。"""
        if self.backend in ("file", "multi"):
            fpath = self._file_path(key)
            try:
                if fpath.exists():
                    os.remove(fpath)
            except OSError:
                pass
        if self.backend in ("memory", "multi"):
            with self._lock:
                self._cache.pop(key, None)

    def clear(self):
        """清空全部缓存。"""
        if self.backend in ("file", "multi") and self._cache_dir:
            for f in self._cache_dir.glob("*.json"):
                with contextlib.suppress(OSError):
                    os.remove(f)
        if self.backend in ("memory", "multi"):
            with self._lock:
                self._cache.clear()

    def size(self) -> int:
        """当前缓存条目数。"""
        if self.backend == "multi":
            with self._lock:
                return len(self._cache)
        if self.backend == "file":
            if self._cache_dir:
                return sum(1 for _ in self._cache_dir.glob("*.json"))
            return 0
        with self._lock:
            return len(self._cache)

    # ── 批量操作 ──────────────────────────────

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        """批量读取 —— 逐项 get，返回 {key: value}（仅含命中的 key）。

        注: 当前实现逐项加锁，并非单次锁获取。如需减少锁竞争可改为
        在单次锁内遍历内存层（file 后端仍需逐项 I/O）。
        """
        result: dict[str, Any] = {}
        if self.ttl < 0:
            return result
        for key in keys:
            val = self.get(key)
            if val is not None:
                result[key] = val
        return result

    def set_many(self, items: dict[str, Any]):
        """批量写入 —— 逐项 set，每项各记一次 set 统计。"""
        if self.ttl < 0:
            return
        for key, data in items.items():
            self.set(key, data)

    # ── 异步接口 ──────────────────────────────

    async def aget(self, key: str) -> Any | None:
        """异步读取 —— 在 async 上下文中用 run_in_executor 避免阻塞事件循环。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get, key)

    async def aset(self, key: str, data: Any):
        """异步写入。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.set, key, data)

    async def aget_many(self, keys: list[str]) -> dict[str, Any]:
        """异步批量读取 —— 并发获取所有 key。"""
        tasks = [self.aget(k) for k in keys]
        values = await asyncio.gather(*tasks)
        return {k: v for k, v in zip(keys, values, strict=False) if v is not None}

    # ── 生命周期 ──────────────────────────────

    def shutdown(self):
        """停止后台清理线程。"""
        self._cleanup_stop.set()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2.0)

    # ── 文件后端 ──────────────────────────────

    def _file_path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self._cache_dir / f"{h}.json"

    def _file_get(self, key: str) -> Any | None:
        """文件层读取（仅返回 data，兼容旧调用方）。"""
        data, _ = self._file_get_entry(key)
        return data

    def _file_get_entry(self, key: str) -> tuple[Any | None, float]:
        """文件层读取，返回 (data, ts)。

        未命中/过期/出错返回 (None, 0.0)。
        ts 为写入时的时间戳，用于 multi 后端回填内存时保持 TTL 一致性（P0 修复）。
        """
        fpath = self._file_path(key)
        if not fpath.exists():
            self.stats.record_miss()
            return None, 0.0
        try:
            with open(fpath, encoding="utf-8") as f:
                entry = json.load(f)
            ts = entry.get("ts", 0.0)
            if self.ttl > 0 and time.time() - ts > self.ttl:
                with contextlib.suppress(OSError):
                    os.remove(fpath)
                self.stats.record_miss()
                return None, 0.0
            self.stats.record_hit()
            return entry.get("data"), ts
        except (json.JSONDecodeError, OSError):
            self.stats.record_miss()
            return None, 0.0

    def _file_set(self, key: str, data: Any):
        fpath = self._file_path(key)
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"key": key, "ts": time.time(), "data": data},
                          f, ensure_ascii=False)
            self.stats.record_set()
        except (OSError, TypeError) as e:
            logger.debug("缓存序列化失败 [%s]: %s", key, e)

    # ── 指标 ──────────────────────────────────

    def _emit(self, event: str, extra: dict):
        if self._metrics_cb:
            try:
                self._metrics_cb(f"cache.{self.name}.{event}", extra)
            except Exception as e:
                logger.debug("缓存指标回调失败 [%s.%s]: %s", self.name, event, e)

    def get_stats(self) -> dict:
        """返回缓存统计快照。"""
        s = self.stats.snapshot()
        s["name"] = self.name
        s["backend"] = self.backend
        s["size"] = self.size()
        s["max_size"] = self.max_size
        s["ttl"] = self.ttl
        return s


# ── 全局注册表 ──────────────────────────────

_registry: dict[str, CacheManager] = {}
_registry_lock = threading.Lock()


def get_cache(name: str, **kwargs) -> CacheManager:
    """获取或创建命名缓存实例（单例）。"""
    with _registry_lock:
        if name not in _registry:
            _registry[name] = CacheManager(name=name, **kwargs)
        return _registry[name]


def clear_all_caches():
    """清空所有已注册缓存。"""
    with _registry_lock:
        for cm in _registry.values():
            cm.clear()


def shutdown_all_caches():
    """停止所有缓存的后台清理线程。"""
    with _registry_lock:
        for cm in _registry.values():
            cm.shutdown()


def all_stats() -> dict[str, dict]:
    """所有缓存的统计快照。"""
    with _registry_lock:
        return {name: cm.get_stats() for name, cm in _registry.items()}
