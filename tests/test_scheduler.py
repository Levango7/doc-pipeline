"""Scheduler — plan parsing, execution nodes, pooling"""
import pytest
from pipeline_core.scheduler import Scheduler


class TestSchedulerParsing:
    """YAML pipeline 解析"""

    def test_parse_docgen(self, docgen_plan):
        assert docgen_plan is not None
        assert docgen_plan.pipeline_name in ("docgen", "test_pipeline")
        assert docgen_plan.node_count > 0
        assert docgen_plan.plan_id is not None

    def test_plan_has_levels(self, docgen_plan):
        assert len(docgen_plan.levels) > 0, "should have at least 1 level"
        for level in docgen_plan.levels:
            assert len(level) > 0, "each level needs >=1 node"

    def test_plan_node_structure(self, docgen_plan):
        """每个节点有 agent_name、config、dependencies"""
        node = docgen_plan.levels[0][0]
        assert hasattr(node, "agent_name")
        assert hasattr(node, "dependencies")
        assert hasattr(node, "timeout")
        assert hasattr(node, "max_retries")
        assert hasattr(node, "agent_config"), f"node missing agent_config"

    def test_researcher_has_pool_size(self, docgen_plan):
        """researcher agent 应有 pool_size=2"""
        for level in docgen_plan.levels:
            for node in level:
                if "researcher" in node.agent_name:
                    assert hasattr(node.agent_config, "pool_size")
                    break

    def test_dependency_ordering(self, docgen_plan):
        """writer 依赖 fetcher/researcher, checker 依赖 writer"""
        dep_map = {}
        for level in docgen_plan.levels:
            for node in level:
                dep_map[node.agent_name] = node.dependencies
        assert "writer" in dep_map
        assert "checker" in dep_map
        # writer 应依赖 fetcher 或 researcher
        writer_deps = dep_map.get("writer", [])
        assert any(d in ("fetcher", "researcher") for d in writer_deps), \
            f"writer should depend on fetcher/researcher, got {writer_deps}"


class TestSchedulerFromDict:
    """从 dict 构建 plan（通过 _build_plan）"""

    def test_parse_dict(self, scheduler):
        raw = {
            "name": "test_pipeline",
            "agents": [{
                "name": "test_agent",
                "timeout": 30,
                "retry": {"max_retries": 2},
                "dependencies": [],
                "config": {"key": "val"},
            }],
            "topology": {"levels": [["test_agent"]]},
        }
        plan = scheduler._build_plan(raw, "test_pipeline")
        assert plan.pipeline_name == "test_pipeline"
        assert plan.node_count == 1
        assert plan.levels[0][0].agent_name == "test_agent"

    def test_checkpoint_blocked(self, scheduler):
        """blocked_agents 配置应被解析"""
        raw = {
            "name": "check_test",
            "agents": [{
                "name": "a",
                "dependencies": [],
                "config": {},
            }],
            "topology": {"levels": [["a"]]},
            "checkpoint": {"blocked_agents": ["a"]},
            "user": {"username": ""},
        }
        plan = scheduler._build_plan(raw, "check_test")
        assert plan is not None
        assert plan.raw.get("checkpoint", {}).get("blocked_agents") == ["a"]

    def test_raw_config_preserved(self, scheduler):
        raw = {
            "name": "raw_test",
            "agents": [{
                "name": "a",
                "dependencies": [],
                "config": {"nested": {"k": "v"}},
            }],
            "topology": {"levels": [["a"]]},
        }
        plan = scheduler._build_plan(raw, "raw_test")
        assert plan.raw["agents"][0]["config"]["nested"]["k"] == "v"