"""FormatConverter — 格式转换

测试原则：
  - 用 mock 模拟外部命令（mmdc、pandoc）
  - 不实际调用网络 API
  - 每个测试方法聚焦一个行为
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.format_converter import FormatConverter

# ─── 工具检测 ────────────────────────────

class TestToolDetection:
    """mmdc / pandoc 工具检测"""

    def test_find_mmdc_not_found(self):
        """mmdc 不存在时返回 None"""
        with patch("subprocess.run", side_effect=Exception("not found")):
            conv = FormatConverter()
            assert conv._mmdc_path is None

    def test_find_mmdc_found(self):
        """mmdc 存在时返回命令"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            conv = FormatConverter()
            assert conv._mmdc_path is not None

    def test_find_pandoc_not_found(self):
        """pandoc 不存在时返回 None"""
        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "pandoc":
                raise Exception("not found")
            mock_result = MagicMock()
            mock_result.returncode = 0
            return mock_result
        with patch("subprocess.run", side_effect=mock_run):
            conv = FormatConverter()
            assert conv._pandoc_path is None


# ─── Markdown → HTML ────────────────────────────

class TestMarkdownToHtml:
    """Markdown → HTML 转换"""

    def test_basic_conversion(self, tmp_path):
        """基本 Markdown 转 HTML"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title\n\nThis is a paragraph.")
        html_file = tmp_path / "test.html"

        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file), str(html_file))

        assert "<html" in result
        assert "<h1>Title</h1>" in result
        assert "<p>This is a paragraph.</p>" in result
        assert html_file.exists()

    def test_heading_levels(self, tmp_path):
        """各级标题"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# H1\n\n## H2\n\n### H3")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<h1>H1</h1>" in result
        assert "<h2>H2</h2>" in result
        assert "<h3>H3</h3>" in result

    def test_code_block(self, tmp_path):
        """代码块"""
        md_file = tmp_path / "test.md"
        md_file.write_text("```python\nprint('hello')\n```")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<pre>" in result
        assert "<code" in result
        assert "python" in result

    def test_table(self, tmp_path):
        """表格"""
        md_file = tmp_path / "test.md"
        md_file.write_text("| A | B |\n|---|---|\n| 1 | 2 |")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<table>" in result
        assert "<th>A</th>" in result
        assert "<td>1</td>" in result

    def test_list_with_ul_wrapper(self, tmp_path):
        """列表用 <ul> 包裹（P1 修复验证）"""
        md_file = tmp_path / "test.md"
        md_file.write_text("- item 1\n- item 2\n- item 3")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<ul>" in result
        assert "</ul>" in result
        assert "<li>item 1</li>" in result

    def test_bold_italic(self, tmp_path):
        """粗体斜体"""
        md_file = tmp_path / "test.md"
        md_file.write_text("**bold** and *italic*")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<strong>bold</strong>" in result
        assert "<em>italic</em>" in result

    def test_link(self, tmp_path):
        """链接"""
        md_file = tmp_path / "test.md"
        md_file.write_text("[text](http://example.com)")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))

        assert '<a href="http://example.com">text</a>' in result

    def test_blockquote(self, tmp_path):
        """引用"""
        md_file = tmp_path / "test.md"
        md_file.write_text("> quoted text")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<blockquote>" in result
        assert "quoted text" in result

    def test_hr(self, tmp_path):
        """分隔线"""
        md_file = tmp_path / "test.md"
        md_file.write_text("---")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "<hr>" in result

    def test_html_escape_in_code(self, tmp_path):
        """代码块内 HTML 转义"""
        md_file = tmp_path / "test.md"
        md_file.write_text("```\n<div>raw html</div>\n```")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert "&lt;div&gt;" in result

    def test_returns_html_string_when_no_path(self, tmp_path):
        """无 html_path 时返回字符串"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title")
        conv = FormatConverter()
        result = conv.markdown_to_html(str(md_file))
        assert isinstance(result, str)
        assert "<html" in result


# ─── Markdown → Word ────────────────────────────

class TestMarkdownToWord:
    """Markdown → Word 转换"""

    def test_pandoc_available(self, tmp_path):
        """pandoc 可用时使用 pandoc"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title\n\nContent")
        docx_file = tmp_path / "test.docx"

        conv = FormatConverter()
        conv._pandoc_path = "pandoc"  # 模拟 pandoc 可用

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = conv.markdown_to_word(str(md_file), str(docx_file))
        assert result == str(docx_file)

    def test_pandoc_fallback_to_python_docx(self, tmp_path):
        """pandoc 失败时回退到 python-docx"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title\n\nContent")
        docx_file = tmp_path / "test.docx"

        conv = FormatConverter()
        conv._pandoc_path = None  # pandoc 不可用

        # mock python-docx
        mock_doc = MagicMock()
        with patch.dict("sys.modules", {"docx": MagicMock(Document=MagicMock(return_value=mock_doc))}):
            conv.markdown_to_word(str(md_file), str(docx_file))
        # 应调用 add_heading 和 add_paragraph
        mock_doc.add_heading.assert_called()
        mock_doc.save.assert_called_with(str(docx_file))


# ─── Mermaid 渲染 ────────────────────────────

class TestMermaidRender:
    """Mermaid → PNG/SVG 渲染"""

    def test_mmdc_render_success(self, tmp_path):
        """mmdc 渲染成功"""
        conv = FormatConverter()
        conv._mmdc_path = "mmdc"

        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            output = tmp_path / "out.png"
            result = conv.mermaid_to_png("graph LR; A-->B", str(output))
        assert result is True

    def test_mmdc_render_failure(self, tmp_path):
        """mmdc 渲染失败"""
        conv = FormatConverter()
        conv._mmdc_path = "mmdc"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        with patch("subprocess.run", return_value=mock_result), \
                patch.object(conv, "_kroki_render", return_value=False), \
                patch.object(conv, "_mermaid_ink_render", return_value=False):
            result = conv.mermaid_to_png("graph LR; A-->B", str(tmp_path / "out.png"))
        assert result is False

    def test_no_render_tools_returns_false(self, tmp_path):
        """无渲染工具时返回 False"""
        conv = FormatConverter()
        conv._mmdc_path = None
        with patch.object(conv, "_kroki_render", return_value=False), \
                patch.object(conv, "_mermaid_ink_render", return_value=False):
            result = conv.mermaid_to_png("graph LR", str(tmp_path / "out.png"))
        assert result is False


# ─── render_mermaid_in_markdown ────────────────────────────

class TestRenderMermaidInMarkdown:
    """Markdown 中 Mermaid 图渲染"""

    def test_replaces_mermaid_blocks(self, tmp_path):
        """替换 Mermaid 代码块为图片引用"""
        md_file = tmp_path / "test.md"
        md_file.write_text("```mermaid\ngraph LR\nA-->B\n```\n\nText.")

        conv = FormatConverter()
        with patch.object(conv, "mermaid_to_png", return_value=True):
            conv.render_mermaid_in_markdown(str(md_file), str(tmp_path / "images"))

        content = md_file.read_text()
        assert "mermaid" not in content or "![mermaid" in content
        assert "Text." in content

    def test_preserves_failed_renders(self, tmp_path):
        """渲染失败保留原样"""
        md_file = tmp_path / "test.md"
        original = "```mermaid\ngraph LR\nA-->B\n```\n\nText."
        md_file.write_text(original)

        conv = FormatConverter()
        with patch.object(conv, "mermaid_to_png", return_value=False):
            conv.render_mermaid_in_markdown(str(md_file), str(tmp_path / "images"))

        content = md_file.read_text()
        assert "```mermaid" in content  # 保留原样


# ─── status ────────────────────────────

class TestStatus:
    """status 状态查询"""

    def test_status_returns_dict(self):
        """status 返回字典"""
        conv = FormatConverter()
        s = conv.status()
        assert isinstance(s, dict)
        assert "mmdc" in s
        assert "pandoc" in s
        assert "capabilities" in s

    def test_capabilities(self):
        """capabilities 包含所有能力"""
        conv = FormatConverter()
        s = conv.status()
        caps = s["capabilities"]
        assert "mermaid_to_png" in caps
        assert "mermaid_to_svg" in caps
        assert "markdown_to_html" in caps
        assert "markdown_to_word" in caps
