"""tests/test_dag_executor_run.py — DAG 节点执行/层级调度/重做循环。"""
from unittest.mock import MagicMock

from pipeline_core.dag_executor import DAGExecutor


def _make_executor():
    return DAGExecutor(
        registry=MagicMock(),
        bus=MagicMock(),
        cb_registry=MagicMock(),
        rate_limiters=MagicMock(),
        metrics=MagicMock(),
    )


def _make_node(name="writer", deps=None):
    """构造完整 mock node。"""
    node = MagicMock()
    node.agent_name = name
    node.dependencies = deps or []
    node.agent_config.pool_size = 1
    node.agent_config.circuit_breaker = None
    node.agent_config.rate_limit = {}
    node.agent_config.config = {}
    node.timeout = 300
    node.max_retries = 3
    node.initial_delay = 1.0
    node.backoff = "exponential"
    return node


class TestExecuteNode:
    def test_missing_agent_returns_error(self):
        ex = _make_executor()
        ex.registry.get_instance.return_value = None
        node = _make_node("ghost")
        task = MagicMock()
        task.id = "t1"
        task.stop_event.is_set.return_value = False
        result = ex.execute_node_from_scheduler(task, node, "in.md", MagicMock())
        assert "error" in result

    def test_circuit_open_returns_blocked(self):
        ex = _make_executor()
        breaker = MagicMock()
        breaker.allow_request.return_value = False
        ex._cb_registry.get_or_create.return_value = breaker
        ex.registry.get_instance.return_value = MagicMock()
        node = _make_node("writer")
        node.agent_config.circuit_breaker = {"enabled": True, "failure_threshold": 1}
        task = MagicMock()
        task.id = "t1"
        task.stop_event.is_set.return_value = False
        result = ex.execute_node_from_scheduler(task, node, "in.md", MagicMock())
        assert result.get("status") == "blocked"


class TestBusinessFailure:
    def test_blocked_status(self):
        ok, err = DAGExecutor._business_failure({"status": "blocked"})
        assert ok and "blocked" in err

    def test_error_key(self):
        ok, err = DAGExecutor._business_failure({"error": "boom"})
        assert ok and err == "boom"

    def test_ok_result(self):
        ok, err = DAGExecutor._business_failure({"status": "ok", "content": "x"})
        assert not ok


class TestHandleRegeneration:
    def test_no_regenerate_when_not_needed(self):
        ex = _make_executor()
        result = {"status": "pass", "needs_regenerate": False}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {})
        assert out["status"] == "pass"

    def test_stops_at_max_generations(self):
        ex = _make_executor()
        task = MagicMock()
        task.stop_event.is_set.return_value = False
        ex.bus.request.return_value = {"needs_regenerate": True, "can_regenerate": True,
                                        "overall_score": 50, "scores": {}}
        result = {"needs_regenerate": True, "can_regenerate": True, "overall_score": 50}
        ex._handle_regeneration(MagicMock(), MagicMock(), result, {}, max_gen=2)
        # 应调用 bus.request 有限次
        assert ex.bus.request.call_count <= 4  # writer + qg per generation


class TestBackoffDelay:
    def test_exponential_grows(self):
        ex = _make_executor()
        d1 = ex._backoff_delay("exponential", 1.0, 0)
        d2 = ex._backoff_delay("exponential", 1.0, 2)
        assert d2 >= d1  # 延迟递增（含 jitter）

    def test_linear(self):
        ex = _make_executor()
        d = ex._backoff_delay("linear", 1.0, 2)
        assert d > 0
