"""FetcherAgent 正文提取 — 代码块（<pre>）保真护栏。

背景：<pre> 原先与普通段落同等对待（separator=" " / \\s+→" "），
导致 `asyncio.run(main())` 被压成 `asyncio . run ( main ( ) )`，
代码不可读，且下游 docx/pdf 渲染无法还原排版。

本文件锁住修复后的行为：换行、缩进、围栏、语言标注、实体还原、代码去重。
两条提取路径（selectolax / 正则回退）都必须满足。
"""
import pytest

from agents.fetcher import (
    FetcherAgent,
    _extract_pre_code,
    _harvest_pre_blocks,
    _unescape_html,
)

CODE_SAMPLE = """import asyncio

async def main():
    # 缩进即语义，不能被压平
    tasks = [asyncio.create_task(work(i)) for i in range(3)]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())"""

HTML_WITH_CODE = (
    "<html><body><article>"
    "<p>下面这个示例演示了 Python 异步编程的基本用法，"
    "段落本身足够长以便通过密度筛选不被丢弃。</p>"
    f"<pre><code class=\"language-python\">{CODE_SAMPLE}</code></pre>"
    "<p>上面代码展示了 asyncio.gather 的并发用法，"
    "第二段正文同样足够长以通过密度筛选。</p>"
    "</article></body></html>"
)


@pytest.fixture
def agent(tmp_path):
    """绕开 __init__ 的目录/配置依赖，只测纯函数方法。"""
    return FetcherAgent.__new__(FetcherAgent)


# ────────────────────────────── 模块级辅助函数 ──────────────────────────────

class TestUnescapeHtml:
    def test_common_entities_decoded(self):
        assert _unescape_html("a&lt;b&amp;&gt;c") == "a<b&>c"

    def test_numeric_entities(self):
        assert _unescape_html("&#65;&#x42;") == "AB"

    def test_nbsp_becomes_space_not_stripped(self):
        assert _unescape_html("a&nbsp;b") == "a b"

    def test_residual_named_entity_removed(self):
        # 未覆盖的命名实体不应留在代码里
        assert "copy" not in _unescape_html("&copy; 2024")

    def test_invalid_numeric_entity_kept_verbatim(self):
        # 越界码点不应抛异常，保留原文
        assert _unescape_html("&#999999999;") == "&#999999999;"


class TestExtractPreCode:
    def test_preserves_newlines_and_indent(self):
        out = _extract_pre_code(CODE_SAMPLE)
        assert "    tasks = [asyncio.create_task" in out
        assert out.count("\n") >= 8

    def test_emits_markdown_fence_with_lang(self):
        out = _extract_pre_code(
            '<code class="language-python">x=1</code>'
        )
        assert out.startswith("```python")

    def test_lang_sanitized(self):
        out = _extract_pre_code('<code class="language-c++">int a;</code>')
        assert "```c++" in out

    def test_no_lang_defaults_plain_fence(self):
        out = _extract_pre_code("plain code here")
        assert out.startswith("```\n")

    def test_br_tag_becomes_newline(self):
        out = _extract_pre_code("line1<br>line2<br/>line3")
        assert "line1\nline2\nline3" in out

    def test_inner_span_tags_removed(self):
        out = _extract_pre_code(
            '<span class="k">def</span> <span class="nf">f</span>:'
        )
        assert "def" in out and "<span" not in out

    def test_trailing_ws_stripped_but_indent_kept(self):
        out = _extract_pre_code("if x:\n    pass   \n")
        assert "    pass\n" in out

    def test_entities_restored_in_code(self):
        out = _extract_pre_code("if a&lt;b and b&gt;c:")
        assert "if a<b and b>c:" in out

    def test_crlf_normalized(self):
        out = _extract_pre_code("a\r\nb\r\nc")
        assert "\r" not in out

    def test_embedded_fence_gets_longer_fence(self):
        # 代码自身含 ``` 围栏 → 外层围栏必须加长，否则提前闭合
        out = _extract_pre_code("intro\n```python\nx=1\n```")
        assert out.startswith("````")
        assert out.rstrip().endswith("````")

    def test_empty_pre_returns_empty(self):
        assert _extract_pre_code("   \n  ") == ""

    def test_whitespace_only_after_tags(self):
        assert _extract_pre_code("<span> </span>") == ""


class TestHarvestPreBlocks:
    def test_extracts_and_removes(self):
        code, rest = _harvest_pre_blocks(
            f"<p>before</p><pre>{CODE_SAMPLE}</pre><p>after</p>"
        )
        assert "    tasks = [asyncio" in code
        assert "<pre>" not in rest

    def test_duplicate_code_deduped(self):
        code, _ = _harvest_pre_blocks(
            f"<pre>{CODE_SAMPLE}</pre><pre>{CODE_SAMPLE}</pre>"
        )
        assert code.count("asyncio.gather") == 1

    def test_no_pre_returns_original_html(self):
        html = "<p>nothing here</p>"
        code, rest = _harvest_pre_blocks(html)
        assert code == ""
        assert rest == html

    def test_multiple_blocks_joined(self):
        code, _ = _harvest_pre_blocks("<pre>a=1</pre><pre>b=2</pre>")
        assert "a=1" in code and "b=2" in code

    def test_adjacent_text_not_glued(self):
        # 摘除处必须留空，否则 "before" + "after" 会粘成一个词
        _, rest = _harvest_pre_blocks("<p>before</p><pre>x</pre><p>after</p>")
        assert "beforeafter" not in rest


# ───────────────────────────两条提取路径的端到端护栏───────────────────────────

class TestExtractTextPreservesCode:
    @pytest.mark.parametrize("use_regex", [False, True], ids=["selectolax", "regex"])
    def test_code_keeps_newline_and_indent(self, agent, use_regex):
        text = (agent._extract_text_regex(HTML_WITH_CODE) if use_regex
                else agent._extract_text(HTML_WITH_CODE))
        # 样本里 tasks/await 在函数体内（4 空格），注释行同为 4 空格
        assert "\n    tasks = [asyncio.create_task" in text
        assert "\n    # 缩进即语义，不能被压平" in text

    @pytest.mark.parametrize("use_regex", [False, True], ids=["selectolax", "regex"])
    def test_code_wrapped_in_markdown_fence(self, agent, use_regex):
        text = (agent._extract_text_regex(HTML_WITH_CODE) if use_regex
                else agent._extract_text(HTML_WITH_CODE))
        assert "```python" in text
        assert text.count("```") >= 2

    @pytest.mark.parametrize("use_regex", [False, True], ids=["selectolax", "regex"])
    def test_surrounding_prose_preserved(self, agent, use_regex):
        text = (agent._extract_text_regex(HTML_WITH_CODE) if use_regex
                else agent._extract_text(HTML_WITH_CODE))
        assert "演示了 Python 异步编程" in text
        assert "asyncio.gather 的并发用法" in text

    @pytest.mark.parametrize("use_regex", [False, True], ids=["selectolax", "regex"])
    def test_code_not_collapsed_into_one_line(self, agent, use_regex):
        """回归护栏：修复前整段代码被压成 `asyncio . run ( main ( ) )`。"""
        text = (agent._extract_text_regex(HTML_WITH_CODE) if use_regex
                else agent._extract_text(HTML_WITH_CODE))
        assert "asyncio . run" not in text
        assert "main ( )" not in text

    def test_code_only_document_still_returned(self, agent):
        """整页只有代码块时（正文为空）也不能丢内容。"""
        html = f"<html><body><pre>{CODE_SAMPLE}</pre></body></html>"
        text = agent._extract_text(html)
        assert "asyncio.gather" in text
        assert "    tasks" in text

    def test_script_style_still_stripped(self, agent):
        """修复不得放宽噪音过滤。"""
        html = (
            "<html><body><article>"
            "<script>var evil=1;</script>"
            f"<pre>{CODE_SAMPLE}</pre>"
            "<p>正常正文段落，长度足够长以便通过密度筛选不被丢弃掉。</p>"
            "</article></body></html>"
        )
        text = agent._extract_text(html)
        assert "var evil" not in text
        assert "asyncio.gather" in text

    def test_code_text_reusable_across_paths(self, agent):
        """_extract_text 把已摘出的 code_text 传给正则回退，代码块不得出现两次。

        回归场景：早期实现「传入 code_text 就跳过摘除 <pre>」，
        导致 <pre> 残留 → 代码被压平后混进正文，与保真副本重复。
        注意样本正文里本身就含一次 "asyncio.gather" 字样，
        所以这里数的是代码块围栏的开合次数。
        """
        code, _ = _harvest_pre_blocks(HTML_WITH_CODE)
        text = agent._extract_text_regex(HTML_WITH_CODE, code_text=code)
        assert text.count("```python") == 1      # 只有一个代码块围栏
        assert "\n    tasks = [asyncio" in text  # 且是保真形态，未被压平
