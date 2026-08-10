"""
Three-Pass Pipeline — 三阶段文档生成
=====================================
核心特性：
  - Phase 1 (Research)：多引擎搜索 + 网页抓取 + 内容提取
  - Phase 2 (Structure)：骨架规划 + 章节分配 + TF-IDF 填充
  - Phase 3 (Refine)：LLM 逐章节精修 + 段落润色 + 格式优化
  - 与现有 DAG 流水线兼容（可作为替代或补充）
  - 支持 LLM 路由器自动 fallback
  - 支持无文章时纯 LLM 生成

用法：
    from pipeline_core.three_pass_pipeline import ThreePassPipeline
    pipeline = ThreePassPipeline()
    result = pipeline.generate("Apache Kafka 核心架构", output_path="output/kafka.md")
"""
import os
import time
import json
import logging
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


@dataclass
class PassResult:
    """单阶段结果"""
    phase: str
    status: str       # "ok" | "error" | "skip"
    duration: float = 0.0
    data: dict = field(default_factory=dict)
    error: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


@dataclass
class DocumentPlan:
    """文档骨架"""
    title: str
    query: str
    sections: list[dict] = field(default_factory=list)
    # 每个 section: {title, prompt, keywords, content, sources}

    def to_markdown(self) -> str:
        lines = [f"# {self.title}\n"]
        lines.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        for sec in self.sections:
            lines.append(f"\n## {sec['title']}\n")
            content = sec.get("content", "")
            if content:
                lines.append(content)
            else:
                lines.append("*（暂无可用的相关内容）*")
            lines.append("")
        return "\n".join(lines)


class ThreePassPipeline:
    """三阶段文档生成流水线

    Phase 1 — Research & Gather
        多引擎搜索 → 网页抓取 → 正文提取 → 相关性过滤

    Phase 2 — Structure & Draft
        骨架规划（5-7 章节）→ TF-IDF 语义匹配 → 章节内容填充

    Phase 3 — Refine & Polish
        LLM 逐章节精修 → 段落衔接润色 → 格式优化 → 最终输出
    """

    def __init__(self, project_root: str = None):
        import warnings
        warnings.warn(
            "ThreePassPipeline 已废弃，请使用 DAG 流水线模式（python run.py --pipeline docgen）。"
            "该模块绕过 DAG/registry/bus，不再维护，将在未来版本移除。",
            DeprecationWarning,
            stacklevel=2,
        )
        self._root = Path(project_root) if project_root else Path(__file__).parent.parent
        self._llm_router = None
        self._search_mgr = None
        self._init_components()

    def _init_components(self):
        """初始化组件（优雅降级）"""
        # LLM 路由器
        try:
            from pipeline_core.llm_router import get_router
            self._llm_router = get_router()
        except Exception as e:
            logger.warning(f"LLM 路由器初始化失败: {e}")

        # 搜索引擎管理器
        try:
            from pipeline_core.search_engines import SearchEngineManager
            self._search_mgr = SearchEngineManager.from_env()
        except Exception as e:
            logger.warning(f"搜索引擎管理器初始化失败: {e}")

    # ─── Phase 1: Research & Gather ───────────────

    def phase1_research(self, query: str, max_results: int = 20) -> PassResult:
        """Phase 1: 搜索 + 抓取"""
        start = time.time()
        logger.info(f"[Phase 1] 开始研究: {query}")

        search_results = []
        if self._search_mgr:
            search_results = self._search_mgr.search_with_sites(query, max_results=max_results)
            logger.info(f"[Phase 1] 搜索返回 {len(search_results)} 条结果")

        # 抓取网页内容（并行）
        articles = []
        urls_to_fetch = search_results[:max_results]
        # 修复 P1：原代码 ThreadPoolExecutor(min(8, len(urls_to_fetch)))，
        # 当 urls_to_fetch 为空时 max_workers=0 会抛 ValueError。
        # 用 max(1, ...) 保证至少 1 个 worker，空列表时循环体不执行。
        with ThreadPoolExecutor(max(1, min(8, len(urls_to_fetch)))) as pool:
            future_map = {
                pool.submit(self._fetch_url, item.url): item
                for item in urls_to_fetch
            }
            for future in as_completed(future_map):
                item = future_map[future]
                try:
                    content = future.result()
                except Exception as e:
                    logger.debug(f"抓取异常 {item.url}: {e}")
                    continue
                if content and len(content) > 200:
                    articles.append({
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "text": content[:10000],
                        "source": item.source,
                    })

        duration = time.time() - start
        logger.info(f"[Phase 1] 完成: {len(articles)} 篇文章, {duration:.1f}s")

        return PassResult(
            phase="research", status="ok", duration=duration,
            data={"articles": articles, "search_count": len(search_results)},
        )

    def _fetch_url(self, url: str) -> str:
        """抓取网页正文（优先 trafilatura，回退到正则）"""
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.debug(f"抓取失败 {url}: {e}")
            return ""

        # 优先使用 trafilatura 提取正文
        try:
            import trafilatura
            text = trafilatura.extract(html, include_comments=False, include_tables=True)
            if text and len(text) > 200:
                return text.strip()
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"trafilatura 提取失败: {e}")

        # 优先使用 selectolax 提取正文
        try:
            from selectolax.parser import HTMLParser
            tree = HTMLParser(html)
            for tag in ("nav", "footer", "aside", "header", "script", "style"):
                for node in tree.css(tag):
                    node.decompose()
            text = tree.body.text(separator=" ", strip=True) if tree.body else ""
            if text and len(text) > 200:
                return text.strip()
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"selectolax 提取失败: {e}")

        # 回退：正则提取
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;|&amp;|&lt;|&gt;|&quot;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    # ─── Phase 2: Structure & Draft ───────────────

    def phase2_structure(self, query: str, articles: list[dict]) -> PassResult:
        """Phase 2: 骨架规划 + 内容填充"""
        start = time.time()
        logger.info(f"[Phase 2] 开始结构化: {len(articles)} 篇文章")

        # 规划骨架
        plan = self._plan_skeleton(query)

        # TF-IDF 填充（并行）
        # 修复 P1：防御性 max(1, ...) 避免 sections 为空时 ThreadPoolExecutor(0) 崩溃
        with ThreadPoolExecutor(max(1, min(len(plan.sections), 4))) as pool:
            future_map = {
                pool.submit(self._fill_section, sec, articles, query): sec
                for sec in plan.sections
            }
            for future in as_completed(future_map):
                sec = future_map[future]
                try:
                    sec["content"] = future.result()
                except Exception as e:
                    logger.warning(f"章节填充失败 {sec.get('title', '')}: {e}")
                    sec["content"] = ""

        duration = time.time() - start
        logger.info(f"[Phase 2] 完成: {len(plan.sections)} 个章节, {duration:.1f}s")

        return PassResult(
            phase="structure", status="ok", duration=duration,
            data={"plan": plan},
        )

    def _plan_skeleton(self, query: str) -> DocumentPlan:
        """规划文档骨架（优先 LLM 动态生成，回退到模板）"""
        title = query

        # 尝试 LLM 动态生成骨架
        if self._llm_router:
            try:
                prompt = (
                    f"为主题「{query}」规划一份技术文档的章节结构。\n"
                    f"要求：\n"
                    f"1. 输出 5-7 个章节，覆盖从概述到深入的核心内容\n"
                    f"2. 每个章节需包含：title（章节标题）、prompt（写作提示）、keywords（3-5 个关键词）\n"
                    f"3. 严格按照 JSON 格式输出，不要包含其他文字：\n"
                    f'{{"sections": [{{"title": "...", "prompt": "...", "keywords": ["...", "..."]}}]}}'
                )
                content, _ = self._llm_router.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=2048, temperature=0.3, timeout=45)
                # 提取 JSON
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    plan_data = json.loads(json_match.group())
                    sections = plan_data.get("sections", [])
                    if len(sections) >= 3:
                        logger.info(f"LLM 动态生成骨架: {len(sections)} 个章节")
                        return DocumentPlan(title=title, query=query, sections=sections)
            except Exception as e:
                logger.warning(f"LLM 骨架生成失败，回退到模板: {e}")

        # 回退：硬编码模板
        sections = [
            {"title": "概述", "prompt": f"介绍 {query} 的基本概念和背景", "keywords": ["概述", "简介", "概念", "背景"]},
            {"title": "核心架构", "prompt": f"分析 {query} 的核心架构和设计原理", "keywords": ["架构", "设计", "原理", "组件"]},
            {"title": "关键特性", "prompt": f"列举 {query} 的关键特性和功能", "keywords": ["特性", "功能", "特点", "支持"]},
            {"title": "工作流程", "prompt": f"描述 {query} 的工作流程和数据流", "keywords": ["流程", "工作", "数据流", "处理"]},
            {"title": "实践应用", "prompt": f"说明 {query} 的实践应用和部署", "keywords": ["实践", "应用", "部署", "配置", "使用"]},
            {"title": "总结", "prompt": f"总结 {query} 的要点和最佳实践", "keywords": ["总结", "最佳实践", "建议", "展望"]},
        ]
        return DocumentPlan(title=title, query=query, sections=sections)

    def _fill_section(self, section: dict, articles: list[dict], query: str) -> str:
        """用 TF-IDF 语义匹配从文章提取段落填充章节"""
        keywords = section.get("keywords", [])
        section_title = section.get("title", "")

        scored_paragraphs = []
        for article in articles:
            text = article.get("text", "")
            if not text:
                continue
            # 分段
            paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 50]
            for para in paragraphs:
                score = self._tfidf_score(para, keywords, query)
                if score > 0:
                    scored_paragraphs.append((score, para, article))

        # 取 top 3 段落
        scored_paragraphs.sort(key=lambda x: -x[0])
        top = scored_paragraphs[:3]

        if not top:
            return ""

        parts = []
        seen = set()
        for score, para, article in top:
            para_hash = hash(para[:200])
            if para_hash in seen:
                continue
            seen.add(para_hash)
            parts.append(para)

        return "\n\n".join(parts)

    def _tfidf_score(self, text: str, keywords: list[str], query: str) -> float:
        """简单 TF-IDF 评分"""
        text_lower = text.lower()
        score = 0.0
        for kw in keywords:
            if kw.lower() in text_lower:
                score += 1.0
        # query 中的关键词加权
        query_words = re.findall(r"[a-zA-Z]{3,}|[一-鿿]{2,}", query)
        for w in query_words:
            if w.lower() in text_lower:
                score += 2.0
        return score / max(len(text) / 1000, 1)  # 归一化

    # ─── Phase 3: Refine & Polish ─────────────────

    def phase3_refine(self, plan: DocumentPlan) -> PassResult:
        """Phase 3: LLM 逐章节精修"""
        start = time.time()
        logger.info(f"[Phase 3] 开始精修: {len(plan.sections)} 个章节")

        if not self._llm_router:
            logger.warning("[Phase 3] LLM 路由器不可用，跳过精修")
            return PassResult(phase="refine", status="skip", duration=0.0,
                              data={"plan": plan}, error="LLM 路由器不可用")

        # 并行 LLM 精修
        sections_to_refine = list(enumerate(plan.sections))
        # 修复 P1：防御性 max(1, ...) 避免 sections 为空时 ThreadPoolExecutor(0) 崩溃
        with ThreadPoolExecutor(max(1, min(len(sections_to_refine), 4))) as pool:
            future_map = {
                pool.submit(self._llm_refine_section, sec, plan.query, plan.title): (i, sec)
                for i, sec in sections_to_refine
            }
            for future in as_completed(future_map):
                i, sec = future_map[future]
                try:
                    refined = future.result()
                    if refined:
                        sec["content"] = refined
                        logger.info(f"[Phase 3] 章节 {i+1}/{len(plan.sections)} 精修完成")
                except Exception as e:
                    logger.warning(f"[Phase 3] 章节 {i+1} 精修失败: {e}")

        duration = time.time() - start
        logger.info(f"[Phase 3] 完成: {duration:.1f}s")

        return PassResult(phase="refine", status="ok", duration=duration,
                          data={"plan": plan})

    def _llm_refine_section(self, section: dict, query: str, title: str) -> str:
        """LLM 精修单个章节"""
        section_title = section.get("title", "")
        content = section.get("content", "")
        prompt = section.get("prompt", "")

        messages = [
            {"role": "system", "content": f"你是技术文档专家。正在撰写关于「{query}」的技术文档。"
             f"请精修以下章节内容，使其更加专业、结构清晰。保持 Markdown 格式。"
             f"如果内容为空或不足，请基于你的知识补充。"},
            {"role": "user", "content": f"## {section_title}\n\n原始内容:\n{content}\n\n"
             f"要求: {prompt}\n\n请输出精修后的 Markdown 内容（仅内容，不要标题）:"},
        ]

        result, provider = self._llm_router.chat(messages, max_tokens=2048, temperature=0.3)
        return result

    # ─── 主入口 ──────────────────────────────────

    def generate(self, query: str, output_path: str = None,
                 max_search_results: int = 20) -> dict:
        """执行三阶段文档生成

        Args:
            query: 文档主题
            output_path: 输出文件路径（None 则返回内容不写文件）
            max_search_results: 最大搜索结果数

        Returns:
            {"status", "duration", "phases", "output_path", "content"}
        """
        start = time.time()
        logger.info(f"三阶段文档生成开始: {query}")

        results = {}

        # Phase 1
        p1 = self.phase1_research(query, max_search_results)
        results["phase1"] = p1
        if not p1.is_ok:
            return {"status": "error", "duration": time.time() - start,
                    "error": f"Phase 1 失败: {p1.error}"}

        # Phase 2
        articles = p1.data.get("articles", [])
        p2 = self.phase2_structure(query, articles)
        results["phase2"] = p2
        if not p2.is_ok:
            return {"status": "error", "duration": time.time() - start,
                    "error": f"Phase 2 失败: {p2.error}"}

        # Phase 3
        plan = p2.data.get("plan")
        p3 = self.phase3_refine(plan)
        results["phase3"] = p3

        # 生成最终文档
        content = plan.to_markdown()

        # 写文件
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"文档已写入: {output_path}")

        duration = time.time() - start
        logger.info(f"三阶段文档生成完成: {duration:.1f}s")

        return {
            "status": "ok",
            "duration": duration,
            "phases": {
                "research": {"status": p1.status, "duration": p1.duration},
                "structure": {"status": p2.status, "duration": p2.duration},
                "refine": {"status": p3.status, "duration": p3.duration},
            },
            "output_path": str(output_path) if output_path else None,
            "content_length": len(content),
            "section_count": len(plan.sections),
        }
