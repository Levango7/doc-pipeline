"""LayoutOptimizer — 布局优化

测试原则：
  - 每个测试方法聚焦一个行为
  - 不依赖外部文件
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.layout_optimizer import (
    LayoutOptimizer,
    char_vis,
    fix_border_line,
    has_cjk_content,
    is_fence,
    is_md_table_sep,
    is_outer_border,
    leading_spaces,
    rebuild_border,
    vis_bytes,
    vis_str,
)

# ─── 视觉宽度计算 ────────────────────────────

class TestVisualWidth:
    """视觉宽度计算"""

    def test_ascii_char_width_1(self):
        """ASCII 字符宽度 1"""
        assert char_vis("a") == 1
        assert char_vis(" ") == 1
        assert char_vis("1") == 1

    def test_cjk_char_width_2(self):
        """CJK 字符宽度 2"""
        assert char_vis("中") == 2
        assert char_vis("文") == 2
        assert char_vis("字") == 2

    def test_fullwidth_char_width_2(self):
        """全角符号宽度 2"""
        assert char_vis("：") == 2  # 全角冒号
        assert char_vis("（") == 2  # 全角括号

    def test_vis_str_ascii(self):
        """ASCII 字符串宽度"""
        assert vis_str("hello") == 5
        assert vis_str("hello world") == 11

    def test_vis_str_mixed(self):
        """混合字符串宽度"""
        assert vis_str("abc中") == 5  # 3 + 2
        assert vis_str("中文") == 4   # 2 + 2

    def test_vis_bytes_ascii(self):
        """ASCII 字节流宽度"""
        assert vis_bytes(b"hello") == 5

    def test_vis_bytes_cjk(self):
        """CJK 字节流宽度"""
        assert vis_bytes("中文".encode()) == 4

    def test_vis_bytes_mixed(self):
        """混合字节流宽度"""
        assert vis_bytes("abc中".encode()) == 5

    def test_vis_bytes_empty(self):
        """空字节流宽度 0"""
        assert vis_bytes(b"") == 0


# ─── 辅助函数 ────────────────────────────

class TestHelperFunctions:
    """辅助判断函数"""

    def test_leading_spaces(self):
        """前导空格统计"""
        assert leading_spaces(b"   text") == 3
        assert leading_spaces(b"text") == 0
        assert leading_spaces(b"    ") == 4

    def test_is_fence(self):
        """代码栅栏识别"""
        assert is_fence(b"```python") is True
        assert is_fence(b"```") is True
        assert is_fence(b"  ```") is True
        assert is_fence(b"``") is False
        assert is_fence(b"text") is False

    def test_is_outer_border(self):
        """外框边框识别"""
        assert is_outer_border(b"+---+") is True
        assert is_outer_border(b"+----------+") is True
        assert is_outer_border(b"|---|") is False  # 含 | 不是外框
        assert is_outer_border(b"---") is False    # 无 +
        assert is_outer_border("+中文+".encode()) is False  # 含中文

    def test_has_cjk_content(self):
        """CJK 内容检测"""
        assert has_cjk_content("这是中文".encode()) is True
        assert has_cjk_content(b"english only") is False
        assert has_cjk_content(b"") is False
        assert has_cjk_content(b"---") is False  # 纯横线

    def test_is_md_table_sep(self):
        """Markdown 表格分隔行"""
        # regex: ^\|[:\-\s│]+\|$  要求内部只含 :- 空格 │
        assert is_md_table_sep("|:---:|") is True  # 单列分隔
        assert is_md_table_sep("|---|") is True     # 单列分隔
        assert is_md_table_sep("| a | b |") is False  # 含字母
        assert is_md_table_sep("---") is False       # 无 |


# ─── rebuild_border / fix_border_line ────────────────────────────

class TestBorderRebuild:
    """边框重建"""

    def test_rebuild_border(self):
        """重建边框到目标宽度"""
        result = rebuild_border("", 10)
        assert result == "+" + "-" * 8 + "+"
        assert vis_str(result) == 10

    def test_rebuild_border_min_width(self):
        """最小宽度"""
        result = rebuild_border("", 2)
        assert result == "+-+"

    def test_fix_border_line_no_change(self):
        """差值小不修复"""
        line = b"+------+\n"
        result = fix_border_line(line, 8)
        assert result == line  # 宽度已匹配

    def test_fix_border_line_widens(self):
        """差值大时加宽"""
        line = b"+--+\n"
        result = fix_border_line(line, 10)
        assert vis_bytes(result.strip()) == 10

    def test_fix_border_preserves_eol(self):
        """保留换行符"""
        line = b"+--+\r\n"
        result = fix_border_line(line, 10)
        assert result.endswith(b"\r\n")


# ─── LayoutOptimizer 主类 ────────────────────────────

class TestLayoutOptimizer:
    """LayoutOptimizer 主类"""

    def test_no_code_blocks(self):
        """无代码块时不修改"""
        content = "# Title\n\nSome text without code blocks."
        opt = LayoutOptimizer(content)
        result = opt.run()
        assert result["status"] == "ok"
        assert result["stats"]["borders_fixed"] == 0
        assert opt.get_result() == content

    def test_code_block_no_borders(self):
        """代码块无边框问题"""
        content = "```\nhello world\n```"
        opt = LayoutOptimizer(content)
        result = opt.run()
        assert result["stats"]["borders_fixed"] == 0

    def test_fix_narrow_border(self):
        """修复窄边框"""
        content = """```
+--+
│ 中文内容测试 │
+--+
```"""
        opt = LayoutOptimizer(content)
        result = opt.run()
        # 应检测到边框需修复
        assert result["status"] == "ok"

    def test_preserves_non_code_blocks(self):
        """不修改代码块外的内容"""
        content = "+--+\nOutside code block\n+--+"
        opt = LayoutOptimizer(content)
        result = opt.run()
        assert result["stats"]["borders_fixed"] == 0

    def test_stats_structure(self):
        """统计字段完整"""
        opt = LayoutOptimizer("# Title")
        result = opt.run()
        stats = result["stats"]
        assert "blocks_scanned" in stats
        assert "borders_fixed" in stats
        assert "lines_examined" in stats
        assert "block_details" in stats

    def test_multiple_code_blocks(self):
        """多个代码块"""
        content = """```
+--+
│ 内容 │
+--+
```

Some text.

```
another block
```"""
        opt = LayoutOptimizer(content)
        result = opt.run()
        assert result["stats"]["blocks_scanned"] >= 1

    def test_idempotent(self):
        """幂等：已优化的内容再次优化无变化"""
        content = "```\n+------+\n│ test │\n+------+\n```"
        opt1 = LayoutOptimizer(content)
        opt1.run()
        result1 = opt1.get_result()
        opt2 = LayoutOptimizer(result1)
        opt2.run()
        result2 = opt2.get_result()
        assert result1 == result2
