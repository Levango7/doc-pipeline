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

    def test_status_error_envelope_without_error_key(self):
        """`{"status":"error","message":...}` 也是业务失败。

        实测：ingest 抢订阅 `researcher.input` 后回了这份回执，旧判据只认
        blocked/fail 与 "error" 键，于是节点被记成 success、下游拿到空结果，
        整条 docgen 静默产出占位文档却 exit 0。
        """
        ok, err = DAGExecutor._business_failure(
            {"status": "error", "task_id": "t", "message": "未指定待摄入文件"})
        assert ok, "status=error 的回执不得被判成成功"
        assert err == "未指定待摄入文件"

    def test_skipped_is_not_a_failure(self):
        """无事可做既不是失败也不算交付：status=skipped 不得让节点红。"""
        ok, err = DAGExecutor._business_failure(
            {"status": "skipped", "message": "未指定待摄入文件"})
        assert not ok, f"skipped 不该被判业务失败: {err}"


class TestApplyNodeSuccess:
    def test_skipped_result_is_recorded_as_skipped_step(self):
        ex = _make_executor()
        task = MagicMock()
        node = _make_node(name="ingest")
        dag_node = MagicMock()
        step = MagicMock()
        ex._apply_node_success(task, node, dag_node, step,
                               {"status": "skipped", "message": "未指定待摄入文件"})
        assert step.status == "skipped", (
            f"节点什么都没做却记成 {step.status!r}，报表里就看不出来这一格是空的")
        assert dag_node.status == "success"

    def test_normal_result_still_records_success(self):
        ex = _make_executor()
        dag_node, step = MagicMock(), MagicMock()
        ex._apply_node_success(MagicMock(), _make_node(), dag_node, step,
                               {"status": "ok", "content": "x"})
        assert step.status == "success"


class TestHandleRegeneration:
    def test_no_regenerate_when_not_needed(self):
        ex = _make_executor()
        result = {"status": "pass", "needs_regenerate": False}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {},
                              regenerate_agent="generator", recheck_agent="gate")
        assert out["status"] == "pass"

    def test_stops_at_max_generations(self):
        ex = _make_executor()
        task = MagicMock()
        task.stop_event.is_set.return_value = False
        ex.bus.request.return_value = {"needs_regenerate": True, "can_regenerate": True,
                                        "overall_score": 50, "scores": {}}
        result = {"needs_regenerate": True, "can_regenerate": True, "overall_score": 50}
        ex._handle_regeneration(MagicMock(), MagicMock(), result, {}, max_gen=2,
                            regenerate_agent="generator", recheck_agent="gate")
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
