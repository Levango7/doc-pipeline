"""
QualityGate Agent v2 - 质量门禁（Profile 模板驱动）
=================================================
评分维度:
  - completeness:   文档结构完整度（必需章节、目录、参考资料）
  - structure:      标题层级合理性（H1→H2→H3 递进）
  - readability:    内容可读性（段落长度、语言风格）
  - citation:       引用可追溯性（URL 有效性、来源一致性）
  - depth:          内容深度（字数、段落数、信息密度）

特点:
  - 从 YAML Quality Profile 加载评分配置（可插拔）
  - 风格规则从 Profile 加载，不硬编码
  - 引用检查可配置启用/禁用
  - 扣分上限可配置
"""
import re
import os
import sys
import time
import yaml
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from pipeline_core.base_agent import BaseAgent, Message, AgentStatus, AgentMeta


AGENT_NAME = "quality_gate"
AGENT_VERSION = "2.0"
AGENT_DESC = "质量门禁 Agent v2 - Profile 模板驱动、可插拔评分"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 40
INPUT_TOPICS = ["writer.done", "quality_gate.check", "quality_gate.input"]
OUTPUT_TOPICS = ["quality_gate.done", "quality_gate.failed"]
DEPENDENCIES = ["writer"]
CACHE_TTL = 0
RESPAWN = False
SUPPORTS_REGENERATION = True
REGENERATION_TARGET = "writer"
REGENERATION_RECHECK = "quality_gate"
AGENT_TAGS = ["quality", "gate"]

# 默认配置文件路径
QUALITY_DIR = Path(__file__).parent.parent / "pipelines" / "quality"
DEFAULT_PROFILE = "technical-doc.yaml"


def load_profile(profile_name: str) -> dict:
    """加载 Quality Profile YAML"""
    try:
        path = Path(profile_name)
        if not path.exists():
            path = QUALITY_DIR / profile_name
            if not path.suffix:
                path = path.with_suffix(".yaml")
        if not path.exists():
            path = QUALITY_DIR / DEFAULT_PROFILE
        if not path.exists():
            print(f"[quality_gate] 警告: 未找到 profile 文件", flush=True)
            return {}

        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[quality_gate] 警告: 加载 profile 失败: {e}", flush=True)
        return {}


class QualityGateAgent(BaseAgent):
    """质量门禁 Agent（Profile 驱动）"""

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)

        # 加载 Quality Profile
        profile_name = config.get("quality_profile", "technical-doc")
        self._profile = load_profile(profile_name)
        self._profile_name = self._profile.get("name", profile_name)

        # 从 Profile 加载配置（fallback 到 config → 默认值）
        self._weights = {**self._profile.get("weights", {}), **config.get("weights", {})}
        self._threshold = config.get("threshold", self._profile.get("threshold", 70))
        self._max_regenerations = config.get("max_regenerations",
                                              self._profile.get("max_regenerations", 3))
        self._max_penalty = config.get("max_penalty", self._profile.get("max_penalty", 40))
        self._citation_cfg = {**self._profile.get("citation", {}),
                              **config.get("citation", {})}

        # 编译风格规则（从 Profile 加载，不硬编码）
        self._style_rules = []
        for rule_cfg in self._profile.get("style_rules", []):
            if rule_cfg.get("enabled", True):
                self._style_rules.append({
                    "name": rule_cfg["name"],
                    "pattern": re.compile(rule_cfg["pattern"]),
                    "message": rule_cfg.get("message", ""),
                    "penalty": rule_cfg.get("penalty", 0),
                })

        self.log_info(f"QualityGate v{AGENT_VERSION} (profile={self._profile_name}, "
                      f"threshold={self._threshold})")

    def _rebuild_from_profile(self, run_config: dict):
        """从已加载的 profile 重建配置"""
        self._weights = {**self._profile.get("weights", {}), **run_config.get("weights", {})}
        self._threshold = run_config.get("threshold", self._profile.get("threshold", 70))
        self._max_regenerations = run_config.get("max_regenerations",
                                                  self._profile.get("max_regenerations", 3))
        self._max_penalty = run_config.get("max_penalty", self._profile.get("max_penalty", 40))
        self._citation_cfg = {**self._profile.get("citation", {}),
                              **run_config.get("citation", {})}
        self._style_rules = []
        for rule_cfg in self._profile.get("style_rules", []):
            if rule_cfg.get("enabled", True):
                self._style_rules.append({
                    "name": rule_cfg["name"],
                    "pattern": re.compile(rule_cfg["pattern"]),
                    "message": rule_cfg.get("message", ""),
                    "penalty": rule_cfg.get("penalty", 0),
                })

    def handle(self, msg: Message) -> dict | None:
        """处理质量检查请求"""
        self.report(AgentStatus.RUNNING, "开始质量评估...")
        payload = msg.payload
        content = payload.get("content", "")
        task_id = payload.get("task_id", "")
        generation_count = payload.get("generation_count", 0)

        if not content:
            return {"status": "error", "message": "内容为空", "score": 0}

        # 支持从流水线配置覆盖 Quality Profile
        run_config = payload.get("config", {})
        profile_name = run_config.get("quality_profile", "technical-doc")
        if profile_name != self._profile_name:
            self._profile = load_profile(profile_name)
            self._profile_name = self._profile.get("name", profile_name)
            self._rebuild_from_profile(run_config)

        # 多维度评分（按 profile weights）
        queries = payload.get("queries", []) or []
        scores = self._score_all(content, queries)
        overall = self._overall_score(scores)

        # 风格检查（从 profile 规则）
        style_issues = self._check_style(content)
        style_penalty = sum(i.get("penalty", 0) for i in style_issues)

        # 引用检查（可禁用）
        citation_penalty = 0
        citation_report = {"total_refs": 0, "issues": []}
        if self._citation_cfg.get("enabled", True):
            citation_report = self._check_citations(content)
            citation_penalty = len(citation_report.get("issues", [])) * \
                               self._citation_cfg.get("penalty_per_issue", 5)

        # 总扣分
        total_penalty = min(style_penalty + citation_penalty, self._max_penalty)
        overall = max(0, overall - total_penalty)

        needs_regenerate = overall < self._threshold
        can_regenerate = generation_count < self._max_regenerations

        result = {
            "status": "pass" if not needs_regenerate else "fail",
            "task_id": task_id,
            "overall_score": round(overall, 1),
            "scores": {k: round(v, 1) for k, v in scores.items()},
            "profile": self._profile_name,
            "style_issues": style_issues,
            "citation_report": citation_report,
            "penalty": {"style": style_penalty, "citation": citation_penalty, "total": total_penalty},
            "needs_regenerate": needs_regenerate,
            "can_regenerate": can_regenerate,
            "generation_count": generation_count,
        }

        if needs_regenerate:
            info = f" (扣分: {total_penalty})" if total_penalty > 0 else ""
            self.log_warning(
                f"质量分 {overall:.1f} < {self._threshold}{info}"
                f"({'可重做' if can_regenerate else '已达上限'}) "
                f"问题: {self._score_breakdown(scores)}"
            )
        else:
            self.log_info(f"质量分 {overall:.1f}/{self._threshold} 通过 (profile={self._profile_name})")

        self.publish("quality_gate.done" if not needs_regenerate else "quality_gate.failed", result)
        return result

    # ── 多维度评分 ─────────────────────

    def _score_all(self, content: str, queries: list[str] = None) -> dict[str, float]:
        return {
            "completeness": self._score_completeness(content),
            "structure": self._score_structure(content),
            "readability": self._score_readability(content),
            "citation": self._score_citations(content),
            "depth": self._score_depth(content),
            "substance": self._score_substance(content),
            "topic_relevance": self._score_topic_relevance(content, queries),
        }

    def _score_topic_relevance(self, content: str, queries: list[str]) -> float:
        """主题相关度：文档是否真的在讲 query 主题（而非跑题）

        从 queries 提取核心关键词，检查是否出现在文档标题/章节/正文中。
        无 query 时返回中性分（不惩罚）。
        """
        if not queries:
            return 70.0  # 无 query 时中性，不阻断
        import re
        # 提取所有 query 的核心词（去噪音）
        stop = {"的", "了", "是", "在", "我", "有", "和", "与", "及", "一个", "这份",
                "介绍", "简单", "基本", "概念", "生成", "一份", "文档", "技术", "测试",
                "这是", "用于", "验证", "流水线", "是否", "正常", "工作", "a", "the",
                "of", "to", "and", "is", "for", "this", "that", "with", "in", "on"}
        tokens: set[str] = set()
        for q in queries:
            for w in re.findall(r"[一-鿿]{2,}|[a-zA-Z]{2,}", q.lower()):
                if w not in stop:
                    tokens.add(w)
        if not tokens:
            return 70.0

        text = content.lower()
        head = "\n".join(content.split("\n")[:30]).lower()  # 标题+目录+前两章
        hit = sum(1 for t in tokens if t.lower() in text)
        hit_head = sum(1 for t in tokens if t.lower() in head)
        coverage = hit / len(tokens)
        # 头部命中加权（跑题文档头部往往没有关键词）
        return round(min(100.0, coverage * 70 + hit_head / len(tokens) * 30), 1)

    def _score_substance(self, content: str) -> float:
        """内容实质度：检测水话 / 车轱辘话 / 空洞，而非仅看格式"""
        score = 100.0
        paras = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 30]
        if not paras:
            return 0.0

        # 1. 信息密度：实词（长度>=2 的词）占比
        all_words = re.findall(r'[\w\u4e00-\u9fff]{2,}', content)
        if all_words:
            # 高频虚词（中英文停用词）占比高 = 信息密度低
            stop = set("的 了 是 在 和 与 及 也 都 就 而 等 我们 可以 这个 那个 一种 以及 通过 对于 由于 因此 但是 因为 the a an of to and or in on for is are be".split())
            content_words = [w for w in all_words if w.lower() not in stop]
            density = len(content_words) / len(all_words)
            if density < 0.4:
                score -= 25 * (0.4 - density) / 0.4

        # 2. 相邻段落重复率（Jaccard），过高 = 车轱辘话
        max_sim = 0.0
        for i in range(1, len(paras)):
            a = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', paras[i-1]))
            b = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', paras[i]))
            if a and b:
                sim = len(a & b) / len(a | b)
                max_sim = max(max_sim, sim)
        if max_sim > 0.6:
            score -= 30 * (max_sim - 0.6) / 0.4

        # 3. 实质信号缺失：无数字、无代码块、无列表/定义
        has_number = bool(re.search(r'\d', content))
        has_code = '```' in content
        has_list = bool(re.search(r'^\s*[-*]\s', content, re.MULTILINE))
        signals = sum([has_number, has_code, has_list])
        if signals == 0:
            score -= 20

        return max(0, score)

    def _score_completeness(self, content: str) -> float:
        score = 100.0
        if not re.search(r"^#\s", content, re.MULTILINE):
            score -= 25
        if "## 目录" not in content and "目录" not in content[:500]:
            score -= 15
        if "## 参考资料" not in content and "## 参考" not in content:
            score -= 20
        paragraphs = [p for p in content.split("\n\n") if len(p.strip()) > 30]
        if len(paragraphs) < 3:
            score -= 20 * (3 - len(paragraphs))
        return max(0, score)

    def _score_structure(self, content: str) -> float:
        score = 100.0
        headings = re.findall(r"^(#+)\s", content, re.MULTILINE)
        if not headings:
            return 30
        h1_count = headings.count("#")
        if h1_count == 0:
            score -= 20
        elif h1_count > 1:
            score -= 10
        levels = [len(h) for h in headings]
        for i in range(1, len(levels)):
            if levels[i] > levels[i - 1] + 1:
                score -= 5
        h2_count = headings.count("##")
        if h2_count < 2:
            score -= 15 * (2 - h2_count)
        return max(0, score)

    def _score_readability(self, content: str) -> float:
        score = 100.0
        lines = content.split("\n")
        long_lines = sum(1 for l in lines if len(l) > 120)
        score -= long_lines * 2
        if "```" not in content and "> " not in content:
            score -= 10
        if "- " not in content and "* " not in content:
            score -= 5
        return max(0, score)

    def _score_citations(self, content: str) -> float:
        score = 100.0
        refs = re.findall(r"\[([^\]]*)\]\(([^)]*)\)", content)
        if not refs:
            return 50
        for title, url in refs:
            if not url or url.strip() == "":
                score -= 15
            elif url.startswith("https://example.com"):
                score -= 10
            elif not url.startswith(("http://", "https://", "#")):
                score -= 5
        return max(0, score)

    def _score_depth(self, content: str) -> float:
        score = 100.0
        word_count = len(content)
        para_count = len([p for p in content.split("\n\n") if len(p.strip()) > 30])
        if word_count < 200:
            score = 30
        elif word_count < 500:
            score = 50
        elif word_count < 1000:
            score = 70
        elif word_count > 5000:
            score = 95
        if para_count < 3:
            score = min(score, 40)
        return score

    def _overall_score(self, scores: dict[str, float]) -> float:
        if not self._weights:
            return sum(scores.values()) / max(len(scores), 1)
        total_weight = sum(self._weights.values())
        if total_weight <= 0:
            return sum(scores.values()) / max(len(scores), 1)
        total = sum(scores.get(k, 0) * w for k, w in self._weights.items())
        return total / total_weight

    def _score_breakdown(self, scores: dict[str, float]) -> str:
        parts = [f"{k}={v:.0f}" for k, v in sorted(scores.items())]
        return ", ".join(parts)

    # ── 风格检查（从 Profile 规则） ─────

    def _check_style(self, content: str) -> list[dict]:
        issues = []
        for rule in self._style_rules:
            matches = rule["pattern"].findall(content)
            if matches:
                issues.append({
                    "rule": rule["name"],
                    "message": rule["message"],
                    "count": len(matches),
                    "penalty": rule["penalty"],
                })
        return issues

    # ── 引用验证（可禁用） ─────

    def _check_citations(self, content: str) -> dict:
        refs = re.findall(r"\[([^\]]*)\]\(([^)]*)\)", content)
        seen_urls = {}
        issues = []
        for title, url in refs:
            if url in seen_urls:
                seen_urls[url]["count"] += 1
            else:
                seen_urls[url] = {"title": title, "count": 1}
            if not url:
                continue
            if self._citation_cfg.get("check_url_format", True):
                if not url.startswith(("http://", "https://", "#", "/")):
                    issues.append(f"非标准 URL: {url[:50]}")
            if self._citation_cfg.get("check_empty_title", True):
                if not title.strip():
                    issues.append("存在无标题链接")
        return {
            "total_refs": len(refs),
            "unique_urls": len(seen_urls),
            "duplicates": sum(1 for v in seen_urls.values() if v["count"] > 1),
            "issues": issues,
        }

    def handle_writer_done(self, msg: Message):
        payload = msg.payload
        content = payload.get("content", "")
        if content:
            self.publish("quality_gate.check", {
                "task_id": payload.get("task_id", ""),
                "content": content,
                "generation_count": payload.get("generation_count", 0),
            })