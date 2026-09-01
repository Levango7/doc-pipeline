"""tests/test_registry.py — Agent 注册/发现/拓扑排序/respawn/健康检查。"""

import pytest

from pipeline_core.registry import AgentMeta, AgentStatus, Registry


class _FakeAgent:
    def __init__(self, name):
        self.name = name
        self.status = AgentStatus.LOADED


class TestRegistry:
    def test_register_and_get_meta(self):
        reg = Registry()
        meta = AgentMeta(name="writer", version="2.0", priority=30)
        agent = _FakeAgent("writer")
        reg.register(meta, agent)
        # Registry.get 返回 meta dict
        info = reg.get("writer")
        assert info is not None
        assert info["name"] == "writer"
        assert reg.get_meta("writer").name == "writer"

    def test_list_agent_names(self):
        reg = Registry()
        reg.register(AgentMeta(name="a"), _FakeAgent("a"))
        reg.register(AgentMeta(name="b"), _FakeAgent("b"))
        assert sorted(reg.list_agent_names()) == ["a", "b"]

    def test_deps_order_topological(self):
        reg = Registry()
        reg.register(AgentMeta(name="writer", dependencies=["researcher"]), _FakeAgent("writer"))
        reg.register(AgentMeta(name="researcher"), _FakeAgent("researcher"))
        order = reg.deps_order()
        assert order.index("researcher") < order.index("writer")

    def test_deps_order_detects_cycle(self):
        reg = Registry()
        reg.register(AgentMeta(name="a", dependencies=["b"]), _FakeAgent("a"))
        reg.register(AgentMeta(name="b", dependencies=["a"]), _FakeAgent("b"))
        with pytest.raises(ValueError, match="循环依赖"):
            reg.deps_order()

    def test_set_status_and_get(self):
        reg = Registry()
        reg.register(AgentMeta(name="x"), _FakeAgent("x"))
        reg.set_status("x", AgentStatus.RUNNING)
        assert reg.get_status("x") == AgentStatus.RUNNING

    def test_find_by_role(self):
        reg = Registry()
        reg.register(AgentMeta(name="writer", input_topics=["researcher.done"]),
                      _FakeAgent("writer"))
        reg.register(AgentMeta(name="researcher", output_topics=["researcher.done"]),
                      _FakeAgent("researcher"))
        found = reg.find("researcher.done")
        names = [a["name"] for a in found]
        assert "writer" in names
        assert "researcher" in names

    def test_get_dependency_graph(self):
        reg = Registry()
        reg.register(AgentMeta(name="writer", dependencies=["researcher"]),
                      _FakeAgent("writer"))
        reg.register(AgentMeta(name="researcher"), _FakeAgent("researcher"))
        graph = reg.get_dependency_graph()
        assert len(graph["nodes"]) == 2
        assert any(e == {"from": "researcher", "to": "writer"} for e in graph["edges"])

    def test_unregister(self):
        reg = Registry()
        reg.register(AgentMeta(name="temp"), _FakeAgent("temp"))
        reg.unregister("temp")
        assert reg.get("temp") is None

    def test_stats_tracking(self):
        reg = Registry()
        reg.register(AgentMeta(name="writer"), _FakeAgent("writer"))
        reg.set_status("writer", AgentStatus.RUNNING)
        reg.record_processing_time("writer", 100.0)
        agents = reg.list()
        writer_info = next(a for a in agents if a["name"] == "writer")
        assert "stats" in writer_info
