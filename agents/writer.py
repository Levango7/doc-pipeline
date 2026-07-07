"""
Writer Agent v2 - 增强型内容整合插件
==================================
改进点：
  - 多模板支持（可配置模板）
  - 智能章节分类（NLP 关键词提取）
  - 内容去重和合并
  - 自动目录生成
  - 引用追踪
  - 文档骨架规划 + 从完整文章填充内容（配合 fetcher）
  - TF-IDF 向量语义匹配（替代关键词计数）
"""
import json
import os
import time
import re
import hashlib
import urllib.request
import urllib.error
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.base_agent import BaseAgent, Message, AgentStatus, AgentMeta


AGENT_NAME = "writer"
AGENT_VERSION = "2.0"
AGENT_DESC = "增强型内容整合 Agent - 多模板、智能分类、引用追踪、TF-IDF 语义匹配"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 30
INPUT_TOPICS = ["writer.input", "researcher.done", "researcher.partial", "fetcher.done"]
OUTPUT_TOPICS = ["writer.done", "writer.progress"]
DEPENDENCIES = ["researcher"]
CACHE_TTL = 0
RESPAWN = False


@dataclass
class ContentChunk:
    """内容块"""
    title: str
    content: str
    source: str
    url: str
    section: str = ""
    keywords: list = field(default_factory=list)
    relevance_score: float = 0.0


class TemplateManager:
    """模板管理器"""

    def __init__(self):
        self._templates = {
            "default": {
                "header": "# {title}\n\n> 生成时间: {time}\n\n",
                "section": "## {section_title}\n\n{content}\n\n",
                "footer": "\n---\n\n## 参考资料\n\n{references}\n",
            }
        }

    def render(self, template_name: str, title: str, sections: list,
               references: list, **kwargs) -> str:
        template = self._templates.get(template_name, self._templates["default"])
        parts = [template["header"].format(title=title, time=time.strftime('%Y-%m-%d %H:%M:%S'))]
        for sec in sections:
            parts.append(template["section"].format(
                section_title=sec.get("title", ""), content=sec.get("content", "")))
        refs = "\n".join(f"- [{r['title']}]({r['url']})" for r in references) if references else ""
        parts.append(template["footer"].format(references=refs))
        return "\n".join(parts)


class WriterAgent(BaseAgent):
    """增强型内容整合 Agent"""

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self.pending_results: dict = {}
        self._template_mgr = TemplateManager()
        self._pending_expire_secs = config.get("pending_expire_secs", 300)
        self._section_scores = {
            "技术教程": ["教程", "指南", "入门", "安装", "配置", "使用"],
            "概念原理": ["概念", "原理", "机制", "架构", "设计", "模式"],
            "实践应用": ["实践", "应用", "案例", "示例", "场景", "项目"],
            "对比分析": ["对比", "区别", "比较", "优缺点", "vs", "versus"],
            "最佳实践": ["最佳实践", "建议", "技巧", "推荐", "优化"],
        }
        self.log_info(f"Writer v{AGENT_VERSION} 初始化完成")

        # LLM 润色配置（硅基流动 DeepSeek V4 Flash）
        self._llm_api_url = config.get("llm_api_url",
            "https://api.siliconflow.cn/v1/chat/completions")
        self._llm_api_key = config.get("llm_api_key",
            os.environ.get("SILICONFLOW_API_KEY", ""))
        self._llm_model = config.get("llm_model", "deepseek-ai/DeepSeek-V4-Flash")
        if self._llm_api_key:
            self.log_info("LLM 润色已启用")

        # 润色缓存
        self._polish_cache: dict[str, tuple[float, str]] = {}  # key -> (expire_ts, content)
        self._polish_cache_ttl = config.get("polish_cache_ttl", 3600)  # 1h 缓存

        # 规则过渡句模板（0 成本兜底）
        self._transition_templates = [
            ("简介", "核心概念", "了解了基本概念后，下面我们来深入分析其核心原理。"),
            ("核心概念", "详细分析", "理解了核心概念后，我们进一步探讨具体的技术细节。"),
            ("核心概念", "实践", "理论之外，实践应用同样值得关注。"),
            ("详细分析", "实践", "上面的分析为我们打下了基础，接下来看看实际应用。"),
            ("详细分析", "实践与应用", "技术原理之外，实际应用场景同样重要。"),
            ("实践", "总结", "综合以上讨论，我们可以做出以下总结。"),
            ("实践与应用", "总结", "回顾以上的实践内容，可以得出以下结论。"),
            ("", "", "接下来，我们继续探讨相关内容。"),  # 通用回退
        ]

    def _polish_with_llm(self, content: str, query: str) -> str:
        """用 LLM 润色文档段落衔接（含缓存+质量门控+分段+规则兜底）"""
        if not self._llm_api_key or not content.strip():
            return content

        # ── Layer 4: 缓存检查 ──
        cache_key = hashlib.md5((content[:200] + query).encode()).hexdigest()
        now = time.time()
        if cache_key in self._polish_cache:
            expire_ts, cached = self._polish_cache[cache_key]
            if now < expire_ts:
                self.log_info("LLM 润色缓存命中，跳过")
                return cached

        # ── Layer 1: 质量门控 ──
        # 如果文档已完整（3+ 章节、有引用、500+ 字），跳过 LLM
        section_count = len(re.findall(r'^##\s+\S+', content, re.MULTILINE))
        ref_count = len(re.findall(r'\[([^\]]+)\]\(https?://[^)]+\)', content))
        if section_count >= 3 and ref_count >= 1 and len(content) >= 500:
            self.log_info(f"质量门控跳过 LLM ({section_count}章, {ref_count}引用, {len(content)}字)")
            return content

        # ── Layer 2: 分段润色 ──
        segments = re.split(r'\n(?=##\s)', content)
        polished_segments = []

        for i, seg in enumerate(segments):
            seg = seg.strip()
            if not seg or len(seg) < 100:
                polished_segments.append(seg)
                continue

            # 段落间插规则过渡句（Layer 3）
            if i > 0:
                prev_heading = re.search(r'^##\s+(.+)', segments[i - 1], re.MULTILINE)
                curr_heading = re.search(r'^##\s+(.+)', seg, re.MULTILINE)
                prev = prev_heading.group(1).strip() if prev_heading else ""
                curr = curr_heading.group(1).strip() if curr_heading else ""

                # 找匹配的过渡模板
                transition = ""
                for t_prev, t_curr, t_text in self._transition_templates:
                    if (t_prev in prev or prev in t_prev) and (t_curr in curr or curr in t_curr):
                        transition = t_text
                        break
                if not transition:
                    for tp, tc, tt in self._transition_templates:
                        if not tp and not tc:
                            transition = tt  # 通用回退
                            break

                if transition:
                    polished_segments.append(f"> {transition}\n")

            # 小段 fallback: LLM 润色
            seg_prompt = f"润色以下段落，改善语句流畅性，保持原意不变：\n\n{seg[:2000]}"
            data = json.dumps({
                "model": self._llm_model,
                "messages": [{"role": "user", "content": seg_prompt}],
                "max_tokens": 1024,
                "temperature": 0.3,
            }).encode()
            req = urllib.request.Request(
                self._llm_api_url, data=data,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self._llm_api_key}"})
            try:
                t0 = time.time()
                resp = urllib.request.urlopen(req, timeout=30)
                body = json.loads(resp.read().decode())
                polished = body["choices"][0]["message"]["content"]
                polished_segments.append(polished)
                self.log_debug(f"  段{i}: {len(seg)}→{len(polished)} 字, {time.time()-t0:.1f}s")
            except Exception as e:
                self.log_debug(f"  段{i} 润色失败, 保留原文")
                polished_segments.append(seg)

        result = "\n\n".join(polished_segments)
        elapsed = time.time() - now

        # 写入缓存
        self._polish_cache[cache_key] = (now + self._polish_cache_ttl, result)
        self.log_info(f"LLM 润色完成 ({len(segments)}段, {elapsed:.1f}s, {len(content)}→{len(result)} 字)")
        return result

    def handle(self, msg: Message) -> dict | None:
        """处理整合请求（支持 fetcher 文章 + 文档骨架规划）"""
        self.report(AgentStatus.RUNNING, "开始整合内容...")

        payload = msg.payload
        task_id = payload.get("task_id", "")
        template_name = payload.get("template", "default")
        title = payload.get("title", "自动生成文档")
        query = payload.get("query", "")
        if not query:
            queries = payload.get("queries", [])
            query = queries[0] if queries else ""
        if not query:
            # 从输入文件取首行非注释内容作为主题
            input_file = payload.get("input_file", "")
            if input_file and os.path.exists(input_file):
                try:
                    with open(input_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                query = line[:80]
                                break
                except Exception:
                    pass

        self._expire_stale_pending()

        # ── 优先使用 fetcher 的完整文章 ──
        articles = payload.get("articles", [])
        if articles:
            return self._build_from_articles(articles, query, title, template_name, task_id)

        # ── 次选：搜索摘要（旧模式） ──
        results = payload.get("results", [])
        if not results:
            results = self.pending_results.get(task_id, [])

        if not results:
            return {
                "status": "ok",
                "task_id": task_id,
                "message": "无待整合内容，生成占位文档",
                "content": f"# {title}\n\n> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n未采集到可整合的搜索结果。\n",
            }

        self.log_info(f"任务 {task_id}: 整合 {len(results)} 条搜索摘要")
        chunks = self._to_chunks(results)
        chunks = self._classify_chunks(chunks)
        chunks = self._deduplicate_chunks(chunks)
        content = self._generate_document(chunks, template_name, title)
        self.pending_results.pop(task_id, None)

        self.report(AgentStatus.RUNNING, f"整合完成，生成 {len(content)} 字符")
        return {
            "status": "ok",
            "task_id": task_id,
            "content": content,
            "stats": {
                "total_results": len(results),
                "unique_chunks": len(chunks),
                "sections": len(set(c.section for c in chunks)),
                "word_count": len(content),
                "char_count": len(content),
            }
        }

    # ═══════════════════════════════════════════════
    # 新流程：文章 → 骨架 → TF-IDF 填充
    # ═══════════════════════════════════════════════

    def _build_from_articles(self, articles: list[dict], query: str,
                              title: str, template_name: str,
                              task_id: str) -> dict:
        """从完整文章构建文档（先骨架，再填内容）"""
        self.log_info(f"任务 {task_id}: 从 {len(articles)} 篇文章构建文档")

        # Step 1: 读取所有本地文章内容
        article_contents = []
        for art in articles:
            local_path = art.get("local_path", "")
            if local_path and os.path.exists(local_path):
                try:
                    with open(local_path, "r", encoding="utf-8") as f:
                        text = f.read()
                    article_contents.append({
                        "title": art.get("title", ""),
                        "url": art.get("url", ""),
                        "source": art.get("source", ""),
                        "text": text,
                    })
                except Exception:
                    continue

        self.log_info(f"  成功读取 {len(article_contents)}/{len(articles)} 篇文章")

        # Step 2: 创建文档骨架
        skeleton = self._plan_skeleton(query, title, article_contents)

        # Step 3: 构建最终文档
        content_parts = [f"# {title}", ""]
        content_parts.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if query:
            content_parts.append(f"> 主题: {query}")
        content_parts.append("")

        # 目录
        content_parts.append("## 目录")
        content_parts.append("")
        for sec in skeleton:
            content_parts.append(f"1. [{sec['heading']}](#{sec['anchor']})")
        content_parts.append("")
        content_parts.append("---")
        content_parts.append("")

        # 每个章节填充内容（TF-IDF 语义匹配）
        all_refs = []
        for sec in skeleton:
            content_parts.append(f"## {sec['heading']}")
            content_parts.append("")
            filled = self._fill_section(sec, article_contents)
            if filled["paragraphs"]:
                for para in filled["paragraphs"]:
                    content_parts.append(para)
                    content_parts.append("")
            else:
                content_parts.append("*（暂无可用的相关内容）*")
                content_parts.append("")
            all_refs.extend(filled["references"])
            content_parts.append("---")
            content_parts.append("")

        # 参考资料
        if all_refs:
            seen_refs = set()
            content_parts.append("## 参考资料")
            content_parts.append("")
            for ref in all_refs:
                if ref["url"] not in seen_refs:
                    seen_refs.add(ref["url"])
                    content_parts.append(f"- [{ref['title']}]({ref['url']})")
            content_parts.append("")

        content = "\n".join(content_parts)

        # LLM 润色段落衔接
        if query:
            polished = self._polish_with_llm(content, query)
        else:
            polished = content

        self.report(AgentStatus.RUNNING, f"骨架+TF-IDF填充完成，{len(skeleton)} 章节，{len(polished)} 字符")

        return {
            "status": "ok",
            "task_id": task_id,
            "content": polished,
            "stats": {
                "articles_used": len(article_contents),
                "sections": len(skeleton),
                "word_count": len(content),
                "char_count": len(content),
            }
        }

    def _plan_skeleton(self, query: str, title: str,
                       articles: list[dict]) -> list[dict]:
        """根据查询和文章内容规划文档骨架"""
        if not query:
            return [
                {"heading": "概述", "anchor": "概述", "keywords": []},
                {"heading": "详细内容", "anchor": "详细内容", "keywords": []},
                {"heading": "总结", "anchor": "总结", "keywords": []},
            ]

        query_kws = re.findall(r'[\w\u4e00-\u9fff]+', query)
        skeleton = []

        skeleton.append({
            "heading": "简介",
            "anchor": "简介",
            "keywords": query_kws[:3] + ["介绍", "概述", "什么是"],
        })

        core_kws = [kw for kw in query_kws if len(kw) > 1 and kw not in ("的", "是", "在", "了")]
        if core_kws:
            skeleton.append({
                "heading": "核心概念",
                "anchor": "核心概念",
                "keywords": core_kws + ["概念", "定义", "原理", "机制"],
            })

        # 从文章中自动提取主题
        content_kws = set()
        for art in articles:
            text = art.get("text", "")
            paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 40]
            for p in paras[:30]:
                if any(kw.lower() in p.lower() for kw in query_kws):
                    extra = re.findall(r'[\w\u4e00-\u9fff]{2,}', p)
                    content_kws.update(extra[:5])
        if content_kws:
            skeleton.append({
                "heading": "详细分析",
                "anchor": "详细分析",
                "keywords": list(content_kws)[:8],
            })

        skeleton.append({
            "heading": "实践与应用",
            "anchor": "实践与应用",
            "keywords": ["实践", "应用", "示例", "用法", "教程", "案例"],
        })
        skeleton.append({
            "heading": "总结",
            "anchor": "总结",
            "keywords": ["总结", "结论", "未来", "展望", "发展趋势"],
        })

        return skeleton

    def _fill_section(self, section: dict,
                      articles: list[dict]) -> dict:
        """从文章中提取与章节相关的段落（TF-IDF 语义匹配）"""
        keywords = section.get("keywords", [])
        paragraphs = []

        for art in articles:
            text = art.get("text", "")
            title = art.get("title", "")
            url = art.get("url", "")

            raw_paras = re.split(r'\n\s*\n', text)
            for para in raw_paras:
                p = para.strip()
                if len(p) < 40:
                    continue
                if len(p) > 3000:
                    p = p[:3000] + "..."
                if re.search(r'^(navigation|menu|footer|copyright|©|广告|推荐|热门|相关文章)',
                             p, re.IGNORECASE):
                    continue
                paragraphs.append({"text": p, "title": title, "url": url})

        if not paragraphs:
            return {"paragraphs": [], "references": []}

        # TF-IDF 向量语义匹配
        scored = self._semantic_rank(paragraphs, keywords)
        top = scored[:8]

        result_paras = [p["text"] for p in top]
        result_refs = []
        seen_urls = set()
        for p in top:
            if p["url"] and p["url"] not in seen_urls:
                seen_urls.add(p["url"])
                result_refs.append({"title": p["title"], "url": p["url"]})

        return {"paragraphs": result_paras, "references": result_refs}

    def _semantic_rank(self, paragraphs: list[dict],
                       keywords: list[str]) -> list[dict]:
        """用 TF-IDF + 余弦相似度对段落排序（纯 NumPy 实现）"""
        if not keywords:
            return paragraphs[:8]

        # 1. 分词
        all_docs = []
        for p in paragraphs:
            words = re.findall(r'[\w\u4e00-\u9fff]{2,}', p["text"].lower())
            all_docs.append(words)

        query_words = [kw.lower() for kw in keywords if len(kw) > 1]

        # 2. 构建词汇表
        vocab_set = set()
        for doc in all_docs:
            vocab_set.update(doc)
        vocab = sorted(vocab_set)
        if not vocab:
            return paragraphs[:8]
        vocab_idx = {w: i for i, w in enumerate(vocab)}
        n_docs = len(all_docs)
        n_terms = len(vocab)

        # 3. TF-IDF 矩阵
        import numpy as np
        tfidf = np.zeros((n_docs, n_terms), dtype=np.float32)

        for i, doc in enumerate(all_docs):
            if not doc:
                continue
            tf_counter = {}
            for w in doc:
                tf_counter[w] = tf_counter.get(w, 0) + 1
            max_tf = max(tf_counter.values()) if tf_counter else 1
            for w, cnt in tf_counter.items():
                if w in vocab_idx:
                    tfidf[i, vocab_idx[w]] = cnt / max_tf

        # IDF
        df = np.zeros(n_terms, dtype=np.float32)
        for i in range(n_docs):
            df += (tfidf[i] > 0).astype(np.float32)
        idf = np.log((n_docs + 1) / (df + 1)) + 1
        tfidf *= idf.reshape(1, -1)

        # 4. 查询向量
        query_vec = np.zeros(n_terms, dtype=np.float32)
        for w in query_words:
            if w in vocab_idx:
                query_vec[vocab_idx[w]] = 1.0
        query_norm = np.linalg.norm(query_vec)
        if query_norm > 0:
            query_vec = query_vec / query_norm

        # 5. 余弦相似度
        doc_norms = np.linalg.norm(tfidf, axis=1, keepdims=True)
        doc_norms[doc_norms == 0] = 1
        tfidf_normed = tfidf / doc_norms
        similarities = tfidf_normed @ query_vec

        # 6. 排序
        sorted_indices = np.argsort(-similarities)
        scored = []
        for idx in sorted_indices:
            score = float(similarities[idx])
            if score > 0.01:
                scored.append({**paragraphs[idx], "score": score})

        if not scored:
            return paragraphs[:3]  # 兜底：返回前 3 段

        return scored

    # ═══════════════════════════════════════════════
    # 原有逻辑：搜索摘要模式
    # ═══════════════════════════════════════════════

    def _to_chunks(self, results: list[dict]) -> list[ContentChunk]:
        """转换为内容块"""
        chunks = []
        for r in results:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            url = r.get("url", "")
            source = r.get("source", "")
            keywords = self._extract_keywords(title + " " + snippet)
            chunks.append(ContentChunk(
                title=title, content=snippet, source=source,
                url=url, keywords=keywords,
            ))
        return chunks

    def _extract_keywords(self, text: str) -> list[str]:
        """提取关键词（简单实现）"""
        text = re.sub(r'[^\w\s]', ' ', text)
        words = text.lower().split()
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                     "的", "是", "在", "和", "了", "有", "与", "及", "等"}
        word_freq = defaultdict(int)
        for word in words:
            if len(word) > 2 and word not in stopwords:
                word_freq[word] += 1
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        return [w for w, _ in sorted_words[:10]]

    def _classify_chunks(self, chunks: list[ContentChunk]) -> list[ContentChunk]:
        """将内容块分类到合适的章节"""
        for chunk in chunks:
            text = (chunk.title + " " + chunk.content).lower()
            best_section = "其他"
            best_score = 0
            for section, kws in self._section_scores.items():
                score = sum(1 for kw in kws if kw.lower() in text)
                if score > best_score:
                    best_score = score
                    best_section = section
            chunk.section = best_section
            chunk.relevance_score = best_score
        return chunks

    def _deduplicate_chunks(self, chunks: list[ContentChunk]) -> list[ContentChunk]:
        """去重"""
        seen = set()
        unique = []
        for chunk in chunks:
            key = chunk.title.strip().lower()[:50]
            if key and key not in seen:
                seen.add(key)
                unique.append(chunk)
        return unique

    def _generate_document(self, chunks: list[ContentChunk],
                           template_name: str, title: str) -> str:
        """生成文档"""
        sections = defaultdict(list)
        for chunk in chunks:
            sections[chunk.section].append(chunk)

        section_data = []
        all_refs = []
        for sec_title in ["概念原理", "技术教程", "实践应用", "对比分析", "最佳实践", "其他"]:
            sec_chunks = sections.get(sec_title, [])
            if not sec_chunks:
                continue
            sec_content_parts = []
            for c in sec_chunks:
                sec_content_parts.append(f"### {c.title}\n\n{c.content}")
                if c.url and c.title:
                    all_refs.append({"title": c.title, "url": c.url})
            section_data.append({"title": sec_title, "content": "\n\n".join(sec_content_parts)})

        return self._template_mgr.render(template_name, title, section_data, all_refs)

    def _expire_stale_pending(self):
        """清理过期的 pending 结果"""
        now = time.time()
        stale = [tid for tid, (ts, _) in self.pending_results.items()
                 if now - ts > self._pending_expire_secs]
        for tid in stale:
            self.pending_results.pop(tid, None)