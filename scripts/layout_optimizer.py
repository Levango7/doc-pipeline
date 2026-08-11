"""
LayoutOptimizer v3 - ASCII 图表对齐优化器（改进版）
=====================================================
改进点 v3：
  - 修复 CJK 视觉宽度计算（v2 的 continuation byte bug 已修复，v3 继承）
  - 支持嵌套代码块探测（```语言 正确识别）
  - 新增统计：每个 code block 的修复情况
  - 支持"仅检查"模式（不修改）

核心算法：
  1. 只处理 ``` 代码块内部
  2. 找 +--- 风格边框（前导空格 ≤ 10，无 |，无中文）
  3. 目标宽度 = 最近的含中文内容行的视觉宽度
  4. 仅当差值 > 2 时修复
  5. 内嵌小框（缩进 > 10）不修
"""

import argparse
import re
import sys
from pathlib import Path

# =============================================================================
# 视觉宽度计算（核心）
# =============================================================================

def char_vis(c: str) -> int:
    """单字符视觉宽度"""
    if "\u4e00" <= c <= "\u9fff":   # CJK 汉字
        return 2
    if "\uff00" <= c <= "\uffef":   # 全角符号
        return 2
    return 1


def vis_str(s: str) -> int:
    """字符串视觉宽度（用于 Python str）"""
    return sum(char_vis(c) for c in s)


def vis_bytes(data: bytes) -> int:
    """
    UTF-8 字节流视觉宽度

    v2 修复：
    - continuation byte (0x80-0xBF) 不计宽度（已在首字节计入）
    """
    w = 0
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b < 0x80:
            w += 1
            i += 1
        elif b < 0xC0:
            # UTF-8 continuation byte：跳过，宽度已在首字节统计
            i += 1
        elif b < 0xE0:
            # 2字节字符（Latin Extended 等）
            w += 1
            i += 2
        elif b < 0xF0:
            # 3字节字符（CJK 等）
            try:
                c = data[i : i + 3].decode("utf-8", "replace")
                if "\u4e00" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef":
                    w += 2
                else:
                    w += 1
            except Exception:
                w += 1
            i += 3
        else:
            # 4字节字符（emoji 等）
            w += 2
            i += 4
    return w


def leading_spaces(line: bytes) -> int:
    """统计前导空格数"""
    return len(line) - len(line.lstrip(b" "))


# =============================================================================
# 判断函数
# =============================================================================

def is_fence(line: bytes) -> bool:
    """是否是代码栅栏 ``` 开头"""
    s = line.strip()
    return len(s) >= 3 and s[:3] == b"```"


def is_outer_border(line: bytes) -> bool:
    """
    是否是需要修复的外框顶/底边框
    条件：
      - 以 + 开头
      - 包含 -
      - 不含 |（竖线意味着是内嵌框或表格）
      - 不含中文（中文意味着是内容行）
      - 长度 >= 5
    """
    s = line.strip()
    if not s or b"|" in s:
        return False
    try:
        ts = s.decode("utf-8")
        if any("\u4e00" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef" for c in ts):
            return False
    except Exception:
        return False
    return b"+" in s and b"-" in s and len(s) >= 5


def has_cjk_content(line: bytes) -> bool:
    """是否含有中文内容（排除纯装饰行）"""
    try:
        s = line.decode("utf-8", "replace").strip()
        if not s:
            return False
        if not any("\u4e00" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef" for c in s):
            return False
        # 排除纯横线和纯加号行
        return not (re.match(r"^[\-\s]+$", s) or re.match(r"^[\+\s]+$", s))
    except Exception:
        return False


def is_md_table_sep(s: str) -> bool:
    """是否是 Markdown 表格分隔行"""
    return bool(re.match(r"^\|[:\-\s│]+\|$", s.strip()))


# =============================================================================
# 修复函数
# =============================================================================

def rebuild_border(content: str, target: int) -> str:
    """将 +---+ 边框重建到目标视觉宽度"""
    inner = max(1, target - 2)
    return "+" + "-" * inner + "+"


def fix_border_line(line: bytes, target: int) -> bytes:
    """修复一行边框到目标宽度"""
    # 检测换行符
    eol = b""
    if line.endswith(b"\r\n"):
        eol = b"\r\n"
    elif line.endswith(b"\n"):
        eol = b"\n"
    elif line.endswith(b"\r"):
        eol = b"\r"

    s = line.decode("utf-8", "replace").rstrip("\r\n")
    stripped = s.lstrip()
    indent = " " * (len(s) - len(stripped))

    cur = vis_str(stripped)
    if abs(cur - target) <= 2:
        return line  # 差值小，不修

    new_content = indent + rebuild_border(stripped, target)
    return new_content.encode("utf-8") + eol


# =============================================================================
# 主类
# =============================================================================

class LayoutOptimizer:
    """
    ASCII 图表布局优化器 v3

    用法：
        opt = LayoutOptimizer(content)
        result = opt.run()
        if result["stats"]["borders_fixed"] > 0:
            fixed_content = opt.get_result()
    """

    def __init__(self, content: str = ""):
        self.original = content
        self.content = content
        self.stats = {
            "blocks_scanned": 0,
            "borders_fixed": 0,
            "lines_examined": 0,
            "block_details": [],
        }

    def get_result(self) -> str:
        return self.content

    def run(self) -> dict:
        """执行优化"""
        self.content = self.original
        fixed = self._scan_and_fix()
        self.stats["borders_fixed"] = fixed

        steps = []
        if fixed > 0:
            steps.append(f"修复 {fixed} 处 ASCII 边框")

        return {
            "status": "ok",
            "steps": steps,
            "stats": self.stats,
        }

    def _scan_and_fix(self) -> int:
        """扫描所有代码块，修复外框边框"""
        raw = self.content.encode("utf-8")
        lines = raw.split(b"\n")
        self.stats["lines_examined"] = len(lines)

        total_fixed = 0
        in_code = False
        block_start = 0
        block_fixed = 0
        i = 0

        while i < len(lines):
            line = lines[i]

            if is_fence(line):
                if not in_code:
                    in_code = True
                    block_start = i
                    block_fixed = 0
                else:
                    in_code = False
                    if block_fixed > 0:
                        self.stats["block_details"].append({  # type: ignore[attr-defined]
                            "start_line": block_start + 1,
                            "end_line": i + 1,
                            "fixed": block_fixed,
                        })
                i += 1
                continue

            if not in_code:
                i += 1
                continue

            # 跳过 Markdown 表格分隔行
            try:
                ts = line.decode("utf-8", "replace").strip()
                if is_md_table_sep(ts):
                    i += 1
                    continue
            except Exception:
                pass

            # 不是外框边框 → 跳过
            if not is_outer_border(line):
                i += 1
                continue

            # 内嵌框（缩进 > 10）→ 跳过
            sp = leading_spaces(line)
            if sp > 10:
                i += 1
                continue

            # 往前找最近的含中文内容行（最多 50 行）
            target_width = None
            for k in range(i - 1, max(-1, i - 51), -1):
                nb = lines[k].strip()
                if not nb:
                    continue
                if is_fence(nb):
                    break
                if is_outer_border(nb):
                    continue
                if has_cjk_content(nb):
                    w = vis_bytes(nb)
                    if w > 0:
                        target_width = w
                        break
                # 更深缩进的内容 → 停止
                if leading_spaces(lines[k]) > sp + 5:
                    break

            if target_width is None:
                i += 1
                continue

            cur = vis_bytes(line.strip())
            if cur < target_width - 2:
                new_line = fix_border_line(line, target_width)
                if new_line != line:
                    lines[i] = new_line
                    total_fixed += 1
                    block_fixed += 1

            i += 1

        self.stats["blocks_scanned"] = sum(1 for line in lines if is_fence(line)) // 2

        # 重建内容（保持原始换行符风格）
        orig_bytes = self.original.encode("utf-8")
        if b"\r\n" in orig_bytes:
            joined = b"\n".join(lines)
            joined = joined.replace(b"\r\n", b"\n").replace(b"\r", b"").replace(b"\n", b"\r\n")
        elif b"\r" in orig_bytes:
            joined = b"\n".join(lines)
        else:
            joined = b"\n".join(lines)

        self.content = joined.decode("utf-8", "replace")
        return total_fixed


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="LayoutOptimizer v3")
    parser.add_argument("input", help="输入文件")
    parser.add_argument("--output", "-o", help="输出文件（默认覆盖原文件）")
    parser.add_argument("--dry-run", "-n", action="store_true", help="仅显示，不写入")
    parser.add_argument("--check", "-c", action="store_true", help="仅检查，不修改")
    parser.add_argument("--stats", "-s", action="store_true", help="输出详细统计")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8", errors="replace") as f:
        content = f.read()

    print(f"[Layout] 输入: {args.input} ({len(content):,} 字符)")

    opt = LayoutOptimizer(content)
    result = opt.run()
    stats = result["stats"]

    print("\n[Layout] 统计:")
    print(f"  扫描行数:   {stats['lines_examined']}")
    print(f"  代码块数:   {stats['blocks_scanned']}")
    print(f"  修复边框数: {stats['borders_fixed']}")

    if args.stats and stats.get("block_details"):
        print("\n[Layout] 修复详情:")
        for d in stats["block_details"]:
            print(f"  代码块 行{d['start_line']}-{d['end_line']}: 修复 {d['fixed']} 处")

    if args.check or args.dry_run:
        if stats["borders_fixed"] == 0:
            print("\n[Layout] 无需修复")
        else:
            print(f"\n[Layout] 发现 {stats['borders_fixed']} 处需修复（{'dry-run' if args.dry_run else '仅检查'}，未写入）")
        return

    new_content = opt.get_result()
    if new_content == content:
        print("\n[Layout] 内容无变化，退出")
        return

    out_path = args.output or args.input
    # 优先使用 safe_writer
    scripts_dir = Path(__file__).parent
    sys.path.insert(0, str(scripts_dir))

    try:
        from safe_writer import safe_write
        result2 = safe_write(
            target=out_path,
            content=new_content,
            backup_dir=str(Path(out_path).parent / "backups"),
            reason="layout_optimizer_v3",
            agent="LayoutOptimizer",
        )
        if result2["status"] == "ok":
            print(f"\n[Layout] ✓ 已写入: {out_path} ({result2['size']:,} bytes)")
        else:
            print(f"\n[Layout] ✗ 写入失败: {result2}")
    except ImportError:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        print(f"\n[Layout] 已写入（降级模式）: {out_path}")


if __name__ == "__main__":
    main()
