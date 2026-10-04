"""静默绿修复：空响应 / 占位产出不得被记成 success。

实测过的三类"绿着失败"：
  1. `bus.request` 命中历史幂等键返回 None → dag_executor `result or {}`
     → 节点空转却记 success（复现方式：复用同一个 task_id 再跑一次，
     幂等库在 message_store.py:28 按 checkout 绝对路径共享）。
  2. quality_gate 返回 `{"status":"error","message":"内容为空"}`，
     被 `_handle_regeneration` 末尾的 `final_status = ... else "pass"`
     无条件改写成通过。
  3. writer 缺素材时产出 99 字节占位文档（"未采集到可整合的搜索结果"），
     评分维度只量"写得好不好"，量不出"到底有没有内容"。
"""
import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.dag_executor import DAGExecutor  # noqa: E402


def _make_executor():
    return DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(),
                       MagicMock())


REAL_DOC = (
    "# Kafka 配额与限流\n\n"
    "## 生产者配额\n\n"
    "producer_byte_rate 限制单个客户端每秒可写入的字节数。超过阈值后请求不会"
    "被直接拒绝，而是进入节流等待，从而避免突发流量打爆 broker 的网络线程。"
    "配额服务每 15 秒重新计算一次剩余份额，并在租户之间按权重公平分配。\n\n"
    "## 参考资料\n\n- [Kafka 官方文档](https://kafka.apache.org/documentation)\n"
)

PLACEHOLDER = "# 自动生成文档\n\n> 生成时间: 2026-10-04\n\n未采集到可整合的搜索结果。\n"


# ─── 1. 幂等命中不得伪装成执行结果 ────────────────────────

class TestDuplicateKeyIsNotSuccess:
    def test_bus_returns_structured_error(self, tmp_path):
        from pipeline_core.message_bus_v3 import MessageBus
        bus = MessageBus(db_path=str(tmp_path / "bus.db"), enable_persistence=True)
        try:
            calls = []
            bus.subscribe("idem.err", lambda m: calls.append(m) or {"ok": True})
            first = bus.request("idem.err", "t", "a", {}, timeout=5,
                                idempotency_key="dup-key-1")
            assert first == {"ok": True}
            dup = bus.request("idem.err", "t", "a", {}, timeout=5,
                              idempotency_key="dup-key-1")
            assert isinstance(dup, dict), f"去重命中不得返回 None，实际 {dup!r}"
            assert dup.get("error") == "duplicate_idempotency_key"
            assert dup.get("idempotency_key") == "dup-key-1"
            assert len(calls) == 1, "去重命中不应再次触达订阅者"
        finally:
            bus.shutdown()

    def test_dag_node_raises_on_duplicate_key(self):
        """节点收到 duplicate_idempotency_key 必须抛错，而不是 {} + success。"""
        ex = _make_executor()
        dup = {"status": "error", "error": "duplicate_idempotency_key",
               "idempotency_key": "t1:writer:1"}
        ex.bus.request.return_value = dup
        task = MagicMock()
        task.id = "t1"
        node = MagicMock()
        node.agent_name = "writer"
        node.timeout = 5
        node.dependencies = []
        node.agent_config.pool_size = 1
        node.agent_config.config = {}
        node.agent_config.rate_limit = {}
        task.dag_nodes = {"writer": node}
        ex._build_node_payload = lambda *a, **k: {}
        with pytest.raises(RuntimeError, match="未执行：幂等键"):
            ex.execute_node_from_scheduler(task, node, "in.md", MagicMock())


# ─── 2. 质量门控状态不得被无条件洗成 pass ─────────────────

class TestRegenerationStatusDiscipline:
    def test_error_status_is_not_washed_into_pass(self):
        ex = _make_executor()
        result = {"status": "error", "message": "内容为空", "score": 0}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {})
        assert out["status"] == "error", (
            f"业务失败被软化放行了（原缺陷）：{out['status']}")

    def test_hard_floor_stays_fail(self):
        ex = _make_executor()
        result = {"status": "fail", "hard_floor": True, "needs_regenerate": False,
                  "violations": ["内容过短（40 < 200 字符）"]}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {})
        assert out["status"] == "fail"
        assert out.get("hard_floor") is True

    def test_soft_fail_after_max_generations_still_warns(self):
        """已达重做上限的低分文档维持既有语义：accepted_with_warnings。"""
        ex = _make_executor()
        result = {"status": "fail", "needs_regenerate": True, "can_regenerate": False}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {})
        assert out["status"] == "accepted_with_warnings"

    def test_pass_stays_pass(self):
        ex = _make_executor()
        out = ex._handle_regeneration(MagicMock(), MagicMock(),
                                      {"status": "pass", "needs_regenerate": False}, {})
        assert out["status"] == "pass"


# ─── 3. 硬失败必须穿透 fail_fast=false ─────────────────────

class TestHardFloorIsFatal:
    """`pipeline.fail_fast: false` 的语义是"单个 Agent 的软失败不中断"，
    不能把"产出根本没有内容"也一起兑成 done + exit 0。"""

    def _node(self):
        node = MagicMock()
        node.agent_name = "quality_gate"
        node.timeout = 5
        node.dependencies = []
        node.agent_config.pool_size = 1
        node.agent_config.config = {}
        node.agent_config.rate_limit = {}
        return node

    def test_executor_raises_with_hard_floor_marker(self):
        from pipeline_core.dag_executor import _HARD_FLOOR_PREFIX
        ex = _make_executor()
        ex.bus.request.return_value = {
            "status": "fail", "hard_floor": True, "needs_regenerate": False,
            "violations": ["内容过短（52 < 120 字符）"],
        }
        ex._build_node_payload = lambda *a, **k: {}
        task = MagicMock()
        task.id = "t1"
        task.dag_nodes = {"quality_gate": MagicMock(result=None, status="pending")}
        with pytest.raises(RuntimeError) as exc_info:
            ex.execute_node_from_scheduler(task, self._node(), "in.md", MagicMock())
        assert str(exc_info.value).startswith(_HARD_FLOOR_PREFIX), exc_info.value
        assert "保真底线" in str(exc_info.value)

    def test_both_level_runners_honor_the_marker(self):
        """同步与 async 两条执行路径都得把它当硬失败（防只修一半）。"""
        src = Path(inspect.getfile(DAGExecutor))
        text = src.read_text(encoding="utf-8")
        assert text.count("_HARD_FLOOR_PREFIX") >= 3, (
            "execute_level 与 execute_level_async 都需检查硬失败前缀，"
            f"实际出现 {text.count('_HARD_FLOOR_PREFIX')} 次")

    def test_soft_fail_still_respects_fail_fast_setting(self):
        """checker 的 P1 问题等软失败语义不变：仍由 fail_fast 决定。"""
        ex = _make_executor()
        result = {"status": "fail", "needs_regenerate": True, "can_regenerate": False}
        out = ex._handle_regeneration(MagicMock(), MagicMock(), result, {})
        assert out["status"] == "accepted_with_warnings"
        assert not out.get("hard_floor")

# ─── 4. QualityGate 产出保真底线 ───────────────────────────

class TestFidelityFloor:
    @pytest.fixture
    def gate(self, tmp_path):
        from agents.quality_gate import QualityGateAgent
        from pipeline_core.base_agent import AgentMeta, Message
        agent = QualityGateAgent("quality_gate",
                                 AgentMeta(name="quality_gate", version="2.0"),
                                 {"quiet": True}, None, None)
        return agent, Message

    def test_placeholder_document_is_rejected(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": PLACEHOLDER,
                                            "task_id": "t1", "queries": ["Kafka 配额"]}))
        assert res["status"] == "fail"
        assert res["hard_floor"] is True
        assert res["needs_regenerate"] is False, "占位文档重做也救不回来"
        assert any("占位内容" in v for v in res["violations"]), res["violations"]

    def test_too_short_document_is_rejected(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": "# 标题\n\n很短。\n",
                                            "task_id": "t2"}))
        assert res["hard_floor"] is True
        assert any("内容过短" in v for v in res["violations"]), res["violations"]

    def test_real_document_passes_the_floor(self, gate):
        """底线只量"有没有内容"，不能把正常文档也一起拦掉。"""
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC, "task_id": "t3",
                                            "queries": ["Kafka 配额"]}))
        assert not res.get("hard_floor"), res.get("violations")
        assert res["overall_score"] > 0

    def test_thin_but_real_content_is_scored_not_hard_failed(self, gate):
        """底线只拦"没有产出"；薄内容仍走评分→重做（既有语义，不许被劫持）。"""
        agent, Message = gate
        watery = (
            "# Python 简介\n\n"
            "Python 是一门非常通用的编程语言，它很简单，很好用，也很流行，很多人都非常"
            "喜欢它，觉得它是最好的语言，学起来也很容易，用起来也很方便，社区也很庞大，"
            "生态也很丰富，做什么都行，写脚本很快，写服务也行，大家都说好。\n"
        )
        assert len(watery.strip()) >= 120, f"样本需过底线才能测评分路径：{len(watery)}"
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": watery, "task_id": "t4",
                                            "queries": ["Python"]}))
        assert not res.get("hard_floor"), res.get("violations")
        assert res.get("needs_regenerate") is True, \
            f"水话应由评分判出，而不是底线：{res.get('overall_score')}"

    def test_floor_is_configurable(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC[:80],
                                            "task_id": "t4b",
                                            "config": {"min_output_chars": 50}}))
        assert not res.get("hard_floor"), res.get("violations")

    def test_invalid_min_chars_falls_back_to_default(self, gate):
        agent, Message = gate
        res = agent.handle(Message(topic="quality_gate.input",
                                   payload={"content": REAL_DOC[:80], "task_id": "t5",
                                            "config": {"min_output_chars": "很多"}}))
        assert res.get("hard_floor") is True
