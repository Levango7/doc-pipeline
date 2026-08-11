"""ConvertAscii — ASCII 转换

测试原则：
  - 每个测试方法聚焦一个行为
  - 不依赖外部文件
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.convert_ascii import (
    AsciiBlock,
    AsciiConverter,
)

# ─── AsciiBlock 数据类 ────────────────────────────

class TestAsciiBlock:
    """AsciiBlock 数据类"""

    def test_to_dict(self):
        """to_dict 序列化"""
        block = AsciiBlock(
            block_type="box_drawing",
            start_line=0, end_line=3,
            content="+---+\n| a |\n+---+",
            confidence=0.8,
            is_fragment=False,
        )
        d = block.to_dict()
        assert d["type"] == "box_drawing"
        assert d["start"] == 0
        assert d["end"] == 3
        assert d["confidence"] == 0.8

    def test_to_dict_truncates_long_content(self):
        """长内容截断"""
        long_content = "x" * 300
        block = AsciiBlock(
            block_type="test", start_line=0, end_line=1,
            content=long_content,
        )
        d = block.to_dict()
        assert "..." in d["content"]
        assert len(d["content"]) < 300


# ─── 类型检测 ────────────────────────────

class TestDetectType:
    """detect_type 类型识别"""

    def test_empty_lines(self):
        """空行返回 unknown"""
        conv = AsciiConverter()
        result = conv.detect_type([])
        assert result[0] == "unknown"
        assert result[1] == 0.0

    def test_box_drawing_unicode(self):
        """Unicode 盒子图识别"""
        conv = AsciiConverter()
        lines = ["┌───┐", "│ A │", "└───┘"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "box_drawing"
        assert confidence > 0

    def test_box_drawing_ascii(self):
        """ASCII 盒子图识别（+---+ 风格也可能被识别为 table）"""
        conv = AsciiConverter()
        lines = ["+---+", "| A |", "+---+"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        # +---+ 风格可能被识别为 box_drawing 或 table，都含置信度
        assert block_type in ("box_drawing", "table")
        assert confidence > 0

    def test_tree(self):
        """树形图识别"""
        conv = AsciiConverter()
        lines = ["├── branch1", "└── branch2"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "tree"

    def test_table(self):
        """表格识别"""
        conv = AsciiConverter()
        lines = ["+---+---+", "| a | b |", "+---+---+"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "table"

    def test_flowchart_with_arrows(self):
        """流程图识别（Unicode 箭头）"""
        conv = AsciiConverter()
        lines = ["A → B", "B → C"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "flowchart"

    def test_flowchart_with_ascii_arrows(self):
        """流程图识别（ASCII 箭头）"""
        conv = AsciiConverter()
        lines = ["A -> B", "B -> C"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "flowchart"

    def test_banner(self):
        """横幅识别"""
        conv = AsciiConverter()
        lines = ["#####", "#####", "#####"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "banner"

    def test_code_block(self):
        """代码块识别"""
        conv = AsciiConverter()
        lines = ["    line1", "    line2", "    line3"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "code_block"

    def test_unknown(self):
        """普通文本返回 unknown"""
        conv = AsciiConverter()
        lines = ["This is just", "regular text"]
        block_type, confidence, is_fragment = conv.detect_type(lines)
        assert block_type == "unknown"


# ─── 转换 ────────────────────────────

class TestConvert:
    """convert 转换功能"""

    def test_convert_box_unicode(self):
        """Unicode 盒子保留在代码块"""
        conv = AsciiConverter()
        block = AsciiBlock("box_drawing", 0, 2, "┌───┐\n│ A │\n└───┘")
        result = conv.convert(block)
        assert "```" in result
        assert "┌───┐" in result

    def test_convert_box_ascii_to_unicode(self):
        """ASCII 盒子转 Unicode"""
        conv = AsciiConverter()
        block = AsciiBlock("box_drawing", 0, 2, "+---+\n| A |\n+---+")
        result = conv.convert(block)
        assert "```" in result
        # 应转换为 Unicode 盒子字符
        assert "─" in result or "│" in result

    def test_convert_table_to_markdown(self):
        """ASCII 表格转 Markdown 表格"""
        conv = AsciiConverter()
        content = "+---+---+\n| a | b |\n+---+---+\n| 1 | 2 |\n+---+---+"
        block = AsciiBlock("table", 0, 4, content)
        result = conv.convert(block)
        assert "|" in result
        assert "a" in result
        assert "b" in result

    def test_convert_flowchart_to_mermaid(self):
        """流程图转 Mermaid"""
        conv = AsciiConverter()
        block = AsciiBlock("flowchart", 0, 0, "A → B → C")
        result = conv.convert(block)
        assert "mermaid" in result
        assert "graph" in result

    def test_convert_tree_wrapped(self):
        """树形图包裹代码块"""
        conv = AsciiConverter()
        block = AsciiBlock("tree", 0, 1, "├── a\n└── b")
        result = conv.convert(block)
        assert "```" in result

    def test_convert_unknown_preserved(self):
        """未知类型保留原样"""
        conv = AsciiConverter()
        block = AsciiBlock("unknown", 0, 0, "some content")
        result = conv.convert(block)
        assert result == "some content"


# ─── detect_blocks ────────────────────────────

class TestDetectBlocks:
    """detect_blocks 块检测"""

    def test_no_blocks(self):
        """无 ASCII 图时返回空列表"""
        conv = AsciiConverter()
        blocks = conv.detect_blocks("Just regular text.\nAnother line.")
        assert blocks == []

    def test_detects_box(self):
        """检测盒子图"""
        conv = AsciiConverter()
        text = "┌───┐\n│ A │\n└───┘"
        blocks = conv.detect_blocks(text)
        types = [b.block_type for b in blocks]
        assert "box_drawing" in types, f"expected box_drawing in {types}"

    def test_detects_multiple_blocks(self):
        """检测多个块"""
        conv = AsciiConverter()
        text = """┌───┐
│ A │
└───┘

Some text.

+---+
| B |
+---+"""
        blocks = conv.detect_blocks(text)
        assert len(blocks) >= 1

    def test_min_lines_filter(self):
        """少于 min_lines 的块被过滤"""
        conv = AsciiConverter(min_lines=5)
        text = "┌───┐\n│ A │\n└───┘"  # 只有 3 行
        blocks = conv.detect_blocks(text)
        assert blocks == []



# ─── detect_and_convert ────────────────────────────

class TestDetectAndConvert:
    """detect_and_convert 主入口"""

    def test_no_change_for_plain_text(self):
        """普通文本不变"""
        conv = AsciiConverter()
        text = "Just plain text.\nNo ASCII art here."
        assert conv.detect_and_convert(text) == text

    def test_converts_box(self):
        """转换盒子图"""
        conv = AsciiConverter()
        text = "┌───┐\n│ A │\n└───┘"
        result = conv.detect_and_convert(text)
        assert "```" in result

    def test_preserves_non_art(self):
        """非 ASCII 图部分保留"""
        conv = AsciiConverter()
        text = "Before\n\n┌───┐\n│ A │\n└───┘\n\nAfter"
        result = conv.detect_and_convert(text)
        assert "Before" in result
        assert "After" in result


# ─── scan ────────────────────────────

class TestScan:
    """scan 扫描接口"""

    def test_scan_returns_dicts(self):
        """scan 返回字典列表"""
        conv = AsciiConverter()
        text = "┌───┐\n│ A │\n└───┘"
        result = conv.scan(text)
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)
            assert "type" in item
            assert "start" in item
            assert "end" in item

    def test_scan_empty_text(self):
        """空文本返回空列表"""
        conv = AsciiConverter()
        assert conv.scan("") == []
