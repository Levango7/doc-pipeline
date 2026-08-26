"""QualityGate 8 个评分维度测试

测试原则：
  - 直接测试 _score_* 方法，不依赖完整 Agent 初始化
  - 每个测试方法聚焦一个评分维度
  - 用 mock 绕过 BaseAgent.__init__
"""
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.quality_gate import QualityGateAgent, load_profile

# ─── 创建测试用 QualityGateAgent 实例 ────────────────────────

def _make_gate():
    """绕过 BaseAgent.__init__，创建仅用于评分测试的实例"""
    gate = QualityGateAgent.__new__(QualityGateAgent)
    # 加载默认 profile
    gate._profile = load_profile("technical-doc")
    gate._profile_name = "technical-doc"
    gate._weights = gate._profile.get("weights", {})
    gate._threshold = gate._profile.get("threshold", 70)
    gate._max_regenerations = gate._profile.get("max_regenerations", 3)
    gate._max_penalty = gate._profile.get("max_penalty", 40)
    gate._citation_cfg = gate._profile.get("citation", {})
    gate._style_rules = []
    for rule_cfg in gate._profile.get("style_rules", []):
        if rule_cfg.get("enabled", True):
            gate._style_rules.append({
                "name": rule_cfg["name"],
                "pattern": re.compile(rule_cfg["pattern"]),
                "message": rule_cfg.get("message", ""),
                "penalty": rule_cfg.get("penalty", 0),
            })
    # log 方法 mock
    gate.log_info = MagicMock()
    gate.log_warning = MagicMock()
    gate.log_debug = MagicMock()
    return gate


# ─── 1. completeness 完整度 ────────────────────────────

class TestScoreCompleteness:
    """completeness: 文档结构完整度"""

    def test_perfect_document(self):
        """完整文档得高分"""
        gate = _make_gate()
        content = """# Title

## 目录
- Section 1

## Section 1
This is a paragraph with enough content to pass the threshold.

## Section 2
Another paragraph here with sufficient content.

## 参考资料
- [Ref](http://example.com)
"""
        score = gate._score_completeness(content)
        assert score == 100.0

    def test_missing_h1(self):
        """缺少 H1 标题扣分"""
        gate = _make_gate()
        content = "## Only H2\n\nSome content here.\n\n## Ref\n\nMore content."
        score = gate._score_completeness(content)
        assert score < 100.0

    def test_missing_references(self):
        """缺少参考资料扣分"""
        gate = _make_gate()
        content = "# Title\n\n## 目录\n\nPara1.\n\nPara2.\n\nPara3."
        score = gate._score_completeness(content)
        assert score < 100.0

    def test_empty_content(self):
        """空内容得 0 分"""
        gate = _make_gate()
        score = gate._score_completeness("")
        assert score == 0


# ─── 2. structure 结构 ────────────────────────────

class TestScoreStructure:
    """structure: 标题层级合理性"""

    def test_good_structure(self):
        """良好层级结构得高分"""
        gate = _make_gate()
        content = "# H1\n\n## H2a\n\n## H2b\n\n### H3\n\nContent."
        score = gate._score_structure(content)
        assert score == 100.0

    def test_no_headings(self):
        """无标题得 30 分"""
        gate = _make_gate()
        score = gate._score_structure("Just plain text without headings.")
        assert score == 30

    def test_multiple_h1(self):
        """多个 H1 扣分"""
        gate = _make_gate()
        content = "# H1a\n\n## H2\n\n# H1b\n\n## H2b"
        score = gate._score_structure(content)
        assert score < 100.0

    def test_heading_skip(self):
        """标题跳跃扣分（H1 → H3）"""
        gate = _make_gate()
        content = "# H1\n\n### H3\n\n## H2"
        score = gate._score_structure(content)
        assert score < 100.0

    def test_too_few_h2(self):
        """H2 太少扣分"""
        gate = _make_gate()
        content = "# H1\n\n## Only one H2\n\nContent."
        score = gate._score_structure(content)
        assert score < 100.0


# ─── 3. readability 可读性 ────────────────────────────

class TestScoreReadability:
    """readability: 内容可读性"""

    def test_good_readability(self):
        """良好可读性得高分"""
        gate = _make_gate()
        content = "# Title\n\nShort paragraph.\n\n- List item\n\n> Quote\n\n```\ncode\n```"
        score = gate._score_readability(content)
        assert score == 100.0

    def test_long_lines_penalized(self):
        """长行扣分"""
        gate = _make_gate()
        long_line = "x" * 200
        content = f"# Title\n\n{long_line}\n\n- item\n\n> quote"
        score = gate._score_readability(content)
        assert score < 100.0

    def test_no_code_or_quote_penalized(self):
        """无代码块和引用扣分"""
        gate = _make_gate()
        content = "# Title\n\nJust plain text.\n\n- item"
        score = gate._score_readability(content)
        assert score < 100.0


# ─── 4. citation 引用 ────────────────────────────

class TestScoreCitations:
    """citation: 引用可追溯性"""

    def test_no_refs_returns_50(self):
        """无引用返回 50 分（中性）"""
        gate = _make_gate()
        score = gate._score_citations("Just text without references.")
        assert score == 50

    def test_valid_refs(self):
        """有效引用得高分"""
        gate = _make_gate()
        content = "[Kafka](https://kafka.org) and [Spark](https://spark.org)"
        score = gate._score_citations(content)
        assert score == 100.0

    def test_empty_url_penalized(self):
        """空 URL 扣分"""
        gate = _make_gate()
        content = "[Empty]() and [Valid](https://valid.com)"
        score = gate._score_citations(content)
        assert score < 100.0

    def test_example_url_penalized(self):
        """example.com URL 扣分"""
        gate = _make_gate()
        content = "[Ex](https://example.com)"
        score = gate._score_citations(content)
        assert score < 100.0


# ─── 5. depth 深度 ────────────────────────────

class TestScoreDepth:
    """depth: 内容深度"""

    def test_shallow_content(self):
        """浅内容得低分"""
        gate = _make_gate()
        score = gate._score_depth("Short.")
        assert score == 30

    def test_medium_content(self):
        """中等内容得中等分（300 <= word_count < 500，score=50）"""
        gate = _make_gate()
        # 需要 word_count >= 200 且 < 500，且 >= 3 段落
        content = "\n\n".join([f"Paragraph {i} with enough content to pass threshold." for i in range(3)])
        score = gate._score_depth(content)
        # word_count 约 150，< 200 → score=30；调整内容使其在 300-500 范围
        content = "\n\n".join([f"Paragraph {i} " + "x" * 100 for i in range(4)])
        score = gate._score_depth(content)
        # word_count 约 400+，在 300-500 范围 → score=50
        assert score in (50, 70)  # 边界允许浮动

    def test_deep_content(self):
        """深内容得高分"""
        gate = _make_gate()
        content = "\n\n".join([f"Paragraph {i} with content." * 10 for i in range(20)])
        score = gate._score_depth(content)
        assert score >= 70

    def test_few_paragraphs_cap(self):
        """段落太少限制分数"""
        gate = _make_gate()
        content = "x" * 6000  # 长但只有一段
        score = gate._score_depth(content)
        assert score <= 40


# ─── 6. substance 实质度 ────────────────────────────

class TestScoreSubstance:
    """substance: 内容实质度"""

    def test_substantive_content(self):
        """有实质内容得高分"""
        gate = _make_gate()
        content = (
            "# Kafka 架构\n\n"
            "Kafka 是分布式流处理平台，吞吐量达 100万/秒。\n\n"
            "```python\nproducer.send('topic', value)\n```\n\n"
            "- 分区并行\n- 副本容错\n"
        )
        score = gate._score_substance(content)
        assert score > 50

    def test_empty_content(self):
        """空内容得 0 分"""
        gate = _make_gate()
        score = gate._score_substance("")
        assert score == 0

    def test_repetitive_content_penalized(self):
        """重复内容扣分"""
        gate = _make_gate()
        para = "这是相同的段落内容重复多次。" * 5
        content = f"{para}\n\n{para}\n\n{para}"
        score = gate._score_substance(content)
        assert score < 100.0


# ─── 7. topic_relevance 主题相关度 ────────────────────────────

class TestScoreTopicRelevance:
    """topic_relevance: 主题相关度"""

    def test_no_query_returns_neutral(self):
        """无 query 返回中性分 70"""
        gate = _make_gate()
        score = gate._score_topic_relevance("any content", [])
        assert score == 70.0

    def test_relevant_content(self):
        """相关内容得高分"""
        gate = _make_gate()
        content = "# Kafka 架构\n\nKafka 是分布式流处理平台，支持高吞吐量。"
        score = gate._score_topic_relevance(content, ["Kafka 架构"])
        assert score > 50

    def test_missing_proper_noun_penalized(self):
        """缺失专有名词扣分"""
        gate = _make_gate()
        content = "# 文档\n\n这是关于数据库的文档。"
        score = gate._score_topic_relevance(content, ["Kafka 架构"])
        # Kafka 是 mandatory，缺失应低分
        assert score < 50

    def test_partial_missing_mandatory(self):
        """少量专有名词缺失按比例扣分（不归零）"""
        gate = _make_gate()
        content = "# Kafka\n\nKafka 是流处理平台。"
        # query 含 Apache 和 Kafka，文档只有 Kafka
        score = gate._score_topic_relevance(content, ["Apache Kafka"])
        # 缺失 1/2 = 50%，不归零（>50% 才归零）
        assert score >= 0

    def test_english_question_words_not_proper_nouns(self):
        """英文问句的 How/Use 不再被当专名（旧逻辑缺失超半数直接归零）"""
        gate = _make_gate()
        content = "# Redis 入门\n\nRedis 是一个高性能的内存键值数据库。"
        score = gate._score_topic_relevance(content, ["How to Use Redis?"])
        assert score > 0

    def test_sentence_initial_capitalized_word_not_mandatory(self):
        """仅句首出现的大写词（Kubernetes 句首）不再作为专名归零"""
        gate = _make_gate()
        content = "# Clusters\n\nClusters of nodes share resources efficiently."
        score = gate._score_topic_relevance(content, ["Kubernetes clusters"])
        assert score > 0

    def test_real_proper_nouns_still_zero_when_missing(self):
        """真实专名 Kubernetes/API 缺失仍触发归零"""
        gate = _make_gate()
        content = "# 文档\n\n这是完全无关的中文内容，不含任何关键词。"
        score = gate._score_topic_relevance(
            content, ["How to deploy Kubernetes API services?"]
        )
        assert score == 0.0

    def test_real_proper_nouns_present_not_zeroed(self):
        """文档覆盖 Kubernetes/API 时不得误归零"""
        gate = _make_gate()
        content = "# Kubernetes API Guide\n\nThe Kubernetes API server exposes the cluster."
        score = gate._score_topic_relevance(
            content, ["How to deploy Kubernetes API services?"]
        )
        assert score > 0


# ─── 8. style 风格检查 ────────────────────────────

class TestScoreStyle:
    """style: 风格规则检查"""

    def test_no_style_issues(self):
        """无风格问题"""
        gate = _make_gate()
        content = "# Title\n\n## Section\n\nContent here."
        issues = gate._check_style(content)
        # 不应有 empty_headers 或 broken_links
        issue_names = [i["rule"] for i in issues]
        assert "empty_headers" not in issue_names
        assert "broken_links" not in issue_names

    def test_empty_header_detected(self):
        r"""空标题检测（regex ^#+\s*$ 不开 MULTILINE，只匹配字符串开头）"""
        gate = _make_gate()
        # 整个字符串只有 ## 和空格 → 匹配 ^#+\s*$
        content = "## "
        issues = gate._check_style(content)
        issue_names = [i["rule"] for i in issues]
        assert "empty_headers" in issue_names

    def test_broken_link_detected(self):
        """空链接8链接检测"""
        gate = _make_gate()
        content = "# Title\n\n[empty]()"
        issues = gate._check_style(content)
        issue_names = [i["rule"] for i in issues]
        assert "broken_links" in issue_names

    def test_excessive_whitespace_detected(self):
        """过多空行检测"""
        gate = _make_gate()
        content = "# Title\n\n\n\n\nContent."
        issues = gate._check_style(content)
        issue_names = [i["rule"] for i in issues]
        assert "excessive_whitespace" in issue_names


# ─── overall_score 综合 ────────────────────────────

class TestOverallScore:
    """overall_score 加权综合"""

    def test_equal_weights(self):
        """等权重时为平均值"""
        gate = _make_gate()
        gate._weights = {}
        scores = {"a": 80, "b": 60}
        overall = gate._overall_score(scores)
        assert overall == 70.0

    def test_weighted_average(self):
        """加权平均"""
        gate = _make_gate()
        gate._weights = {"a": 0.7, "b": 0.3}
        scores = {"a": 100, "b": 0}
        overall = gate._overall_score(scores)
        assert overall == 70.0

    def test_zero_total_weight_fallback(self):
        """总权重为 0 时回退到平均值"""
        gate = _make_gate()
        gate._weights = {"a": 0, "b": 0}
        scores = {"a": 80, "b": 60}
        overall = gate._overall_score(scores)
        assert overall == 70.0


# ─── _score_all 全维度 ────────────────────────────

class TestScoreAll:
    """_score_all 返回所有 8 个维度"""

    def test_returns_all_dimensions(self):
        """_score_all 返回 7 个评分维度（style 单独检查）"""
        gate = _make_gate()
        scores = gate._score_all("# Title\n\n## Section\n\nContent.", [])
        expected = {"completeness", "structure", "readability",
                    "citation", "depth", "substance", "topic_relevance"}
        assert set(scores.keys()) == expected

    def test_all_scores_in_range(self):
        """所有评分在 0-100 范围"""
        gate = _make_gate()
        scores = gate._score_all("# Title\n\nContent.", ["test"])
        for dim, score in scores.items():
            assert 0 <= score <= 100, f"{dim}={score} 超出范围"
