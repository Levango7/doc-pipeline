"""集成点测试 — 验证模块间调用链真正被触发

这些测试不测功能正确性，只验证"A 确实调了 B"。
如果有人删掉集成调用，这些测试会红。
"""
from unittest.mock import MagicMock, patch
import pytest

from pipeline_core.circuit_breaker import CircuitBreaker, CircuitState
from pipeline_core.llm_router import LLMRouter, LLMProvider
from pipeline_core.message_bus_v3 import MessageBus, Message, MessageType
from pipeline_core.base_agent import AgentMeta
from agents.quality_gate import QualityGateAgent
from agents.writer import WriterAgent


# ════════════════════════════════════════════════
# circuit_breaker → alert_manager
# ════════════════════════════════════════════════

class TestCircuitBreakerAlertIntegration:

    def test_open_triggers_alert(self):
        """熔断器进入 OPEN 时必须调 alert_manager.alert"""
        cb = CircuitBreaker("test-agent", failure_threshold=2)
        with patch("pipeline_core.alert_manager.alert") as mock_alert:
            cb.record_failure()
            assert not mock_alert.called, "第 1 次失败不应告警"
            cb.record_failure()
            assert mock_alert.called, "达到阈值进入 OPEN 必须触发 alert"
            call_args = mock_alert.call_args
            assert call_args[0][0] == "critical"
            assert call_args[0][1] == "circuit_breaker"

    def test_close_does_not_alert(self):
        """熔断器恢复 CLOSED 时不触发 critical 告警"""
        cb = CircuitBreaker("test-agent", failure_threshold=1)
        cb.record_failure()  # 进入 OPEN
        # 等待 recovery_timeout 后模拟恢复
        cb.state = CircuitState.HALF_OPEN
        with patch("pipeline_core.alert_manager.alert") as mock_alert:
            cb.record_success()
            assert not mock_alert.called, "恢复 CLOSED 不应触发 critical 告警"


# ════════════════════════════════════════════════
# llm_router → cost_tracker
# ════════════════════════════════════════════════

class TestLLMRouterCostTrackerIntegration:

    def test_chat_records_cost(self):
        """LLM 调用成功后必须调 cost_tracker.record_call"""
        provider = LLMProvider(
            name="test-provider",
            api_url="http://fake",
            api_key="fake-key",
            model="fake-model",
        )
        router = LLMRouter(providers=[provider])

        with patch("pipeline_core.llm_router._call_llm", return_value="LLM response"), \
             patch("pipeline_core.cost_tracker.get_cost_tracker") as mock_ct:
            mock_ct.return_value.record_call = MagicMock()
            content, provider_name = router.chat([{"role": "user", "content": "hi"}])
            assert content == "LLM response"
            mock_ct.return_value.record_call.assert_called_once()
            kwargs = mock_ct.return_value.record_call.call_args.kwargs
            assert kwargs["provider"] == "test-provider"
            assert kwargs["model"] == "fake-model"

    def test_chat_failure_skips_cost(self):
        """LLM 调用失败时不调 cost_tracker.record_call"""
        provider = LLMProvider(
            name="bad-provider",
            api_url="http://fake",
            api_key="fake-key",
            model="fake-model",
        )
        router = LLMRouter(providers=[provider])

        with patch("pipeline_core.llm_router._call_llm", side_effect=RuntimeError("fail")), \
             patch("pipeline_core.cost_tracker.get_cost_tracker") as mock_ct:
            mock_ct.return_value.record_call = MagicMock()
            with pytest.raises(RuntimeError):
                router.chat([{"role": "user", "content": "hi"}])
            mock_ct.return_value.record_call.assert_not_called()


# ════════════════════════════════════════════════
# quality_gate → quality_feedback
# ════════════════════════════════════════════════

class TestQualityGateFeedbackIntegration:

    @pytest.fixture
    def qg(self):
        return QualityGateAgent(
            "quality_gate",
            AgentMeta(name="quality_gate", version="1.0", description=""),
            {},
            MessageBus(enable_persistence=False),
            None,
        )

    def test_handle_records_quality(self, qg):
        """quality_gate.handle 完成后必须调 quality_feedback.record_quality"""
        msg = Message(
            topic="quality_gate.input",
            payload={"content": "# Test\n\nSome content here.", "task_id": "t-int", "queries": ["test"]},
            msg_type=MessageType.REQUEST,
        )
        with patch("pipeline_core.quality_feedback.record_quality") as mock_rq:
            qg.handle(msg)
            mock_rq.assert_called_once()
            kwargs = mock_rq.call_args.kwargs
            assert kwargs["task_id"] == "t-int"
            assert "scores" in kwargs

    def test_handle_records_even_on_failure(self, qg):
        """质量不达标时也要记录评分（用于学习）"""
        watery = "# 文档\n\n这是很好很好的。非常好非常好的。"
        msg = Message(
            topic="quality_gate.input",
            payload={"content": watery, "task_id": "t-fail", "queries": ["test"]},
            msg_type=MessageType.REQUEST,
        )
        with patch("pipeline_core.quality_feedback.record_quality") as mock_rq:
            qg.handle(msg)
            mock_rq.assert_called_once()
            assert mock_rq.call_args.kwargs["task_id"] == "t-fail"


# ════════════════════════════════════════════════
# writer → quality_feedback
# ════════════════════════════════════════════════

class TestWriterFeedbackIntegration:

    def test_restructure_reads_recommendations(self):
        """Writer._restructure_document 必须读 quality_feedback.get_recommendations"""
        writer = WriterAgent(
            "writer",
            AgentMeta(name="writer", version="1.0", description=""),
            {"llm_api_key": "fake-key"},
            MessageBus(enable_persistence=False),
            None,
        )
        writer._load_prompt_template = MagicMock(return_value={
            "system_prompt": "You are a writer.",
            "sections": [{"title": "intro", "prompt": "Write intro"}],
        })

        with patch("pipeline_core.quality_feedback.get_quality_feedback") as mock_gqf:
            mock_gqf.return_value.get_recommendations = MagicMock(return_value=["加强引用"])
            try:
                writer._restructure_document(
                    content="Some content",
                    articles=[{"title": "a", "url": "http://x", "text": "t"}],
                    query="test",
                    title="Test",
                )
            except Exception:
                pass
            mock_gqf.return_value.get_recommendations.assert_called_once()