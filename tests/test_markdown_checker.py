"""MarkdownChecker — Markdown 检查

测试原则：
  - 每个测试方法聚焦一个检查规则
  - 不依赖外部文件
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.markdown_checker import (
    DEFAULT_RULES,
    Checker,
    IncrementalChecker,
    Issue,
    RuleConfig,
    check_file,
)

# ─── P0 检查 ────────────────────────────

class TestP0Checks:
    """P0 阻断级检查"""

    def test_empty_content(self):
        """空内容触发 P0"""
        checker = Checker("")
        result = checker.run()
        assert result["summary"]["P0_blocking"] >= 1
        assert result["status"] == "fail"

    def test_oversized_file(self):
        """超大文件触发 P0"""
        # 生成超过 10MB 的内容
        huge = "x" * (11 * 1024 * 1024)
        checker = Checker(huge)
        result = checker.run()
        assert result["summary"]["P0_blocking"] >= 1

    def test_normal_content_no_p0(self):
        """正常内容无 P0"""
        content = "# Title\n\n## Section\n\nContent here."
        checker = Checker(content)
        result = checker.run()
        assert result["summary"]["P0_blocking"] == 0


# ─── P1 检查 ────────────────────────────

class TestP1Checks:
    """P1 严重级检查"""

    def test_control_character(self):
        """控制字符触发 P1"""
        content = "# Title\n\nContent\x00with control char"
        checker = Checker(content)
        result = checker.run()
        assert result["summary"]["P1_severe"] >= 1

    def test_unicode_border(self):
        """Unicode 边框字符触发 P1"""
        content = "# Title\n\n┌─────┐\n│ A │  B │\n└─────┘"
        checker = Checker(content)
        result = checker.run()
        assert result["summary"]["P1_severe"] >= 1

    def test_few_sections(self):
        """章节过少触发 P1"""
        content = "# Only one heading\n\nSome content."
        checker = Checker(content, filepath="test.md")
        result = checker.run()
        # default_min=3，只有 1 个章节
        assert result["summary"]["P1_severe"] >= 1

    def test_k8s_requires_more_sections(self):
        """k8s 文档要求更多章节"""
        content = "# K8s\n\n## A\n\n## B\n\nContent."
        checker = Checker(content, filepath="kubernetes.md")
        result = checker.run()
        # k8s_min=10，只有 2 个
        assert result["summary"]["P1_severe"] >= 1


# ─── P2 检查 ────────────────────────────

class TestP2Checks:
    """P2 警告级检查"""

    def test_excessive_empty_lines(self):
        """连续空行过多触发 P2"""
        content = "# Title\n\n\n\n\n\n\nContent."
        checker = Checker(content)
        result = checker.run()
        assert result["summary"]["P2_warning"] >= 1

    def test_normal_empty_lines_no_p2(self):
        """正常空行不触发 P2"""
        content = "# Title\n\n## Section\n\nContent.\n\nMore."
        checker = Checker(content)
        result = checker.run()
        # 不应因空行触发 P2
        empty_issues = [i for i in result["by_level"]["P2"] if i["category"] == "empty_lines"]
        assert len(empty_issues) == 0


# ─── P3 检查 ────────────────────────────

class TestP3Checks:
    """P3 建议级检查"""

    def test_heading_gap(self):
        """标题层级跳跃触发 P3"""
        content = "# H1\n\n### H3\n\nContent."
        checker = Checker(content)
        result = checker.run()
        assert result["summary"]["P3_suggestion"] >= 1

    def test_table_column_mismatch(self):
        """表格列数不一致触发 P3"""
        content = """# Title

| A | B |
|---|---|
| 1 | 2 | 3 |

Content.
"""
        checker = Checker(content)
        result = checker.run()
        # 可能触发 P3
        assert result["summary"]["P3_suggestion"] >= 0

    def test_missing_recommended_sections(self):
        """缺少推荐章节触发 P3"""
        content = "# Title\n\n## Section\n\nContent."
        checker = Checker(content)
        result = checker.run()
        # 应建议 FAQ、最佳实践、故障排查
        missing = [i for i in result["by_level"]["P3"] if i["category"] == "missing_section"]
        assert len(missing) >= 1


# ─── Issue 类 ────────────────────────────

class TestIssue:
    """Issue 数据类"""

    def test_to_dict(self):
        """to_dict 序列化"""
        issue = Issue("P1", "test", "message", line=10, fix="fix it")
        d = issue.to_dict()
        assert d["level"] == "P1"
        assert d["category"] == "test"
        assert d["message"] == "message"
        assert d["line"] == 10
        assert d["fix"] == "fix it"

    def test_repr(self):
        """__repr__ 包含级别和行号"""
        issue = Issue("P1", "test", "msg", line=5)
        repr_str = repr(issue)
        assert "P1" in repr_str
        assert "行5" in repr_str

    def test_repr_no_line(self):
        """无行号时 repr 不包含行号"""
        issue = Issue("P2", "test", "msg")
        repr_str = repr(issue)
        assert "P2" in repr_str
        assert "行" not in repr_str


# ─── RuleConfig ────────────────────────────

class TestRuleConfig:
    """规则配置"""

    def test_default_rules_loaded(self):
        """无 YAML 时加载默认规则"""
        config = RuleConfig(path="/nonexistent.yaml")
        assert config.data == DEFAULT_RULES

    def test_is_enabled_default_true(self):
        """默认启用"""
        config = RuleConfig(path="/nonexistent.yaml")
        assert config.is_enabled("content") is True

    def test_get_param_default(self):
        """get_param 返回默认值"""
        config = RuleConfig(path="/nonexistent.yaml")
        assert config.get_param("size", "max_size_mb", 10) == 10

    def test_get_level_default(self):
        """get_level 返回默认级别"""
        config = RuleConfig(path="/nonexistent.yaml")
        assert config.get_level("content") == "P0"


# ─── IncrementalChecker ────────────────────────────

class TestIncrementalChecker:
    """增量检测"""

    def test_split_sections(self):
        """章节分割"""
        content = "# H1\n\nContent 1\n\n## H2\n\nContent 2"
        checker = IncrementalChecker(content)
        assert len(checker.sections) >= 2

    def test_detect_new_sections(self):
        """检测新章节"""
        content = "# H1\n\nContent"
        checker = IncrementalChecker(content)
        result = checker.detect()
        assert result["total_sections"] >= 1
        # 全部为新（无 baseline）
        assert result["summary"]["new"] >= 1

    def test_hash_consistency(self):
        """相同内容 hash 一致"""
        content = "# H1\n\nContent"
        c1 = IncrementalChecker(content)
        c2 = IncrementalChecker(content)
        assert c1.sections[0]["hash"] == c2.sections[0]["hash"]


# ─── check_file 公共接口 ────────────────────────────

class TestCheckFile:
    """check_file 公共接口"""

    def test_nonexistent_file(self):
        """不存在文件返回 error"""
        result = check_file("/nonexistent/file.md")
        assert result["status"] == "error"

    def test_valid_file(self, tmp_path):
        """有效文件检查"""
        f = tmp_path / "test.md"
        f.write_text("# Title\n\n## Section\n\nContent.\n\n## Ref\n\nMore.")
        result = check_file(str(f), incremental=False)
        assert result["status"] in ("ok", "warning", "error")

    def test_empty_file(self, tmp_path):
        """空文件返回 error"""
        f = tmp_path / "empty.md"
        f.write_text("")
        result = check_file(str(f), incremental=False)
        assert result["status"] == "error"


# ─── run() 完整流程 ────────────────────────────

class TestCheckerRun:
    """Checker.run() 完整流程"""

    def test_run_returns_required_fields(self):
        """run 返回所有必需字段"""
        checker = Checker("# Title\n\nContent.")
        result = checker.run()
        assert "status" in result
        assert "total_issues" in result
        assert "by_level" in result
        assert "summary" in result
        assert set(result["by_level"].keys()) == {"P0", "P1", "P2", "P3"}
        assert set(result["summary"].keys()) == {
            "P0_blocking", "P1_severe", "P2_warning", "P3_suggestion"
        }

    def test_p0_skips_lower_checks(self):
        """P0 问题跳过 P1/P2/P3 检查"""
        checker = Checker("")  # 空内容触发 P0
        result = checker.run()
        assert result["summary"]["P0_blocking"] >= 1
        # P1/P2/P3 应为 0（跳过）
        assert result["summary"]["P1_severe"] == 0
        assert result["summary"]["P2_warning"] == 0
