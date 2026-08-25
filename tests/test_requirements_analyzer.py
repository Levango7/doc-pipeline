"""requirements_analyzer Agent 测试 — 规则分析 / DocumentSpec 往返 / LLM 路径与回退 / handle 行为

不 mock BaseAgent 基类行为；LLM 路径通过 mock get_router 覆盖。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.requirements_analyzer import (
    AUDIENCE_LEVELS,
    DEPTH_ENUM,
    DOC_TYPE_ENUM,
    DocumentSpec,
    RequirementsAnalyzerAgent,
    _extract_keywords,
    _rule_based_analysis,
    analyze,
)


@pytest.fixture
def agent():
    """绕过 BaseAgent 线程/总线初始化，仅测核心逻辑"""
    a = RequirementsAnalyzerAgent.__new__(RequirementsAnalyzerAgent)
    a._confidence_threshold = 0.7
    a._llm_enabled = True
    a._max_questions = 3
    a.config = {}
    a.log_info = MagicMock()
    a.log_warning = MagicMock()
    a.log_error = MagicMock()
    a.report = MagicMock()
    a.publish = MagicMock()
    return a


class TestRuleBasedAnalysis:

    def test_detects_api_doc_type(self):
        spec = _rule_based_analysis("Kafka 集群 API 接口文档整理", {})
        assert spec.doc_type == "api"

    def test_detects_manual_and_depth(self):
        spec = _rule_based_analysis("Docker 部署手册，需要深度覆盖网络配置", {})
        assert spec.doc_type == "手册"
        assert spec.depth == "deep-research"

    def test_detects_audience_level(self):
        spec = _rule_based_analysis("Python 入门教程面向初学者", {})
        assert spec.audience == "入门"

    def test_scope_extracts_keywords(self):
        spec = _rule_based_analysis("Redis 分布式锁的实现原理与生产实践", {})
        assert len(spec.scope) >= 2
        assert any("redis" in s.lower() for s in spec.scope)

    def test_short_input_flags_ambiguity(self):
        spec = _rule_based_analysis("Kafka", {})
        fields = {a["field"] for a in spec.ambiguities}
        assert "scope" in fields
        assert spec.confidence < 1.0

    def test_unknown_type_flags_doc_type_question(self):
        spec = _rule_based_analysis("量子纠缠与咖啡风味之间的关联性探索随笔记录", {})
        fields = {a["field"] for a in spec.ambiguities}
        assert "doc_type" in fields

    def test_url_extracted_into_sources(self):
        spec = _rule_based_analysis(
            "参考 https://kafka.apache.org/documentation 写一份方案", {})
        assert any("kafka.apache.org" in s for s in spec.sources)


class TestDocumentSpec:

    def test_to_dict_from_dict_roundtrip(self):
        spec = DocumentSpec(
            doc_type="报告", scope=["a", "b"], audience="高级",
            depth="quick", constraints={"max_words": 5000},
            sources=["https://example.com"], template="t1",
            language="zh", confidence=0.8,
            ambiguities=[{"field": "scope", "question": "?", "suggestion": "-"}],
        )
        d = spec.to_dict()
        restored = DocumentSpec.from_dict(d)
        assert restored == spec

    def test_from_dict_ignores_unknown_keys(self):
        restored = DocumentSpec.from_dict({"doc_type": "报告", "unknown_field": 1})
        assert restored.doc_type == "报告"

    def test_enum_constants_consistent(self):
        # 枚举表供 LLM 输出校验使用，必须非空
        assert DOC_TYPE_ENUM and DEPTH_ENUM and AUDIENCE_LEVELS


class TestExtractKeywords:

    def test_dedup_and_order(self):
        kws = _extract_keywords("Kafka Kafka 性能优化 performance tuning")
        lowered = [k.lower() for k in kws]
        assert len(lowered) == len(set(lowered))

    def test_caps_at_limit(self):
        text = " ".join(f"主题{i}关于数据库分片策略的演进" for i in range(15))
        assert len(_extract_keywords(text, max_keywords=5)) <= 5

    def test_filters_stopwords(self):
        kws = _extract_keywords("介绍基本概念")
        assert not any(k in ("介绍", "基本") for k in kws if len(k) == 2)


class TestHandle:

    def _msg(self, payload=None):
        msg = MagicMock()
        msg.payload = payload or {}
        return msg

    def test_skip_on_empty_input(self, agent):
        result = agent.handle(self._msg({}))
        assert result["status"] == "skip"

    def test_rule_path_returns_spec(self, agent):
        agent._llm_enabled = False
        result = agent.handle(self._msg({"input": "Kafka 高可用架构方案", "task_id": "t1"}))
        assert result["status"] == "ok"
        assert result["task_id"] == "t1"
        assert result["spec"]["raw_input"].startswith("Kafka")
        assert isinstance(result["confidence"], float)
        assert result["needs_clarification"] == (result["confidence"] < 0.7)

    def test_reads_input_file_in_dag_mode(self, agent, tmp_path):
        input_file = tmp_path / "input.md"
        input_file.write_text("PostgreSQL 索引优化深度报告\n", encoding="utf-8")
        agent._llm_enabled = False
        payload = {"input_file": str(input_file), "task_id": "t2",
                   "queries": ["PostgreSQL 索引优化深度报告"]}
        result = agent.handle(self._msg(payload))
        assert result["status"] == "ok"
        scope_lower = [s.lower() for s in result["spec"]["scope"]]
        assert "postgresql" in scope_lower
        assert result["spec"]["doc_type"] == "报告"

    @pytest.mark.skipif(sys.platform != "linux", reason="路径存在性语义仅 POSIX 一致")
    def test_missing_input_file_falls_back_to_queries(self, agent):
        agent._llm_enabled = False
        payload = {"input_file": "/nonexistent/x.md", "queries": ["消息队列选型报告"]}
        result = agent.handle(self._msg(payload))
        assert result["status"] == "ok"
        assert result["spec"]["doc_type"] == "报告"

    def test_low_confidence_truncates_questions(self, agent):
        agent._max_questions = 1
        agent._llm_enabled = False
        result = agent.handle(self._msg({"input": "简短"}))
        if result["needs_clarification"]:
            assert len(result["spec"]["ambiguities"]) <= 1

    def test_llm_failure_falls_back_to_rules(self, agent):
        with patch("pipeline_core.llm_router.get_router") as mock_gr:
            mock_router = MagicMock()
            mock_router.get_active_providers.return_value = [MagicMock()]
            mock_router.chat.side_effect = RuntimeError("LLM down")
            mock_gr.return_value = mock_router
            result = agent.handle(self._msg({"input": "微服务治理白皮书"}))
        assert result["status"] == "ok"
        agent.log_warning.assert_called()

    def test_llm_success_parses_json(self, agent):
        llm_json = (
            '{"doc_type": "方案", "scope": ["服务网格", "流量治理"], '
            '"audience": "高级", "depth": "deep-research", "constraints": {}, '
            '"sources": [], "template": "", "language": "zh", '
            '"confidence": 0.9, "ambiguities": []}'
        )
        with patch("pipeline_core.llm_router.get_router") as mock_gr:
            mock_router = MagicMock()
            mock_router.get_active_providers.return_value = [MagicMock()]
            mock_router.chat.return_value = (llm_json, "test-provider")
            mock_gr.return_value = mock_router
            result = agent.handle(self._msg({"input": "Istio 流量治理深入方案"}))
        assert result["status"] == "ok"
        assert result["spec"]["doc_type"] == "方案"
        assert result["spec"]["depth"] == "deep-research"
        assert result["confidence"] == 0.9
        assert not result["needs_clarification"]

    def test_llm_invalid_enums_fall_back(self, agent):
        llm_json = (
            '{"doc_type": "科幻小说", "scope": [], "audience": "外星人", '
            '"depth": "超深", "confidence": "很高", "ambiguities": []}'
        )
        with patch("pipeline_core.llm_router.get_router") as mock_gr:
            mock_router = MagicMock()
            mock_router.get_active_providers.return_value = [MagicMock()]
            mock_router.chat.return_value = (llm_json, "test-provider")
            mock_gr.return_value = mock_router
            result = agent.handle(self._msg({"input": "任意输入文本内容足够长以避免歧义标记"}))
        assert result["spec"]["doc_type"] == "其他"
        assert result["spec"]["audience"] == "中级"
        assert result["spec"]["depth"] == "standard"
        assert isinstance(result["spec"]["confidence"], float)

    def test_llm_disabled_skips_llm_path(self, agent):
        agent._llm_enabled = False
        with patch("pipeline_core.llm_router.get_router") as mock_gr:
            result = agent.handle(self._msg({"input": "API 网关对比综述"}))
        mock_gr.assert_not_called()
        assert result["status"] == "ok"


class TestAnalyzeHelper:

    def test_analyze_falls_back_without_llm(self):
        with patch("pipeline_core.llm_router.get_router") as mock_gr:
            mock_gr.side_effect = RuntimeError("no llm")
            spec = analyze("Elasticsearch 深度调优报告",
                           {"requirements_analyzer": {"llm_enabled": True}})
        assert isinstance(spec, DocumentSpec)
        assert spec.doc_type == "报告"

    def test_analyze_rule_path_when_llm_disabled(self):
        spec = analyze("Nginx 手册", {"requirements_analyzer": {"llm_enabled": False}})
        assert spec.doc_type == "手册"
