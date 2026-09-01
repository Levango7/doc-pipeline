"""tests/test_dag_executor_build.py — DAG 构建 + 查询词提取 + 熔断器（纯逻辑）。"""
from unittest.mock import MagicMock

import pytest

from pipeline_core.dag_executor import DAGExecutor


def _make_executor():
    """构造最小 DAGExecutor（无需真实 registry/bus）。"""
    return DAGExecutor(
        registry=MagicMock(),
        bus=MagicMock(),
        cb_registry=MagicMock(),
        rate_limiters=MagicMock(),
        metrics=MagicMock(),
    )


class TestBuildDag:
    def test_single_node(self):
        ex = _make_executor()
        ex.registry.get_meta.return_value = MagicMock(dependencies=[])
        nodes, levels = ex.build_dag(["researcher"])
        assert "researcher" in nodes
        assert levels == [["researcher"]]

    def test_linear_chain(self):
        ex = _make_executor()
        # writer 依赖 researcher，researcher 无依赖
        def _meta(name):
            m = MagicMock()
            m.dependencies = [] if name == "researcher" else ["researcher"]
            return m
        ex.registry.get_meta.side_effect = _meta
        nodes, levels = ex.build_dag(["writer", "researcher"])
        assert levels == [["researcher"], ["writer"]]

    def test_parallel_nodes(self):
        ex = _make_executor()
        ex.registry.get_meta.return_value = MagicMock(dependencies=[])
        nodes, levels = ex.build_dag(["a", "b", "c"])
        # 所有节点无依赖 → 同一层
        assert len(levels) == 1
        assert set(levels[0]) == {"a", "b", "c"}

    def test_detects_cycle(self):
        ex = _make_executor()
        # a 依赖 b，b 依赖 a
        def _meta(name):
            m = MagicMock()
            m.dependencies = ["b"] if name == "a" else ["a"]
            return m
        ex.registry.get_meta.side_effect = _meta
        with pytest.raises(ValueError, match="环"):
            ex.build_dag(["a", "b"])

    def test_missing_meta_skipped(self):
        ex = _make_executor()
        ex.registry.get_meta.return_value = None
        nodes, levels = ex.build_dag(["ghost"])
        assert nodes == {}
        assert levels == []


class TestExtractQueries:
    def test_extracts_normal_lines(self, tmp_path):
        f = tmp_path / "input.md"
        f.write_text("# 标题\n\nPython 异步编程\n\nRAG 架构设计\n", encoding="utf-8")
        ex = _make_executor()
        node = MagicMock()
        node.agent_config.config.get.return_value = None
        queries = ex._extract_queries(str(f), node)
        assert "Python 异步编程" in queries
        assert "RAG 架构设计" in queries

    def test_filters_noise_lines(self, tmp_path):
        f = tmp_path / "input.md"
        f.write_text("这是一个测试，用于验证流水线是否正常工作\n\n真实主题\n", encoding="utf-8")
        ex = _make_executor()
        node = MagicMock()
        node.agent_config.config.get.return_value = None
        queries = ex._extract_queries(str(f), node)
        assert "真实主题" in queries
        # 噪音行被过滤
        assert not any("验证" in q for q in queries)

    def test_all_noise_fallback_to_candidates(self, tmp_path):
        f = tmp_path / "input.md"
        f.write_text("这是一个测试\n用于验证流水线\n", encoding="utf-8")
        ex = _make_executor()
        node = MagicMock()
        node.agent_config.config.get.return_value = None
        queries = ex._extract_queries(str(f), node)
        # 所有行都是噪音 → fallback 到 candidates
        assert len(queries) == 2

    def test_empty_file_raises(self, tmp_path):
        f = tmp_path / "input.md"
        f.write_text("# 只有注释\n", encoding="utf-8")
        ex = _make_executor()
        node = MagicMock()
        node.agent_config.config.get.return_value = None
        with pytest.raises(ValueError, match="未提取到任何有效检索词"):
            ex._extract_queries(str(f), node)

    def test_caches_result(self, tmp_path):
        f = tmp_path / "input.md"
        f.write_text("主题A 足够长的内容\n", encoding="utf-8")
        ex = _make_executor()
        node = MagicMock()
        node.agent_config.config.get.return_value = None
        q1 = ex._extract_queries(str(f), node)
        q2 = ex._extract_queries(str(f), node)
        assert q1 == q2


class TestCircuitBreaker:
    def test_returns_false_when_disabled(self):
        ex = _make_executor()
        node = MagicMock()
        node.agent_name = "writer"
        node.agent_config.circuit_breaker = {"enabled": False}
        assert ex._circuit_breaker(node, MagicMock()) is False

    def test_returns_true_when_open(self):
        ex = _make_executor()
        breaker = MagicMock()
        breaker.record_failure.return_value = None
        breaker.allow_request.return_value = False  # 熔断器开
        ex._cb_registry.get_or_create.return_value = breaker
        node = MagicMock()
        node.agent_name = "writer"
        node.agent_config.circuit_breaker = {"enabled": True, "failure_threshold": 1}
        assert ex._circuit_breaker(node, MagicMock()) is True

    def test_returns_false_when_closed(self):
        ex = _make_executor()
        breaker = MagicMock()
        breaker.record_failure.return_value = None
        breaker.allow_request.return_value = True  # 熔断器关
        ex._cb_registry.get_or_create.return_value = breaker
        node = MagicMock()
        node.agent_name = "writer"
        node.agent_config.circuit_breaker = {"enabled": True, "failure_threshold": 5}
        assert ex._circuit_breaker(node, MagicMock()) is False
