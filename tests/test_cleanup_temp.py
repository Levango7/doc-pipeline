"""P0-2 临时文件清理测试：动态遍历所有已注册 agent（不再硬编码 fetcher）。"""

from pipeline_core.pipeline import PipelineOrchestrator, PipelineTask


class _RecordingAgent:
    def __init__(self):
        self.task_calls = []
        self.stale_calls = []

    def cleanup_task_temp(self, task_id):
        self.task_calls.append(task_id)
        return 3

    def cleanup_stale_temp(self, max_age_hours=24):
        self.stale_calls.append(max_age_hours)
        return 2


class _PlainAgent:
    """未实现清理契约 —— 应被 hasattr 防御跳过"""


class _BrokenAgent:
    def cleanup_task_temp(self, task_id):
        raise RuntimeError("disk on fire")

    def cleanup_stale_temp(self, max_age_hours=24):
        raise RuntimeError("disk on fire")


class _FakeRegistry:
    def __init__(self, agents):
        self._agents = agents

    def list_agent_names(self):
        return list(self._agents)

    def get_instance(self, name):
        return self._agents.get(name)


def _make_skeleton(registry):
    """构造最小 orchestrator 骨架：_cleanup_* 只依赖 registry 与 _log。"""
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    orch.registry = registry
    orch._log = lambda level, msg, **kw: None
    return orch


def _make_task():
    return PipelineTask(id="t9", pipeline_name="docgen", input_file="in.md", config={})


class TestDynamicCleanup:
    def test_cleanup_task_temp_visits_all_registered_agents(self):
        a, b = _RecordingAgent(), _RecordingAgent()
        orch = _make_skeleton(_FakeRegistry({"a": a, "b": b, "plain": _PlainAgent()}))

        orch._cleanup_task_temp(_make_task())

        assert a.task_calls == ["t9"]
        assert b.task_calls == ["t9"]

    def test_cleanup_all_stale_visits_all_registered_agents(self):
        a, b = _RecordingAgent(), _RecordingAgent()
        orch = _make_skeleton(_FakeRegistry({"a": a, "b": b, "plain": _PlainAgent()}))

        orch._cleanup_all_stale_temp(max_age_hours=12)

        assert a.stale_calls == [12]
        assert b.stale_calls == [12]

    def test_cleanup_continues_after_agent_error(self):
        good = _RecordingAgent()
        orch = _make_skeleton(_FakeRegistry({"bad": _BrokenAgent(), "good": good}))

        orch._cleanup_task_temp(_make_task())  # 不应抛异常
        orch._cleanup_all_stale_temp(max_age_hours=24)

        assert good.task_calls == ["t9"]
        assert good.stale_calls == [24]


class TestRealFetcherShutdown:
    def test_shutdown_triggers_stale_cleanup_on_fetcher(self, orch):
        inst = orch.registry.get_instance("fetcher")
        assert inst is not None
        calls = []
        original = inst.cleanup_stale_temp

        def spy(max_age_hours=24):
            calls.append(max_age_hours)
            return original(max_age_hours)

        inst.cleanup_stale_temp = spy
        try:
            orch.shutdown()
        finally:
            inst.cleanup_stale_temp = original

        assert calls == [24]
