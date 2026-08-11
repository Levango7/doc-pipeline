"""P1 集成测试：质量门控 regen 循环 + 全链路故障

覆盖两个未验证的临界场景：
1. Regen 循环：真实低分内容 → 重生成 → 变好的端到端验证
2. 全链路故障：agent.handle 异常 → 总线捕获进 DLQ，编排器不阻塞

依赖真实组件（QualityGateAgent、MessageBus、PipelineOrchestrator）。
"""
import os
import tempfile
from pathlib import Path

from agents.quality_gate import QualityGateAgent
from pipeline_core import PipelineOrchestrator
from pipeline_core.base_agent import AgentMeta, BaseAgent
from pipeline_core.message_bus_v3 import Message, MessageBus, MessageType

# ── 测试数据 ────────────────────────────────

WATERY_CONTENT = """# 简介

在日常工作中，我们经常会遇到各种各样的问题。
一般来说，解决这些问题需要一定的经验和技巧。
总的来说，这是一个非常重要的概念。

# 主要内容

从某种程度上说，这个问题可以从多个角度来分析。
首先，我们要明确的是，没有一种通用的解决方案。

# 总结

综上所述，我们需要持续关注这个领域的发展。
"""

GOOD_CONTENT = """# Python 异步编程指南

> 主题: Python 异步编程

## async/await 基础

Python 3.5 引入 async def 定义协程，await 挂起协程。

```python
import asyncio
async def fetch(url):
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.json()
```

## 事件循环

asyncio.run() 是 Python 3.7+ 入口。
"""


class _RegenMockWriter(BaseAgent):
    """按调用次数返回不同质量内容的 writer mock"""
    def __init__(self, name="writer", meta=None, config=None, bus=None, registry=None):
        if meta is None:
            meta = AgentMeta(name=name, version="1.0", description="mock")
        super().__init__(name, meta, config or {}, bus, registry)
        self.call_count = 0

    def handle(self, msg):
        self.call_count += 1
        if self.call_count == 1:
            return {"status": "ok", "content": WATERY_CONTENT}
        return {"status": "ok", "content": GOOD_CONTENT}


class _FailingAgent(BaseAgent):
    """一直失败的 agent"""
    def __init__(self, name, meta=None, config=None, bus=None, registry=None):
        if meta is None:
            meta = AgentMeta(name=name, version="1.0", description="")
        super().__init__(name, meta, config or {}, bus, registry)
        self.call_count = 0

    def handle(self, msg):
        self.call_count += 1
        raise RuntimeError(f"injected #{self.call_count}")


# ════════════════════════════════════════════════
# P1-A: 质量门控 regen 循环
# ════════════════════════════════════════════════

class TestRegenLoop:

    def test_quality_gate_rejects_watery(self):
        """水话内容 → quality_gate 返回 needs_regenerate=True"""
        qg = QualityGateAgent(
            "quality_gate", AgentMeta(name="quality_gate", version="1.0", description=""), {},
            MessageBus(enable_persistence=False), None,
        )
        msg = Message(topic="quality_gate.input", payload={
            "content": WATERY_CONTENT, "task_id": "t", "queries": ["Python"],
        }, msg_type=MessageType.REQUEST)
        result = qg.handle(msg)
        assert result is not None
        assert result.get("overall_score", 100) < 70, f"得分 {result.get('overall_score')} 应 < 70"
        assert result.get("needs_regenerate") is True

    def test_good_content_passes(self):
        """真实技术内容 → quality_gate 返回 needs_regenerate=False"""
        qg = QualityGateAgent(
            "quality_gate", AgentMeta(name="quality_gate", version="1.0", description=""), {},
            MessageBus(enable_persistence=False), None,
        )
        msg = Message(topic="quality_gate.input", payload={
            "content": GOOD_CONTENT, "task_id": "t", "queries": ["Python"],
        }, msg_type=MessageType.REQUEST)
        result = qg.handle(msg)
        assert result is not None
        assert result.get("overall_score", 0) >= 70, f"得分 {result.get('overall_score')} 应 >= 70"
        assert result.get("needs_regenerate") is False

    def test_regen_loop_improves(self):
        """模拟 regen 循环：低分→重写→高分，writer 被重调"""
        bus = MessageBus(enable_persistence=False)
        orch = PipelineOrchestrator(
            checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"),
        )

        writer = _RegenMockWriter("writer")
        qg = QualityGateAgent(
            "quality_gate", AgentMeta(name="quality_gate", version="1.0", description=""), {},
            bus, orch.registry,
        )
        orch.registry.register(AgentMeta(name="writer", version="1.0", description=""), writer)
        orch.registry.register(
            AgentMeta(name="quality_gate", version="1.0", description=""), qg,
        )

        # 首次 writer 执行（内容太水）
        writer.handle(Message(topic="writer.input", payload={}, msg_type=MessageType.REQUEST))

        # quality check → low score
        msg = Message(topic="quality_gate.input", payload={
            "task_id": "r", "content": WATERY_CONTENT,
            "config": {"max_regenerations": 3}, "queries": ["Python"],
        }, msg_type=MessageType.REQUEST)
        result = qg.handle(msg)
        assert result.get("needs_regenerate") is True

        # regen：重新调 writer（第二次调用应返回好内容）
        feedback = {
            "quality_scores": result.get("scores", {}),
            "overall_score": result.get("overall_score", 0),
            "style_issues": result.get("style_issues", []),
            "generation_count": result.get("generation_count", 0) + 1,
        }
        wr = writer.handle(Message(
            topic="writer.input", payload={**msg.payload, **feedback},
            msg_type=MessageType.REQUEST,
        ))
        assert wr.get("content") == GOOD_CONTENT, "第二次 writer 应返回好内容"

        # quality re-check → pass
        r2 = qg.handle(Message(topic="quality_gate.input", payload={
            **msg.payload, "content": GOOD_CONTENT, **feedback,
        }, msg_type=MessageType.REQUEST))
        assert r2.get("overall_score", 0) >= 70, f"regen 后得分 {r2.get('overall_score')} 应 >= 70"
        assert r2.get("needs_regenerate") is False

        bus._shutdown_event.set()

    def test_max_regen_limit(self):
        """writer 持续差内容 → max_gen 次后 can_regenerate=False"""
        bus = MessageBus(enable_persistence=False)
        orch = PipelineOrchestrator(
            checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"),
        )
        max_gen = 2

        class _BadWriter(BaseAgent):
            def __init__(self, *a, **kw):
                super().__init__(
                    "writer", AgentMeta(name="writer", version="1.0", description=""), {}, None, None,
                )
                self.call_count = 0
            def handle(self, msg):
                self.call_count += 1
                return {"status": "ok", "content": WATERY_CONTENT}

        bad_w = _BadWriter()
        qg = QualityGateAgent(
            "quality_gate", AgentMeta(name="quality_gate", version="1.0", description=""),
            {"max_regenerations": max_gen}, bus, orch.registry,
        )
        orch.registry.register(bad_w.meta, bad_w)
        orch.registry.register(qg.meta, qg)

        msg = Message(topic="quality_gate.input", payload={
            "task_id": "ml", "content": WATERY_CONTENT,
            "config": {"max_regenerations": max_gen}, "queries": ["Python"],
        }, msg_type=MessageType.REQUEST)

        result = qg.handle(msg)
        gen = 0
        while result.get("needs_regenerate") and result.get("can_regenerate") and gen < max_gen + 1:
            gen += 1
            fb = {
                "quality_scores": result.get("scores", {}),
                "overall_score": result.get("overall_score", 0),
                "style_issues": result.get("style_issues", []),
                "generation_count": result.get("generation_count", 0) + 1,
            }
            bad_w.handle(None)  # writer call
            msg.payload["content"] = WATERY_CONTENT
            msg.payload["generation_count"] = fb["generation_count"]
            result = qg.handle(Message(
                topic="quality_gate.input", payload={**msg.payload, **fb},
                msg_type=MessageType.REQUEST,
            ))

        assert gen == max_gen, f"应执行 {max_gen} 次 regen，实际 {gen}"
        assert not result.get("can_regenerate", True), "应已达上限"
        bus._shutdown_event.set()


# ════════════════════════════════════════════════
# P1-B: 全链路故障集成
# ════════════════════════════════════════════════

class TestFailureIntegration:
    """agent.handle 异常 → 总线捕获进 DLQ，编排器不阻塞"""

    def test_faulty_agent_request_returns_none(self):
        """bus.request 调故障 agent → 异常被总线捕获 → 返回 None（非阻塞）"""
        bus = MessageBus(db_path=os.path.join(tempfile.mkdtemp(), "fail_req.db"))
        fail_agent = _FailingAgent("fail-agent")

        def _wrapped(msg):
            return fail_agent.handle(msg)
        bus.subscribe("fail-agent.input", _wrapped)

        # bus.request 不应阻塞或抛异常
        result = bus.request(
            topic="fail-agent.input", from_a="test", to_a="fail-agent",
            payload={"task_id": "t"}, timeout=5,
        )
        assert result is None, f"故障 agent 的 request 应返回 None，实际 {result}"

        # 验证：异常进了 DLQ
        dlq = bus.list_dlq()
        assert len(dlq) >= 1, "故障 agent 的异常应进入 DLQ"
        assert fail_agent.call_count >= 1, "agent.handle 应被调过"

        bus._shutdown_event.set()

    def test_node_execution_handles_faulty_agent(self):
        """_execute_node_from_scheduler 对故障 agent → 返回 None（不阻塞）"""
        bus = MessageBus(enable_persistence=False)
        orch = PipelineOrchestrator(
            checkpoint_dir=str(Path(__file__).parent / ".test_checkpoints"),
        )
        orch.bus = bus

        fail_agent = _FailingAgent("fail-node")
        # 注册到 registry（_execute_node_from_scheduler 先查 registry 找 instance）
        orch.registry.register(
            AgentMeta(name="fail-node", version="1.0", description=""), fail_agent,
        )
        # subscriber（bus.request 通过 subscriber 转发）
        def _wrapped(msg):
            return fail_agent.handle(msg)
        bus.subscribe("fail-node.input", _wrapped)

        from types import SimpleNamespace
        node = SimpleNamespace(
            agent_name="fail-node_pool_0",
            agent_config=SimpleNamespace(
                agent="fail-node", config={},
                circuit_breaker={"enabled": True, "failure_threshold": 2, "recovery_timeout": 1},
                pool_size=1, idempotency=True, max_retries=1,
                retry_initial_delay=0.1, retry_backoff="linear",
                rate_limit={},
            ),
            dependencies=[],
            timeout=5,
        )
        task = SimpleNamespace(
            id="fail-node-test", result={},
            dag_nodes={node.agent_name: SimpleNamespace(
                result=None, status="pending", attempts=0,
            )},
            status="running",
        )
        plan = SimpleNamespace(
            pipeline_name="test", raw={"pipeline": {"output": "out.md"}},
        )

        tdir = tempfile.mkdtemp()
        input_file = os.path.join(tdir, "input.md")
        with open(input_file, "w") as f:
            f.write("query")

        # 不应抛异常 → 返回 None
        result = orch._execute_node_from_scheduler(task, node, input_file, plan)
        assert result is None or result == dict(), \
            f"故障 agent 执行应返回 None/空 dict，实际 {result}"
        assert fail_agent.call_count >= 1, "agent.handle 应被调过"

        bus._shutdown_event.set()
