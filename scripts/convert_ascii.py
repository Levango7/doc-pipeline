"""
Convert ASCII — ASCII 图识别与转换
==================================
核心特性：
  - 7 种 ASCII 图分类识别
  - 碎片检测（不完整 ASCII 图）
  - 转换为 Unicode 等价物或 Markdown 格式
  - 保留原始格式选项

7 种 ASCII 图类型：
  1. box_drawing   — 盒子/边框图（┌─┐│└┘ 等）
  2. tree           — 树形图（├── └── 等）
  3. flowchart      — 流程图（→ ↓ ↑ ← + 方框）
  4. table          — 表格（+---+---+ 格式）
  5. diagram        — 示意图（混合字符画）
  6. banner         — 横幅/Logo（大字艺术）
  7. code_block     — 代码对齐块（缩进对齐的代码片段）

用法：
    from scripts.convert_ascii import AsciiConverter
    converter = AsciiConverter()
    result = converter.detect_and_convert(text)
"""
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AsciiBlock:
    """识别到的 ASCII 图块"""
    block_type: str          # 7 种类型之一
    start_line: int          # 起始行号（0-based）
    end_line: int            # 结束行号
    content: str             # 原始内容
    confidence: float = 0.0  # 置信度 0-1
    is_fragment: bool = False  # 是否为不完整碎片
    converted: str = ""      # 转换后的内容

    def to_dict(self) -> dict:
        return {
            "type": self.block_type,
            "start": self.start_line,
            "end": self.end_line,
            "confidence": self.confidence,
            "is_fragment": self.is_fragment,
            "content": self.content[:200] + "..." if len(self.content) > 200 else self.content,
        }


# ─── 类型检测模式 ────────────────────────────────

# Unicode 盒子绘制字符
BOX_CHARS = set("┌┐└┘├┤┬┴┼─│━┃┏┓┗┛┣┫┳┻╋═║")
# ASCII 盒子绘制字符（+/-/|）
ASCII_BOX_CHARS = set("+-|")
# 树形字符
TREE_CHARS = set("├└┤┴─│┃┌┐")
ASCII_TREE_PATTERN = re.compile(r"^[ \t]*[├└┌┐┤┴─│┃]*[├└][─━�s]*")
# 流程图箭头
ARROW_CHARS = set("→↓↑←↔↕⇒⇓⇑⇐")
ASCII_ARROW_PATTERN = re.compile(r"[-><]+|[\^v<>]")
# 表格分隔线
TABLE_LINE_PATTERN = re.compile(r"^\s*\+[-=+]+\+\s*$")
# 横幅字符（大写字母密集）
BANNER_PATTERN = re.compile(r"^[#*@=XHMWN8@$%&]+$")


class AsciiConverter:
    """ASCII 图识别与转换器"""

    def __init__(self, min_lines: int = 2, min_width: int = 5):
        self.min_lines = min_lines      # 最少行数才算 ASCII 图
        self.min_width = min_width      # 最少宽度

    def detect_type(self, lines: list[str]) -> tuple[str, float, bool]:
        """检测 ASCII 图类型

        Returns: (type, confidence, is_fragment)
        """
        if not lines:
            return ("unknown", 0.0, False)

        text = "\n".join(lines)
        all_chars = set(text) - set(" \t\n\r")

        # 1. Box Drawing
        box_count = sum(1 for c in all_chars if c in BOX_CHARS)
        if box_count >= 3 and box_count / max(len(all_chars), 1) > 0.3:
            confidence = min(box_count / 10, 1.0)
            is_fragment = self._check_box_fragment(lines)
            return ("box_drawing", confidence, is_fragment)

        # 2. Tree
        tree_lines = sum(1 for l in lines if ASCII_TREE_PATTERN.match(l))
        if tree_lines >= self.min_lines and tree_lines / max(len(lines), 1) > 0.5:
            confidence = min(tree_lines / 10, 1.0)
            is_fragment = self._check_tree_fragment(lines)
            return ("tree", confidence, is_fragment)

        # 3. Table
        table_lines = sum(1 for l in lines if TABLE_LINE_PATTERN.match(l))
        if table_lines >= 2:
            confidence = min(table_lines / 5, 1.0)
            is_fragment = table_lines < 3
            return ("table", confidence, is_fragment)

        # 4. Flowchart
        arrow_count = sum(1 for c in text if c in ARROW_CHARS)
        ascii_arrow_lines = sum(1 for l in lines if ASCII_ARROW_PATTERN.search(l))
        if arrow_count >= 2 or ascii_arrow_lines >= 2:
            confidence = min((arrow_count + ascii_arrow_lines) / 10, 1.0)
            is_fragment = arrow_count < 2
            return ("flowchart", confidence, is_fragment)

        # 5. ASCII Box (用 +-| 画的)
        ascii_box_count = sum(1 for c in all_chars if c in ASCII_BOX_CHARS)
        if ascii_box_count >= 5 and ascii_box_count / max(len(all_chars), 1) > 0.4:
            # 检查是否有 + 在行首/行尾（盒子角）
            corner_lines = sum(1 for l in lines if l.strip().startswith("+") and l.strip().endswith("+"))
            if corner_lines >= 2:
                confidence = min(corner_lines / 5, 1.0)
                return ("box_drawing", confidence, corner_lines < 2)

        # 6. Banner
        banner_lines = sum(1 for l in lines if BANNER_PATTERN.match(l.strip()))
        if banner_lines >= 3:
            confidence = min(banner_lines / 8, 1.0)
            return ("banner", confidence, banner_lines < 3)

        # 7. Code Block (缩进对齐)
        indented_lines = sum(1 for l in lines if l.startswith("    ") or l.startswith("\t"))
        if indented_lines >= self.min_lines and indented_lines / max(len(lines), 1) > 0.7:
            confidence = min(indented_lines / 10, 1.0)
            return ("code_block", confidence, False)

        # 8. Diagram (兜底：混合字符画)
        special_chars = all_chars - set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:!?\"'()-/")
        if len(special_chars) >= 3 and len(lines) >= self.min_lines:
            confidence = min(len(special_chars) / 15, 0.6)  # 较低置信度
            return ("diagram", confidence, len(lines) < 3)

        return ("unknown", 0.0, False)

    def _check_box_fragment(self, lines: list[str]) -> bool:
        """检查盒子图是否不完整"""
        first = lines[0].strip() if lines else ""
        last = lines[-1].strip() if lines else ""
        # 完整盒子应有上边界和下边界
        has_top = any(c in "┌┐┏┓+" for c in first)
        has_bottom = any(c in "└┘┗┛+" for c in last)
        return not (has_top and has_bottom)

    def _check_tree_fragment(self, lines: list[str]) -> bool:
        """检查树形图是否不完整"""
        # 完整树应有根节点和叶子节点
        has_root = any("└" in l or "├" in l for l in lines[:1])
        has_leaf = any("└" in l for l in lines[-2:])
        return not (has_root or has_leaf)

    # ─── 转换 ────────────────────────────────────

    def convert(self, block: AsciiBlock) -> str:
        """将 ASCII 图块转换为目标格式"""
        if block.block_type == "box_drawing":
            return self._convert_box(block.content)
        elif block.block_type == "tree":
            return self._convert_tree(block.content)
        elif block.block_type == "table":
            return self._convert_table(block.content)
        elif block.block_type == "flowchart":
            return self._convert_flowchart(block.content)
        elif block.block_type == "banner":
            return self._convert_banner(block.content)
        elif block.block_type == "code_block":
            return self._convert_code_block(block.content)
        elif block.block_type == "diagram":
            return self._convert_diagram(block.content)
        else:
            return block.content  # 未知类型，保留原样

    def _convert_box(self, content: str) -> str:
        """盒子图 → 保留原样（已是 Unicode 盒子字符）或包裹代码块"""
        # 如果已经是 Unicode 盒子字符，保留
        if any(c in content for c in BOX_CHARS):
            return f"```\n{content}\n```"
        # ASCII 盒子（+-|）→ 转 Unicode
        mapping = {"+": "┼", "-": "─", "|": "│"}
        result = content
        for old, new in mapping.items():
            result = result.replace(old, new)
        return f"```\n{result}\n```"

    def _convert_tree(self, content: str) -> str:
        """树形图 → 保留在代码块中"""
        return f"```\n{content}\n```"

    def _convert_table(self, content: str) -> str:
        """ASCII 表格 → Markdown 表格"""
        lines = content.strip().split("\n")
        rows = []
        for line in lines:
            if TABLE_LINE_PATTERN.match(line):
                continue  # 跳过分隔线
            # 按 | 分割
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells:
                rows.append(cells)

        if not rows:
            return f"```\n{content}\n```"

        # 转为 Markdown 表格
        result = []
        if rows:
            result.append("| " + " | ".join(rows[0]) + " |")
            result.append("| " + " | ".join("---" for _ in rows[0]) + " |")
            for row in rows[1:]:
                # 补齐列数
                while len(row) < len(rows[0]):
                    row.append("")
                result.append("| " + " | ".join(row) + " |")
        return "\n".join(result)

    def _convert_flowchart(self, content: str) -> str:
        """流程图 → Mermaid 语法（简单转换）或保留代码块"""
        # 简单检测是否有 → 箭头
        if "→" in content or "->" in content:
            # 尝试提取流程步骤
            steps = re.split(r"→|->", content)
            steps = [s.strip().strip("[](){}") for s in steps if s.strip()]
            if len(steps) >= 2:
                lines = ["```mermaid", "graph LR"]
                for i in range(len(steps) - 1):
                    lines.append(f"    {i}({steps[i]}) --> {i+1}({steps[i+1]})")
                lines.append("```")
                return "\n".join(lines)
        return f"```\n{content}\n```"

    def _convert_banner(self, content: str) -> str:
        """横幅 → 保留在代码块中"""
        return f"```\n{content}\n```"

    def _convert_code_block(self, content: str) -> str:
        """代码块 → Markdown 代码块"""
        return f"```\n{content}\n```"

    def _convert_diagram(self, content: str) -> str:
        """示意图 → 保留在代码块中"""
        return f"```\n{content}\n```"

    # ─── 主入口 ──────────────────────────────────

    def detect_blocks(self, text: str) -> list[AsciiBlock]:
        """检测文本中的所有 ASCII 图块"""
        lines = text.split("\n")
        blocks = []
        i = 0

        while i < len(lines):
            # 尝试从当前行开始识别 ASCII 图
            block_lines = []
            j = i

            # 跳过空行
            while j < len(lines) and not lines[j].strip():
                j += 1

            # 收集连续非空行
            while j < len(lines) and lines[j].strip():
                block_lines.append(lines[j])
                j += 1

            if len(block_lines) >= self.min_lines:
                block_type, confidence, is_fragment = self.detect_type(block_lines)
                if block_type != "unknown" and confidence > 0.2:
                    content = "\n".join(block_lines)
                    block = AsciiBlock(
                        block_type=block_type,
                        start_line=i,
                        end_line=j - 1,
                        content=content,
                        confidence=confidence,
                        is_fragment=is_fragment,
                    )
                    block.converted = self.convert(block)
                    blocks.append(block)
                    i = j
                    continue

            i += 1

        return blocks

    def detect_and_convert(self, text: str) -> str:
        """检测并转换文本中的所有 ASCII 图

        将识别到的 ASCII 图替换为转换后的格式。
        未识别的部分保持原样。
        """
        blocks = self.detect_blocks(text)
        if not blocks:
            return text

        lines = text.split("\n")
        result_lines = []
        skip_until = -1

        for i, line in enumerate(lines):
            if i <= skip_until:
                continue

            # 检查是否有块从当前行开始
            block = None
            for b in blocks:
                if b.start_line == i:
                    block = b
                    break

            if block:
                result_lines.append(block.converted)
                skip_until = block.end_line
            else:
                result_lines.append(line)

        return "\n".join(result_lines)

    def scan(self, text: str) -> list[dict]:
        """扫描文本，返回所有 ASCII 图块信息（不转换）"""
        blocks = self.detect_blocks(text)
        return [b.to_dict() for b in blocks]
