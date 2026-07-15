"""
统一缓存管理层 — 消除项目中 5 处分散缓存，提供单一接口。

支持两种后端：
  - memory:  进程内 LRU + TTL（默认，零依赖）
  - file:    磁盘 JSON 持久化 + TTL（跨进程共享）

线程安全，可选指标上报。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, Callable


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
        backend: str = "memory",       # "memory" | "file"
        cache_dir: Optional[str] = None,
        metrics_callback: Optional[Callable[[str, dict], None]] = None,
    ):
        self.name = name
        self.max_size = max_size
        self.ttl = ttl
        self.backend = backend
        self._metrics_cb = metrics_callback
        self.stats = CacheStats()

        if backend == "file":
            self._cache_dir = Path(cache_dir or "cache") / name
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            self._cache_dir = None

        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    # ── 核心接口 ──────────────────────────────

    def get(self, key: str) -> Any | None:
        """读取缓存。命中返回数据，未命中/过期返回 None。"""
        if self.ttl < 0:
            return None

        if self.backend == "file":
            return self._file_get(key)

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

        if self.backend == "file":
            self._file_set(key, data)
            return

        with self._lock:
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
                self.stats.record_eviction()
            self._cache[key] = {"data": data, "ts": time.time()}
            self.stats.record_set()

    def remove(self, key: str):
        """删除单个缓存项。"""
        if self.backend == "file":
            fpath = self._file_path(key)
            try:
                if fpath.exists():
                    os.remove(fpath)
            except OSError:
                pass
            return
        with self._lock:
            self._cache.pop(key, None)

    def clear(self):
        """清空全部缓存。"""
        if self.backend == "file":
            if self._cache_dir:
                for f in self._cache_dir.glob("*.json"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            return
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        """当前缓存条目数。"""
        if self.backend == "file":
            if self._cache_dir:
                return sum(1 for _ in self._cache_dir.glob("*.json"))
            return 0
        with self._lock:
            return len(self._cache)

    # ── 文件后端 ──────────────────────────────

    def _file_path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self._cache_dir / f"{h}.json"

    def _file_get(self, key: str) -> Any | None:
        fpath = self._file_path(key)
        if not fpath.exists():
            self.stats.record_miss()
            return None
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                entry = json.load(f)
            if self.ttl > 0 and time.time() - entry.get("ts", 0) > self.ttl:
                try:
                    os.remove(fpath)
                except OSError:
                    pass
                self.stats.record_miss()
                return None
            self.stats.record_hit()
            return entry.get("data")
        except (json.JSONDecodeError, OSError):
            self.stats.record_miss()
            return None

    def _file_set(self, key: str, data: Any):
        fpath = self._file_path(key)
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"key": key, "ts": time.time(), "data": data},
                          f, ensure_ascii=False)
            self.stats.record_set()
        except (OSError, TypeError):
            pass  # 非可序列化数据静默跳过

    # ── 指标 ──────────────────────────────────

    def _emit(self, event: str, extra: dict):
        if self._metrics_cb:
            try:
                self._metrics_cb(f"cache.{self.name}.{event}", extra)
            except Exception:
                pass

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


def all_stats() -> dict[str, dict]:
    """所有缓存的统计快照。"""
    with _registry_lock:
        return {name: cm.get_stats() for name, cm in _registry.items()}
