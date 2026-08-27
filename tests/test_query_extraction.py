"""_extract_queries 查询词提取行为测试

背景（修复 A2）：原实现在输入全部命中噪音模式时静默回退到硬编码默认主题
"Python 异步编程"，导致 API/MCP 提交的含"请生成/帮我写"的真实主题被丢弃，
生成一篇与主题无关的文档且无任何警告。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.cache_manager import CacheManager
from pipeline_core.dag_executor import DAGExecutor


def _make_executor() -> DAGExecutor:
    """绕过完整初始化，仅装配 _extract_queries 依赖的 _query_cache"""
    ex = DAGExecutor.__new__(DAGExecutor)
    ex._query_cache = CacheManager(name="test_queries", max_size=10, ttl=0)
    return ex


def _node(default_query=None):
    cfg = {"default_query": default_query} if default_query else {}
    return SimpleNamespace(agent_config=SimpleNamespace(config=cfg))


def _write(tmp_path, text: str) -> str:
    p = tmp_path / "input.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestExtractQueries:
    def test_topic_plus_noise_line(self, tmp_path):
        """主题行保留，测试性噪音行被过滤（对齐 test_input.md 形态）"""
        f = _write(tmp_path, "Kafka 核心架构解析\n这是一个测试，用于验证流水线是否正常工作。\n")
        assert _make_executor()._extract_queries(f, _node()) == ["Kafka 核心架构解析"]

    def test_pure_polite_query_kept(self, tmp_path):
        """仅含"请生成/帮我写"的输入：这就是用户的主题，必须原样保留（修复点）"""
        f = _write(tmp_path, "请生成一份介绍 Kafka 的文档\n")
        assert _make_executor()._extract_queries(f, _node()) == ["请生成一份介绍 Kafka 的文档"]

    def test_only_noise_lines_fallback_to_candidates(self, tmp_path):
        """全部行命中噪音模式时回退为候选行，而不是丢弃"""
        f = _write(tmp_path, "这是一个测试\n帮我写个东西验证一下\n")
        assert _make_executor()._extract_queries(f, _node()) == \
            ["这是一个测试", "帮我写个东西验证一下"]

    def test_empty_input_raises_without_default(self, tmp_path):
        """空输入且未配置 default_query：必须显式失败，不得静默造文档"""
        f = _write(tmp_path, "# 只有标题\n\n")
        with pytest.raises(ValueError, match="未提取到任何有效检索词"):
            _make_executor()._extract_queries(f, _node())

    def test_empty_input_uses_configured_default(self, tmp_path):
        """显式配置的 default_query 仍然生效"""
        f = _write(tmp_path, "")
        got = _make_executor()._extract_queries(f, _node(default_query="自定义兜底主题"))
        assert got == ["自定义兜底主题"]

    def test_short_and_comment_lines_skipped(self, tmp_path):
        f = _write(tmp_path, "# 注释行\nab\n有效主题行\n")
        assert _make_executor()._extract_queries(f, _node()) == ["有效主题行"]

    def test_result_cached_per_file(self, tmp_path):
        f = _write(tmp_path, "缓存主题\n")
        ex = _make_executor()
        assert ex._extract_queries(f, _node()) == ["缓存主题"]
        f2 = Path(f)
        f2.write_text("换了内容\n", encoding="utf-8")
        assert ex._extract_queries(f, _node()) == ["缓存主题"]  # 命中缓存
