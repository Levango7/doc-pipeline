"""CacheManager 补充测试 — 补齐覆盖率薄弱区（原 50%）。

覆盖范围：
- memory 后端：命中/未命中、LRU 驱逐、TTL 过期、ttl<0 只读禁用、批量接口、异步接口
- file 后端：持久化读写、损坏 JSON、TTL 过期删文件、序列化失败兜底、remove/clear/size
- multi 后端：内存 miss 回填文件层且保留原始 ts（P0 修复验证）
- _purge_expired / 后台清理线程 / shutdown
- 全局注册表 get_cache / clear_all_caches / shutdown_all_caches / all_stats
"""
import asyncio
import json
import time

import pytest

import pipeline_core.cache_manager as cm_mod
from pipeline_core.cache_manager import CacheManager, CacheStats, get_cache

# ─── CacheStats ──────────────────────────────

class TestCacheStats:
    def test_snapshot_zero_division_guard(self):
        s = CacheStats()
        snap = s.snapshot()
        assert snap["hit_rate"] == 0.0

    def test_snapshot_hit_rate(self):
        s = CacheStats()
        s.record_hit()
        s.record_miss()
        s.record_set()
        s.record_eviction()
        snap = s.snapshot()
        assert snap["hits"] == 1 and snap["misses"] == 1
        assert snap["hit_rate"] == pytest.approx(0.5)


# ─── memory 后端 ──────────────────────────────

class TestMemoryBackend:
    def test_set_get_hit_miss(self):
        cm = CacheManager("t_mem_basic", backend="memory")
        assert cm.get("k") is None  # miss
        cm.set("k", {"v": 1})
        assert cm.get("k") == {"v": 1}  # hit
        assert cm.get_stats()["hits"] == 1

    def test_lru_eviction(self):
        cm = CacheManager("t_mem_evict", max_size=2, backend="memory")
        cm.set("a", 1)
        cm.set("b", 2)
        cm.set("c", 3)  # 触发驱逐最旧项 a
        assert cm.get("a") is None
        assert cm.get("b") == 2 and cm.get("c") == 3
        assert cm.get_stats()["evictions"] == 1

    def test_ttl_expiry(self):
        cm = CacheManager("t_mem_ttl", ttl=1, backend="memory")
        cm.set("k", "v")
        cm._cache["k"]["ts"] -= 2  # 回拨时间戳模拟过期
        assert cm.get("k") is None
        assert "k" not in cm._cache  # 过期项被移除

    def test_ttl_expired_emits_metric(self):
        events = []
        cm = CacheManager("t_mem_metric", ttl=1, backend="memory",
                          metrics_callback=lambda e, x: events.append(e))
        cm.set("k", "v")
        cm._cache["k"]["ts"] -= 2
        cm.get("k")
        assert "cache.t_mem_metric.miss_expired" in events

    def test_metrics_callback_exception_suppressed(self):
        def _bad_cb(e, x):
            raise RuntimeError("cb down")
        cm = CacheManager("t_mem_cberr", ttl=1, backend="memory",
                          metrics_callback=_bad_cb)
        cm.set("k", "v")
        cm._cache["k"]["ts"] -= 2
        assert cm.get("k") is None  # 回调异常不影响主流程

    def test_negative_ttl_disables(self):
        cm = CacheManager("t_mem_neg", ttl=-1, backend="memory")
        cm.set("k", "v")  # 写入被忽略
        assert cm.get("k") is None
        assert cm.get_many(["k"]) == {}
        cm.set_many({"k": "v"})
        assert cm.size() == 0

    def test_remove_clear_size(self):
        cm = CacheManager("t_mem_ops", backend="memory")
        cm.set("a", 1)
        cm.set("b", 2)
        assert cm.size() == 2
        cm.remove("a")
        assert cm.get("a") is None and cm.size() == 1
        cm.clear()
        assert cm.size() == 0

    def test_get_many_set_many(self):
        cm = CacheManager("t_mem_batch", backend="memory")
        cm.set_many({"a": 1, "b": 2, "c": 3})
        got = cm.get_many(["a", "b", "missing"])
        assert got == {"a": 1, "b": 2}

    def test_get_stats_fields(self):
        cm = CacheManager("t_mem_stats", max_size=10, ttl=60, backend="memory")
        s = cm.get_stats()
        assert s["name"] == "t_mem_stats"
        assert s["backend"] == "memory"
        assert s["max_size"] == 10 and s["ttl"] == 60
        assert "hit_rate" in s

    def test_async_interfaces(self):
        cm = CacheManager("t_mem_async", backend="memory")

        async def _run():
            await cm.aset("k", "v")
            assert await cm.aget("k") == "v"
            assert await cm.aget("missing") is None
            many = await cm.aget_many(["k", "missing"])
            assert many == {"k": "v"}

        asyncio.run(_run())


# ─── file 后端 ──────────────────────────────

class TestFileBackend:
    def test_set_get_roundtrip(self, tmp_path):
        cm = CacheManager("t_file_basic", backend="file",
                          cache_dir=str(tmp_path))
        cm.set("k", {"data": [1, 2, 3]})
        assert cm.get("k") == {"data": [1, 2, 3]}
        assert cm.size() == 1

    def test_corrupted_json_returns_miss(self, tmp_path):
        cm = CacheManager("t_file_corrupt", backend="file",
                          cache_dir=str(tmp_path))
        fpath = cm._file_path("k")
        fpath.write_text("{not valid json", encoding="utf-8")
        assert cm.get("k") is None
        assert cm.get_stats()["misses"] == 1

    def test_ttl_expiry_deletes_file(self, tmp_path):
        cm = CacheManager("t_file_ttl", ttl=1, backend="file",
                          cache_dir=str(tmp_path))
        cm.set("k", "v")
        fpath = cm._file_path("k")
        entry = json.loads(fpath.read_text(encoding="utf-8"))
        entry["ts"] -= 2
        fpath.write_text(json.dumps(entry), encoding="utf-8")
        assert cm.get("k") is None
        assert not fpath.exists()  # 过期文件被删除

    def test_serialization_failure_suppressed(self, tmp_path):
        cm = CacheManager("t_file_serfail", backend="file",
                          cache_dir=str(tmp_path))
        cm.set("k", {"unserializable": {1, 2, 3}})  # set 不可 JSON 序列化
        assert cm.get("k") is None  # 写入失败，读取 miss

    def test_remove_clear(self, tmp_path):
        cm = CacheManager("t_file_ops", backend="file", cache_dir=str(tmp_path))
        cm.set("a", 1)
        cm.set("b", 2)
        assert cm.size() == 2
        cm.remove("a")
        assert cm.get("a") is None and cm.size() == 1
        cm.clear()
        assert cm.size() == 0

    def test_missing_file_is_miss(self, tmp_path):
        cm = CacheManager("t_file_miss", backend="file", cache_dir=str(tmp_path))
        assert cm.get("ghost") is None
        assert cm.get_stats()["misses"] == 1


class TestFileSizeCache:
    """file 后端 size() 版本+TTL 缓存（2026-08 性能优化 #8）"""

    def _make(self, tmp_path):
        return CacheManager("t_file_sizecache", backend="file",
                            cache_dir=str(tmp_path))

    def test_size_cached_no_repeated_glob(self, tmp_path, monkeypatch):
        import pipeline_core.cache_manager as cm_mod
        cm = self._make(tmp_path)
        cm.set("a", 1)
        glob_calls = []
        real_glob = cm_mod.Path.glob

        def counting_glob(self, pat):
            if str(self) == str(cm._cache_dir):
                glob_calls.append(pat)
            return real_glob(self, pat)
        monkeypatch.setattr(cm_mod.Path, "glob", counting_glob)
        assert cm.size() == 1
        assert cm.size() == 1  # 窗口内第二次调用不再 glob
        assert len(glob_calls) == 1

    def test_size_immediate_after_set_remove_clear(self, tmp_path):
        """本地写/删立即递增版本 → size() 不读旧缓存"""
        cm = self._make(tmp_path)
        cm.set("a", 1)
        cm.set("b", 2)
        assert cm.size() == 2
        cm.remove("a")
        assert cm.size() == 1
        cm.clear()
        assert cm.size() == 0

    def test_size_ttl_expiry_rescans(self, tmp_path):
        cm = self._make(tmp_path)
        cm.set("a", 1)
        assert cm.size() == 1
        # 把缓存窗口拨过期 → 下次 size() 重新 glob
        version, expire, value = cm._size_cache
        cm._size_cache = (version, expire - 10, value)
        (cm._cache_dir / "extra.json").write_text("{}", encoding="utf-8")
        assert cm.size() == 2  # 重扫捕捉到外部新增

    def test_version_bump_skips_stale_cache_even_in_window(self, tmp_path):
        cm = self._make(tmp_path)
        cm.set("a", 1)
        assert cm.size() == 1
        # 窗口内直接 set：版本递增使缓存失效，不返回过期值
        cm.set("b", 2)
        assert cm.size() == 2


# ─── multi 后端 ──────────────────────────────

class TestMultiBackend:
    def test_backfill_preserves_original_ts(self, tmp_path):
        """P0 修复验证：内存 miss 回填文件层时使用文件原始 ts，而非 time.time()"""
        cm = CacheManager("t_multi_ts", ttl=100, backend="multi",
                          cache_dir=str(tmp_path))
        cm.set("k", "v")
        file_ts = cm._cache["k"]["ts"] - 50  # 模拟文件层更早的写入时间
        fpath = cm._file_path("k")
        entry = json.loads(fpath.read_text(encoding="utf-8"))
        entry["ts"] = file_ts
        fpath.write_text(json.dumps(entry), encoding="utf-8")
        cm._cache.clear()  # 清空内存层，强制走文件回填

        assert cm.get("k") == "v"
        assert cm._cache["k"]["ts"] == pytest.approx(file_ts)  # 保留原始 ts

    def test_backfill_eviction_when_memory_full(self, tmp_path):
        cm = CacheManager("t_multi_evict", ttl=100, max_size=1, backend="multi",
                          cache_dir=str(tmp_path))
        cm.set("a", 1)
        cm.set("b", 2)
        cm._cache.clear()
        assert cm.get("a") == 1  # 回填时内存已满 → 驱逐
        assert cm.get_stats()["evictions"] >= 1

    def test_multi_remove_clear_size(self, tmp_path):
        cm = CacheManager("t_multi_ops", backend="multi", cache_dir=str(tmp_path))
        cm.set("a", 1)
        assert cm.size() == 1
        cm.remove("a")
        assert cm.get("a") is None
        cm.set("b", 2)
        cm.clear()
        assert cm.size() == 0 and cm.get("b") is None

    def test_multi_file_missing_returns_none(self, tmp_path):
        cm = CacheManager("t_multi_miss", backend="multi", cache_dir=str(tmp_path))
        assert cm.get("ghost") is None


# ─── _purge_expired / 后台清理 ──────────────────────────────

class TestPurgeAndCleanup:
    def test_purge_no_ttl_returns_zero(self):
        cm = CacheManager("t_purge_nottl", backend="memory")
        cm.set("k", "v")
        assert cm._purge_expired() == 0

    def test_purge_memory_and_file(self, tmp_path):
        cm = CacheManager("t_purge_mix", ttl=1, backend="multi",
                          cache_dir=str(tmp_path))
        cm.set("m", 1)
        cm._cache["m"]["ts"] -= 2
        # 文件层单独写一个过期项
        fpath = cm._file_path("f")
        fpath.write_text(json.dumps({"ts": time.time() - 2, "data": "x"}),
                         encoding="utf-8")
        removed = cm._purge_expired()
        assert removed == 2
        assert cm.size() == 0 and not fpath.exists()

    def test_cleanup_thread_lifecycle(self, tmp_path):
        cm = CacheManager("t_cleanup", ttl=1, backend="memory",
                          cache_dir=str(tmp_path), cleanup_interval=1)
        assert cm._cleanup_thread is not None and cm._cleanup_thread.is_alive()
        cm.set("k", "v")
        cm._cache["k"]["ts"] -= 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and cm.size() > 0:
            time.sleep(0.1)
        assert cm.size() == 0  # 后台线程已清理过期项
        cm.shutdown()
        assert not cm._cleanup_thread.is_alive()

    def test_shutdown_without_thread_is_noop(self):
        cm = CacheManager("t_shutdown_noop", backend="memory")
        cm.shutdown()  # 无清理线程，不应抛异常


# ─── 全局注册表 ──────────────────────────────

class TestRegistry:
    def test_get_cache_singleton(self, monkeypatch):
        monkeypatch.setattr(cm_mod, "_registry", {})
        c1 = get_cache("t_reg_single")
        c2 = get_cache("t_reg_single")
        assert c1 is c2

    def test_clear_all_and_stats(self, monkeypatch):
        monkeypatch.setattr(cm_mod, "_registry", {})
        cm = get_cache("t_reg_clear")
        cm.set("k", "v")
        cm_mod.clear_all_caches()
        assert cm.size() == 0
        stats = cm_mod.all_stats()
        assert "t_reg_clear" in stats

    def test_shutdown_all(self, monkeypatch):
        monkeypatch.setattr(cm_mod, "_registry", {})
        get_cache("t_reg_shutdown")
        cm_mod.shutdown_all_caches()  # 不应抛异常
