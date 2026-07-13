"""故障注入测试：验证熔断器与死信队列(DLQ)在真实异常下的行为。

覆盖三个真实场景：
1. DLQ 真实重放 —— 故障消息进 DLQ 后可 replay 并重新投递
2. 熔断器状态机 —— 连续失败 OPEN、成功重置、HALF_OPEN 恢复
3. Orchestrator 集成 —— 故障 agent 连续失败触发熔断，成功执行不误熔断
"""
import sys, os, time
from pathlib import Path

from pipeline_core.message_bus_v3 import MessageBus
from pipeline_core.circuit_breaker import CircuitBreaker, CircuitState
from pipeline_core.base_agent import BaseAgent, AgentMeta, AgentStatus


# ── 1. DLQ 真实重放 ──────────────────────────────

class TestDLQReplay:
    def test_dlq_replay_redelivers(self):
        """故障 subscriber 异常 → 进 DLQ → 修复 → replay → 消息重投"""
        import tempfile
        db = os.path.join(tempfile.mkdtemp(), "replay.db")
        bus = MessageBus(db_path=db)

        received = []
        def faulty(msg):
            raise RuntimeError("boom")
        def fixed(msg):
            received.append(msg)

        bus.subscribe("topic.x", faulty)
        # 第一条消息触发异常 → 进 DLQ
        bus.publish("topic.x", "test", {"v": 1})
        time.sleep(0.3)
        dlq = bus.list_dlq()
        assert len(dlq) == 1, f"期望 1 条死信，实际 {len(dlq)}"
        dlq_id = dlq[0]["id"]

        # 替换 subscriber 为修复版
        bus.unsubscribe("topic.x", faulty)
        bus.subscribe("topic.x", fixed)

        # 真实重放（返回重放的消息 dict）
        replayed = bus.replay_dlq(dlq_id)
        assert replayed is not None, "replay 应返回消息 dict"
        assert replayed["replay_count"] >= 1
        time.sleep(0.3)

        assert len(received) == 1, "replay 后消息应被重新投递"
        assert received[0].payload.get("v") == 1

        # replay_count 应递增
        dlq_after = bus.list_dlq()
        assert dlq_after[0]["replay_count"] >= 1

        bus._shutdown_event.set()

    def test_dlq_does_not_crash_bus(self):
        """subscriber 异常不应导致整条总线崩溃"""
        import tempfile
        db = os.path.join(tempfile.mkdtemp(), "crash.db")
        bus = MessageBus(db_path=db)

        ok_received = []
        def faulty(msg):
            raise ValueError("fail")
        def good(msg):
            ok_received.append(msg)

        bus.subscribe("t.a", faulty)
        bus.subscribe("t.b", good)

        bus.publish("t.a", "s", {})  # 异常
        bus.publish("t.b", "s", {"x": 1})  # 正常
        time.sleep(0.3)

        assert len(ok_received) == 1, "总线崩溃会导致正常 subscriber 收不到"
        assert bus.list_dlq()[0]["error"] == "fail"

        bus._shutdown_event.set()


# ── 2. 熔断器状态机（纯单元，真实状态转换）──────

class TestCircuitBreakerStateMachine:
    def test_open_after_threshold_then_half_open(self):
        cb = CircuitBreaker("agent_x", failure_threshold=3, recovery_timeout=0.1)

        # 连续 3 次失败 → OPEN
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

        # 冷却后 → HALF_OPEN
        time.sleep(0.15)
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

        # HALF_OPEN 成功 → CLOSED
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_success_resets_failure_count(self):
        """成功应重置连续失败计数（修复前的 bug：只增不减）"""
        cb = CircuitBreaker("agent_y", failure_threshold=3, recovery_timeout=60)

        cb.record_failure()
        cb.record_failure()  # 2 次失败
        cb.record_success()  # 成功重置
        assert cb.failure_count == 0

        # 再失败 2 次不应触发熔断（因为之前被重置）
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED, "成功重置后不应误熔断"


# ── 3. Orchestrator 熔断集成（mock agent 注入故障）──

class _FaultyAgent(BaseAgent):
    """可注入故障的 mock agent"""
    def __init__(self, name, fail_times=0, meta=None, config=None, bus=None, registry=None):
        if meta is None:
            meta = AgentMeta(name=name, version="1.0", description="mock")
        super().__init__(name, meta, config or {}, bus, registry)
        self._fail_times = fail_times
        self._calls = 0

    def handle(self, msg):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RuntimeError(f"injected fault #{self._calls}")
        return {"status": "ok", "calls": self._calls}


class TestOrchestratorCircuitBreaker:
    def test_success_does_not_trip_breaker(self):
        """连续成功执行不应误触发熔断（验证修复前 bug）"""
        from pipeline_core import PipelineOrchestrator
        from types import SimpleNamespace

        o = PipelineOrchestrator(checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"))
        node = SimpleNamespace(
            agent_name="mock_ok",
            agent_config=SimpleNamespace(circuit_breaker={"enabled": True, "failure_threshold": 2, "recovery_timeout": 1}),
        )
        task = SimpleNamespace()

        # 连续 5 次成功 → 不应熔断
        for _ in range(5):
            o._circuit_breaker_success(node)
        breaker = o._cb_registry.get_or_create("mock_ok")
        assert breaker.failure_count == 0, "成功路径不应累加失败"
        assert breaker.state == CircuitState.CLOSED

    def test_consecutive_failures_trip_breaker(self):
        """连续失败达到阈值 → 熔断返回 True"""
        from pipeline_core import PipelineOrchestrator
        from types import SimpleNamespace

        o = PipelineOrchestrator(checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"))
        node = SimpleNamespace(
            agent_name="mock_fail",
            agent_config=SimpleNamespace(circuit_breaker={"enabled": True, "failure_threshold": 3, "recovery_timeout": 1}),
        )
        task = SimpleNamespace()

        tripped = False
        for _ in range(3):
            if o._circuit_breaker(node, task):
                tripped = True
                break
        assert tripped is True, "连续 3 次失败应触发熔断"
        breaker = o._cb_registry.get_or_create("mock_fail")
        assert breaker.state == CircuitState.OPEN


# ── 4. DLQ 真实自愈（REQUEST 消息重放回填 task）──

class _ReplayMockAgent(BaseAgent):
    """自愈测试中用的 mock agent：记录 handle 调用次数"""
    def __init__(self, name, meta=None, config=None, bus=None, registry=None):
        if meta is None:
            meta = AgentMeta(name=name, version="1.0", description="mock")
        super().__init__(name, meta, config or {}, bus, registry)
        self.handle_calls = 0
        self.last_msg = None
    def handle(self, msg):
        self.handle_calls += 1
        self.last_msg = msg
        return {"status": "ok", "content": "修复后的内容", "calls": self.handle_calls}


class TestDLQSelfHeal:
    def test_request_replay_re_executes_and_backfills(self):
        """REQUEST 死信 → orchestrator.replay_dlq → 真实重执行 node → 回填 task.result"""
        from pipeline_core import PipelineOrchestrator
        from pipeline_core.message_bus_v3 import MessageBus
        from types import SimpleNamespace

        o = PipelineOrchestrator(checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"))
        # 用真实总线（带持久化 store）
        db_path = str(Path(__file__).parent / ".test_dlq_replay.db")
        if os.path.exists(db_path):
            os.remove(db_path)
        o.bus = MessageBus(db_path=db_path)

        # 注册真实 mock agent 到 registry（模拟 writer node）
        agent = _ReplayMockAgent("writer")
        o.registry.register(AgentMeta(name="writer", version="1.0", description="mock"), agent)

        # 构造一条 REQUEST 死信（模拟 writer 故障进 DLQ）
        from pipeline_core.message_bus_v3 import Message, MessageType
        dlq_msg = Message(
            topic="writer.input", payload={"content": "原始输入", "task_id": "task-abc", "node": "writer"},
            msg_type=MessageType.REQUEST, from_agent="orchestrator", to_agent="writer",
            correlation_id="c1", trace_id="t1",
        )
        o.bus._store.move_to_dlq(dlq_msg, "boom (injected fault)")
        dlq = o.bus.list_dlq()
        assert len(dlq) == 1, "应有一条死信"
        dlq_id = dlq[0]["id"]

        # 把 task 放进内存（模拟正在运行）
        task = SimpleNamespace(
            id="task-abc",
            result={},
            dag_nodes={"writer": SimpleNamespace(result=None, status="failed")},
        )
        o._running_tasks["task-abc"] = task

        # 执行真实重放（直接调 agent.handle）
        res = o.replay_dlq(dlq_id)
        assert res is not None
        assert res["node"] == "writer"
        assert res["task_id"] == "task-abc"
        assert res["result"]["status"] == "ok"

        # 验证 agent 被真实重执行
        assert agent.handle_calls == 1, "agent.handle 应被真实重调一次"
        # 验证回填
        assert task.result["writer"]["content"] == "修复后的内容", "task.result 应被回填"
        assert task.dag_nodes["writer"].status == "success", "node 状态应恢复"
        # 验证重放计数递增
        assert o.bus.list_dlq()[0]["replay_count"] >= 1

        o.bus._shutdown_event.set()
