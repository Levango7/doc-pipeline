"""
MarkdownChecker v3 - 结构检查器（改进版）
===========================================
新增 v3：
  - 规则热更新（运行时重载 YAML）
  - 检查报告支持 Markdown 输出
  - YAML 块检查误报率大幅降低
  - P2 降级调整（表格列数不匹配 → P3）
  - 增量检测章节粒度精确到 ### 级

用法：
  python markdown_checker.py --file README.md [--detail] [--fix] [--format md]
"""

import os
import re
import sys
import json
import hashlib
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# =============================================================================
# 规则配置
# =============================================================================

DEFAULT_RULES = {
    "rules": {
        "p0_empty":        {"level": "P0", "category": "content",        "enabled": True,  "description": "文件内容为空"},
        "p0_size":         {"level": "P0", "category": "size",           "enabled": True,  "description": "文件超过限制", "params": {"max_size_mb": 10}},
        "p1_control_char": {"level": "P1", "category": "control_char",   "enabled": True,  "description": "控制字符"},
        "p1_unicode_border": {"level": "P1", "category": "unicode_border", "enabled": True, "description": "Unicode 边框字符"},
        "p1_yaml_syntax":  {"level": "P1", "category": "yaml_syntax",    "enabled": True,  "description": "YAML 语法错误"},
        "p1_structure":    {"level": "P1", "category": "structure",      "enabled": True,  "description": "章节过少",
                            "params": {"default_min": 3, "k8s_min": 10, "docker_min": 6, "mysql_min": 8}},
        "p2_empty_lines":  {"level": "P2", "category": "empty_lines",    "enabled": True,  "description": "连续空行过多", "params": {"max_consecutive": 5}},
        "p3_heading_gap":  {"level": "P3", "category": "heading_gap",    "enabled": True,  "description": "标题层级跳跃"},
        "p3_table_column": {"level": "P3", "category": "table_column",   "enabled": True,  "description": "表格列数不匹配"},
        "p3_missing_section": {"level": "P3", "category": "missing_section", "enabled": True, "description": "缺少推荐章节"},
    },
    "incremental": {
        "enabled": True,
        "hash_algorithm": "sha256",
    },
    "growth_monitor": {
        "enabled": True,
        "warn_size_growth_ratio": 2.0,
        "warn_size_shrink_ratio": 0.5,
        "warn_line_growth_ratio": 1.5,
        "warn_line_shrink_ratio": 0.7,
    }
}


class RuleConfig:
    """规则配置（支持 YAML 文件和硬编码默认值）"""

    DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "checker_rules.yaml")

    def __init__(self, path: str | None = None):
        self.path = path or self.DEFAULT_PATH
        self.data = self._load()
        self._index = {r.get("category", k): r
                       for k, r in self.data.get("rules", {}).items()}

    def _load(self) -> dict:
        # 尝试加载 YAML
        if HAS_YAML and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
                if loaded:
                    return loaded
            except Exception as e:
                print(f"[RuleConfig] YAML 加载失败: {e}，使用默认规则")

        return DEFAULT_RULES

    def is_enabled(self, category: str) -> bool:
        return self._index.get(category, {}).get("enabled", True)

    def get_param(self, category: str, key: str, default=None):
        return self._index.get(category, {}).get("params", {}).get(key, default)

    def get_level(self, category: str) -> str:
        return self._index.get(category, {}).get("level", "P2")

    def reload(self):
        """热重载规则文件"""
        self.data = self._load()
        self._index = {r.get("category", k): r
                       for k, r in self.data.get("rules", {}).items()}


_rules_cache: Optional[RuleConfig] = None


def get_rules(path: str | None = None) -> RuleConfig:
    global _rules_cache
    if _rules_cache is None or path:
        _rules_cache = RuleConfig(path)
    return _rules_cache


# =============================================================================
# Issue
# =============================================================================

class Issue:
    def __init__(self, level: str, category: str, message: str,
                 line: int | None = None, fix: str | None = None):
        self.level = level
        self.category = category
        self.message = message
        self.line = line
        self.fix = fix

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "line": self.line,
            "fix": self.fix,
        }

    def __repr__(self):
        loc = f"行{self.line}: " if self.line else ""
        return f"[{self.level}] {loc}{self.message}"


# =============================================================================
# 增量检测
# =============================================================================

class IncrementalChecker:
    """章节级增量更新检测"""

    def __init__(self, content: str, config: RuleConfig | None = None):
        self.content = content
        self.cfg = config or get_rules()
        self.sections = self._split()
        self.prev: dict = {}
        self._load_prev()

    def _split(self) -> list[dict]:
        lines = self.content.splitlines()
        sections = []
        current = {"title": "__preamble__", "content": [], "line_start": 0}

        for i, line in enumerate(lines):
            if re.match(r"^#{1,3}\s+\S", line.strip()):
                if current.get("content"):
                    current["hash"] = self._hash("\n".join(current["content"]))
                    sections.append(current)
                current = {"title": line.strip(), "content": [line], "line_start": i + 1}
            else:
                current.setdefault("content", []).append(line)

        if current.get("content"):
            current["hash"] = self._hash("\n".join(current["content"]))
            sections.append(current)

        return sections

    def _hash(self, text: str) -> str:
        algo = self.cfg.data.get("incremental", {}).get("hash_algorithm", "sha256")
        h = hashlib.new(algo)
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _load_prev(self):
        path = ".doc_baseline.json"
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.prev = json.load(f)
            except Exception:
                pass

    def save_baseline(self, path: str = ".doc_baseline.json"):
        data = {
            s["title"]: {
                "hash": s["hash"],
                "line_start": s.get("line_start", 0),
                "word_count": len(" ".join(s.get("content", [])).split()),
            }
            for s in self.sections
        }
        data["_stats"] = {
            "size_bytes": len(self.content.encode("utf-8")),
            "lines": self.content.count("\n") + 1,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def detect(self) -> dict:
        changes = []
        for s in self.sections:
            title = s["title"]
            prev = self.prev.get(title, {})
            status = ("new" if not prev
                      else "modified" if prev.get("hash") != s["hash"]
                      else "unchanged")
            changes.append({
                "title": title,
                "status": status,
                "hash": s["hash"],
                "line_start": s.get("line_start", 0),
            })

        new_count = sum(1 for c in changes if c["status"] == "new")
        modified = sum(1 for c in changes if c["status"] == "modified")
        unchanged = sum(1 for c in changes if c["status"] == "unchanged")

        return {
            "total_sections": len(self.sections),
            "summary": {"new": new_count, "modified": modified, "unchanged": unchanged},
            "changed": [c for c in changes if c["status"] != "unchanged"],
        }


# =============================================================================
# 主检查器
# =============================================================================

class Checker:
    """
    Markdown 结构检查器

    检查项：
      P0: 空内容、超大文件
      P1: 控制字符、Unicode 边框、YAML 语法错误、章节过少
      P2: 连续空行过多
      P3: 标题跳跃、表格列数不匹配、缺少推荐章节
    """

    def __init__(self, content: str, filepath: str = "",
                 fix: bool = False, rules: RuleConfig | None = None):
        self.content = content
        self.filepath = filepath
        self.fix = fix
        self.rules = rules or get_rules()
        self.lines = content.splitlines()
        self.issues: list[Issue] = []

    def _enabled(self, cat: str) -> bool:
        return self.rules.is_enabled(cat)

    def _param(self, cat: str, key: str, default=None):
        return self.rules.get_param(cat, key, default)

    # ── P0 ─────────────────────────────────────────────

    def check_p0(self):
        if not self.content.strip():
            self.issues.append(Issue("P0", "content", "文件内容为空"))
            return  # 后续无意义

        max_mb = self._param("size", "max_size_mb", 10)
        if len(self.content.encode("utf-8")) > max_mb * 1024 * 1024:
            self.issues.append(Issue("P0", "size",
                                     f"文件超过 {max_mb}MB 限制"))

    # ── P1 ─────────────────────────────────────────────

    def check_p1(self):
        # 控制字符
        if self._enabled("control_char"):
            for i, line in enumerate(self.lines, 1):
                for j, c in enumerate(line):
                    code = ord(c)
                    if code < 32 and c not in "\t\n\r":
                        self.issues.append(Issue(
                            "P1", "control_char",
                            f"控制字符 \\x{code:02x} 列 {j+1}", line=i
                        ))

        # Unicode 边框
        if self._enabled("unicode_border"):
            border_re = re.compile(r"[│─┌┐└┘├┤┬┴┼〇]")
            for i, line in enumerate(self.lines, 1):
                m = border_re.findall(line)
                if m:
                    self.issues.append(Issue(
                        "P1", "unicode_border",
                        f"Unicode 边框字符: {m}", line=i
                    ))

        # YAML 语法
        if self._enabled("yaml_syntax") and HAS_YAML:
            self._check_yaml_blocks()

        # 章节数量
        if self._enabled("structure"):
            self._check_structure()

    def _check_yaml_blocks(self):
        """检查 YAML 代码块语法"""
        yaml_re = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
        for m in yaml_re.finditer(self.content):
            text = m.group(1)

            # 过滤：JSON 内容
            stripped = text.strip()
            if stripped.startswith("{") or any(
                l.strip().startswith("{") for l in stripped.split("\n")[:5]
            ):
                continue

            # 过滤：REST API 示例
            if re.search(r"\b(PUT|POST|GET|DELETE)\s+[/_]", text):
                continue

            # 过滤：行内注释（降级为 P3）
            try:
                list(yaml.safe_load_all(text))
            except yaml.YAMLError as e:
                err = str(e)
                if "mapping values are not allowed" in err and "#" in text[:200]:
                    if re.search(r":\s*\w.*#", text[:300]):
                        offset = self.content[: m.start(1)].count("\n") + 1
                        self.issues.append(Issue(
                            "P3", "yaml_comment",
                            f"YAML 行内注释可能导致解析警告", line=offset
                        ))
                        continue

                offset = self.content[: m.start(1)].count("\n") + 1
                self.issues.append(Issue(
                    "P1", "yaml_syntax",
                    f"YAML 语法错误: {err[:80]}", line=offset
                ))

    def _check_structure(self):
        name = Path(self.filepath).stem.lower() if self.filepath else ""
        # P2 修复：原代码 self._param("structure", None, {}) 语义错误，
        # get_param(category, key, default) 中 key=None 会查找 params[None]，永远返回 default。
        # 改为通过 rules._index 读取整个 params 字典。
        params = self.rules._index.get("structure", {}).get("params", {}) or {}

        if "kubernetes" in name or "k8s" in name:
            min_ch = params.get("k8s_min", 10)
        elif "docker" in name:
            min_ch = params.get("docker_min", 6)
        elif "mysql" in name:
            min_ch = params.get("mysql_min", 8)
        else:
            min_ch = params.get("default_min", 3)

        found = len(re.findall(r"^#{1,2}\s+\S", self.content, re.MULTILINE))
        if found < min_ch:
            self.issues.append(Issue(
                "P1", "structure",
                f"章节数量过少: {found} 个（预期至少 {min_ch} 个）"
            ))

    # ── P2 ─────────────────────────────────────────────

    def check_p2(self):
        self._check_empty_lines()

    def _check_empty_lines(self):
        if not self._enabled("empty_lines"):
            return
        max_c = self._param("empty_lines", "max_consecutive", 5)
        consecutive = 0
        start = 0

        for i, line in enumerate(self.lines, 1):
            if not line.strip():
                if consecutive == 0:
                    start = i
                consecutive += 1
            else:
                if consecutive >= max_c:
                    self.issues.append(Issue(
                        "P2", "empty_lines",
                        f"连续 {consecutive} 行空行（始于行 {start}）",
                        line=start, fix="删除多余空行"
                    ))
                consecutive = 0

    # ── P3 ─────────────────────────────────────────────

    def check_p3(self):
        self._check_heading_gap()
        self._check_tables()
        self._check_missing_sections()

    def _check_heading_gap(self):
        if not self._enabled("heading_gap"):
            return
        pat = re.compile(r"^(#{1,6})\s+\S")
        prev = 0
        for i, line in enumerate(self.lines, 1):
            m = pat.match(line.strip())
            if m:
                level = len(m.group(1))
                if prev > 0 and level - prev > 1:
                    self.issues.append(Issue(
                        "P3", "heading_gap",
                        f"标题层级跳跃: h{prev} → h{level}", line=i
                    ))
                prev = level

    def _check_tables(self):
        if not self._enabled("table_column"):
            return
        sep_pat = re.compile(r"^\|[\s\-:]+\|$")
        lines = self.lines
        total = len(lines)
        i = 0

        while i < total:
            line = lines[i].strip()
            if not line.startswith("|") or sep_pat.match(line):
                i += 1
                continue

            # 附近是否有分隔行
            ctx_start, ctx_end = max(0, i - 5), min(total, i + 5)
            ctx = "\n".join(lines[ctx_start:ctx_end])
            if not re.search(r"\|\s*[-:]+\s*\|", ctx):
                i += 1
                continue

            parts = [c for c in line.split("|") if c.strip()]
            col_counts = [len(parts)]

            j = i + 1
            while j < total:
                nxt = lines[j].strip()
                if not nxt.startswith("|"):
                    break
                if sep_pat.match(nxt):
                    j += 1
                    continue
                col_counts.append(len([c for c in nxt.split("|") if c.strip()]))
                j += 1

            if len(col_counts) > 1 and len(set(col_counts)) > 1:
                self.issues.append(Issue(
                    "P3", "table_column",
                    f"表格列数不一致: {col_counts}", line=i + 1,
                    fix=f"对齐到 {max(col_counts)} 列"
                ))
            i = j

    def _check_missing_sections(self):
        if not self._enabled("missing_section"):
            return
        expected = [
            (r"常见问题|FAQ|faq", "常见问题 (FAQ)"),
            (r"最佳实践|best.practice", "最佳实践"),
            (r"故障排查|troubleshoot", "故障排查"),
        ]
        for pattern, name in expected:
            if not re.search(pattern, self.content, re.IGNORECASE):
                self.issues.append(Issue(
                    "P3", "missing_section",
                    f"建议补充章节: {name}"
                ))

    # ── 执行 ──────────────────────────────────────────

    def run(self) -> dict:
        self.check_p0()
        if not any(i.level == "P0" for i in self.issues):
            self.check_p1()
            self.check_p2()
            self.check_p3()

        by_level: dict[str, list] = {"P0": [], "P1": [], "P2": [], "P3": []}
        for iss in self.issues:
            by_level[iss.level].append(iss.to_dict())

        p0 = len(by_level["P0"])
        p1 = len(by_level["P1"])

        return {
            "status": "pass" if p0 == 0 and p1 == 0 else "fail",
            "total_issues": len(self.issues),
            "by_level": by_level,
            "summary": {
                "P0_blocking": p0,
                "P1_severe": p1,
                "P2_warning": len(by_level["P2"]),
                "P3_suggestion": len(by_level["P3"]),
            }
        }


# =============================================================================
# 公共入口
# =============================================================================

def check_file(filepath: str, rules_path: str | None = None,
               incremental: bool = True) -> dict:
    """供外部调用的文件检查接口"""
    if not os.path.exists(filepath):
        return {"status": "error", "message": f"文件不存在: {filepath}"}

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return {"status": "error", "message": str(e)}

    rules = get_rules(rules_path)
    checker = Checker(content, filepath, fix=False, rules=rules)
    result = checker.run()

    if incremental:
        inc = IncrementalChecker(content, rules)
        result["incremental"] = inc.detect()
        baseline_dir = os.path.dirname(filepath) or "."
        inc.save_baseline(os.path.join(baseline_dir, ".doc_baseline.json"))

    # 对齐 coordinator 期望的字段
    if result["status"] == "pass":
        result["status"] = "ok"
    elif result["summary"]["P0_blocking"] > 0:
        result["status"] = "error"
    else:
        result["status"] = "warning"

    return result


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="MarkdownChecker v3")
    parser.add_argument("--file", "-f", required=True, help="待检查文件")
    parser.add_argument("--detail", "-d", action="store_true", help="显示详细问题")
    parser.add_argument("--fix", action="store_true", help="自动修复（实验性）")
    parser.add_argument("--format", choices=["text", "json", "md"], default="text")
    parser.add_argument("--rules", help="规则 YAML 路径")
    parser.add_argument("--no-incremental", action="store_true")
    args = parser.parse_args()

    rules = get_rules(args.rules)
    filepath = os.path.abspath(args.file)

    if not os.path.exists(filepath):
        print(f"[Checker] 文件不存在: {filepath}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    checker = Checker(content, filepath, fix=args.fix, rules=rules)
    result = checker.run()

    if not args.no_incremental:
        inc = IncrementalChecker(content, rules)
        changes = inc.detect()
        result["incremental"] = changes
        inc.save_baseline(os.path.join(os.path.dirname(filepath), ".doc_baseline.json"))

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["status"] == "pass" else 1)

    if args.format == "md":
        _print_md(result, filepath)
        sys.exit(0 if result["status"] == "pass" else 1)

    # 文本输出
    summary = result["summary"]
    status_icon = "✅" if result["status"] == "pass" else "❌"
    print(f"\n{'='*55}")
    print(f"{status_icon} 检查结果: {filepath}")
    print(f"{'='*55}")
    print(f"  P0 阻断: {summary['P0_blocking']}")
    print(f"  P1 严重: {summary['P1_severe']}")
    print(f"  P2 警告: {summary['P2_warning']}")
    print(f"  P3 建议: {summary['P3_suggestion']}")

    if "incremental" in result:
        s = result["incremental"]["summary"]
        print(f"  增量: 新 {s['new']} / 改 {s['modified']} / 未变 {s['unchanged']}")

    if args.detail:
        for level in ["P0", "P1", "P2", "P3"]:
            issues = result["by_level"][level]
            if not issues:
                continue
            icons = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🔵"}
            print(f"\n{icons[level]} {level} ({len(issues)} 项):")
            for iss in issues[:20]:
                loc = f"行{iss['line']}: " if iss.get("line") else ""
                print(f"   {loc}{iss['message']}")
                if iss.get("fix"):
                    print(f"   → {iss['fix']}")

    print(f"{'='*55}\n")
    sys.exit(0 if result["status"] == "pass" else 1)


def _print_md(result: dict, filepath: str):
    """Markdown 格式输出"""
    summary = result["summary"]
    status = "✅ 通过" if result["status"] == "pass" else "❌ 失败"
    print(f"# 检查报告: `{os.path.basename(filepath)}`")
    print(f"\n**状态**: {status}")
    print(f"\n| 级别 | 数量 |")
    print(f"|------|------|")
    print(f"| P0 阻断 | {summary['P0_blocking']} |")
    print(f"| P1 严重 | {summary['P1_severe']} |")
    print(f"| P2 警告 | {summary['P2_warning']} |")
    print(f"| P3 建议 | {summary['P3_suggestion']} |")

    for level in ["P0", "P1", "P2", "P3"]:
        issues = result["by_level"][level]
        if not issues:
            continue
        print(f"\n## {level}")
        for iss in issues[:20]:
            loc = f"（行 {iss['line']}）" if iss.get("line") else ""
            print(f"- {iss['message']}{loc}")


if __name__ == "__main__":
    main()
