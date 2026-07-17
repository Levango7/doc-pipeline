"""测试 BaseAgent.on_stop() 生命周期钩子 —— aiohttp session 生命周期管理"""
import asyncio
import pytest
from pathlib import Path

from pipeline_core.registry import Registry, AgentMeta, AgentStatus
from pipeline_core.base_agent import BaseAgent, Message


class _MockAgent(BaseAgent):
    """Mock agent 用于测试 on_stop 生命周期"""
    on_stop_called = False

    def handle(self, msg: Message) -> dict | None:
        return {"status": "ok"}

    def on_stop(self):
        super().on_stop()
        self.on_stop_called = True


class TestOnStopLifecycle:
    """Registry.shutdown() 调用 on_stop() 测试"""

    def test_shutdown_calls_on_stop(self, tmp_path):
        """Registry.shutdown() 应调用所有 agent 的 on_stop()"""
        reg = Registry(enable_health_check=False)

        meta = AgentMeta(name="mock", version="1.0", description="test")
        agent = _MockAgent(name="mock", meta=meta, config={"cache_dir": str(tmp_path)})
        reg.register(meta, instance=agent)

        assert not agent.on_stop_called
        reg.shutdown()
        assert agent.on_stop_called, "on_stop() should be called during shutdown()"

    def test_unregister_calls_on_stop(self, tmp_path):
        """Registry.unregister() 应调用 agent 的 on_stop()"""
        reg = Registry(enable_health_check=False)

        meta = AgentMeta(name="mock", version="1.0", description="test")
        agent = _MockAgent(name="mock", meta=meta, config={"cache_dir": str(tmp_path)})
        reg.register(meta, instance=agent)

        assert not agent.on_stop_called
        reg.unregister("mock")
        assert agent.on_stop_called, "on_stop() should be called during unregister()"

    def test_shutdown_with_no_agents(self):
        """Registry.shutdown() 无 agent 时不报错"""
        reg = Registry(enable_health_check=False)
        reg.shutdown()  # should not raise

    def test_shutdown_on_stop_exception_does_not_propagate(self, tmp_path):
        """on_stop() 抛异常不应中断其他 agent 的清理"""
        reg = Registry(enable_health_check=False)

        class _BadAgent(BaseAgent):
            def handle(self, msg):
                return {}

            def on_stop(self):
                raise RuntimeError("bad agent")

        class _GoodAgent(BaseAgent):
            good_stopped = False

            def handle(self, msg):
                return {}

            def on_stop(self):
                super().on_stop()
                self.good_stopped = True

        meta1 = AgentMeta(name="bad", version="1.0")
        meta2 = AgentMeta(name="good", version="1.0")
        bad = _BadAgent(name="bad", meta=meta1, config={"cache_dir": str(tmp_path)})
        good = _GoodAgent(name="good", meta=meta2, config={"cache_dir": str(tmp_path)})
        reg.register(meta1, instance=bad)
        reg.register(meta2, instance=good)

        reg.shutdown()  # should not raise despite bad agent
        assert good.good_stopped, "good agent on_stop() should still be called"


class TestFetcherOnStop:
    """Fetcher.on_stop() 关闭 aiohttp session 测试"""

    def test_fetcher_on_stop_exists(self):
        """FetcherAgent 有 on_stop 方法"""
        from agents.fetcher import FetcherAgent
        assert hasattr(FetcherAgent, "on_stop")

    def test_fetcher_on_stop_no_session(self, tmp_path):
        """无 session 时 on_stop 不报错"""
        from agents.fetcher import FetcherAgent, AGENT_NAME, AGENT_VERSION, AGENT_DESC, AGENT_PRIORITY
        from pipeline_core.base_agent import AgentMeta

        meta = AgentMeta(name="fetcher", version=AGENT_VERSION, description=AGENT_DESC,
                         priority=AGENT_PRIORITY)
        agent = FetcherAgent(
            name="fetcher", meta=meta,
            config={"cache_dir": str(tmp_path), "temp_dir": str(tmp_path)},
            message_bus=None, registry=None,
        )
        assert agent._aio_session is None
        agent.on_stop()  # should not raise
        assert agent._aio_session is None
