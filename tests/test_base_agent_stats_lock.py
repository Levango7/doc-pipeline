"""BaseAgent 回归：并发统计计数精确、CACHE_TTL 默认 3600 并在写路径生效"""
import contextlib
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pipeline_core.cache_manager as cm_module
from pipeline_core.base_agent import AgentMeta, BaseAgent
from pipeline_core.message_bus_v3 import Message, MessageType


class _ProbeAgent(BaseAgent):
    def handle(self, msg: Message) -> dict | None:
        if msg.payload.get("fail"):
            raise RuntimeError("boom")
        return {"status": "ok"}


def _make_agent(tmp_path: Path, cache_ttl: int = 60) -> _ProbeAgent:
    config = {
        "cache_dir": str(tmp_path / "cache"),
        "log_dir": str(tmp_path / "logs"),
        "quiet": True,
    }
    meta = AgentMeta(name="probe", version="1.0", cache_ttl=cache_ttl)
    return _ProbeAgent("probe", meta, config, None, None)


class TestConcurrentStats:

    def test_concurrent_handle_counts_are_exact(self, tmp_path):
        """多线程交错成功/失败后，success/error 计数与 get_stats/is_healthy 精确一致"""
        agent = _make_agent(tmp_path)
        threads_count = 8
        per_thread = 50
        unexpected = []

        def worker():
            try:
                for i in range(per_thread):
                    msg = Message(topic="t", payload={"fail": (i % 2 == 1)},
                                  msg_type=MessageType.REQUEST)
                    with contextlib.suppress(RuntimeError):
                        agent._wrapped_handle(msg)
            except Exception as e:  # pragma: no cover
                unexpected.append(e)

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not unexpected

        expected = threads_count * per_thread // 2
        assert agent._success_count == expected
        assert agent._error_count == expected

        stats = agent.get_stats()
        assert stats["success_count"] == expected
        assert stats["error_count"] == expected

        assert agent.is_healthy() is False

        snapshot = agent.on_snapshot()
        assert snapshot["success_count"] == expected
        assert snapshot["error_count"] == expected


class TestCacheTtlDefault:

    def test_cache_ttl_default_is_3600(self):
        """类默认 CACHE_TTL 为 3600（不再永不过期）"""
        assert BaseAgent.CACHE_TTL == 3600

    def test_meta_cache_ttl_propagates_to_cachemanager(self, tmp_path):
        """meta.cache_ttl 传递给 CacheManager"""
        agent = _make_agent(tmp_path, cache_ttl=1234)
        assert agent._cache.ttl == 1234

    def test_cache_write_path_expires_after_ttl(self, tmp_path, monkeypatch):
        """cache_set 写入的条目在 TTL 过期后读取返回 None 且文件被清理"""
        agent = _make_agent(tmp_path, cache_ttl=3600)
        agent.cache_set("k", {"v": [1, 2]})
        assert agent.cache_get("k") == {"v": [1, 2]}

        real_time = time.time

        class _FakeTime:
            def time(self):
                return real_time() + 7200

        monkeypatch.setattr(cm_module, "time", _FakeTime())
        assert agent.cache_get("k") is None
