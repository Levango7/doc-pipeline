"""
Researcher Agent v2 - 增强型内容检索插件
======================================
改进点：
  - 多搜索引擎支持（可配置）
  - 智能缓存策略（LRU + TTL）
  - 搜索结果质量评分
  - 并行搜索控制（并发限制）
  - 搜索历史记录
"""
import json
import os
import time
import hashlib
import re
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from dataclasses import dataclass, field

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.base_agent import BaseAgent, Message, AgentStatus, AgentMeta


AGENT_NAME = "researcher"
AGENT_VERSION = "2.0"
AGENT_DESC = "增强型内容检索 Agent - 多引擎、智能缓存、质量评分"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 10
INPUT_TOPICS = ["researcher.input", "researcher.search"]
OUTPUT_TOPICS = ["researcher.done", "researcher.progress", "researcher.partial"]
DEPENDENCIES = []
CACHE_TTL = 86400  # 24小时
RESPAWN = True
EXTRACTS_QUERIES = True
RESULTS_MERGE = "extend"


@dataclass
class SearchResult:
    """搜索结果"""
    title: str
    url: str
    snippet: str
    source: str
    query: str
    score: float = 0.0  # 质量评分
    relevance: float = 0.0  # query 相关性评分
    fetched_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "query": self.query,
            "score": self.score,
            "relevance": self.relevance,
            "fetched_at": self.fetched_at,
        }


class LRUCache:
    """LRU 缓存（带 TTL）—— 基于 OrderedDict，O(1) 淘汰"""

    def __init__(self, max_size: int = 1000, ttl: int = 86400):
        self.max_size = max_size
        self.ttl = ttl
        from collections import OrderedDict
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[list]:
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            if time.time() - entry["ts"] > self.ttl:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return entry["data"]

    def set(self, key: str, data: list):
        with self._lock:
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = {"data": data, "ts": time.time()}

    def _remove(self, key: str):
        self._cache.pop(key, None)

    def clear(self):
        with self._lock:
            self._cache.clear()


class ResearcherAgent(BaseAgent):
    """增强型内容检索 Agent"""

    DEFAULT_SPAM_DOMAINS = {
        "yuanbao.tencent.com", "wb.admaster.com.cn", "clickc.admaster.com.cn",
        "admaster.com.cn", "adsensor.org", "duomai.com", "tanx.com",
        "ex.bidswitch.com", "ib.adnxs.com", "nexage.com", "pubmatic.com",
        "adzerk.net", "doubleclick.net", "googleadservices.com",
        "googlesyndication.com", "amazon-adsystem.com",
    }

    DEFAULT_LOW_QUALITY_DOMAINS = set()

    DEFAULT_PROSEARCH_PATHS = [
        r"F:\Program Files\QClaw\resources\openclaw\config\skills\online-search\scripts\prosearch.cjs",
        r"F:\Program Files (x86)\qclaw\resources\openclaw\config\skills\online-search\scripts\prosearch.cjs",
    ]

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        
        cache_size = config.get("cache_size", 1000)
        self._cache = LRUCache(max_size=cache_size, ttl=self.meta.cache_ttl or CACHE_TTL)
        
        self.dedup_set: set = set()
        self._dedup_lock = threading.Lock()
        
        self._search_history: list[dict] = []
        self._history_lock = threading.Lock()
        self._max_history = config.get("max_history", 100)
        
        self._max_workers = config.get("max_workers", 3)
        
        self._search_engines = config.get("search_engines", ["bing", "sogou", "360"])
        
        from pipeline_core.rate_limiter import RateLimiterRegistry
        self._rate_limiters = RateLimiterRegistry()
        
        self._min_score = config.get("min_score", 0.25)

        self._spam_domains = set(config.get("spam_domains", [])) or self.DEFAULT_SPAM_DOMAINS
        config_lq = config.get("low_quality_domains", None)
        if config_lq is not None:
            self._low_quality_domains = set(config_lq)
        else:
            self._low_quality_domains = set(self.DEFAULT_LOW_QUALITY_DOMAINS)

        self._tracking_url_patterns = config.get("tracking_url_patterns", [
            "/evt/", "/click/", "/rd/", "/goto?", "redirect?",
            "adclick", "adredirect", "union", "trace",
        ])

        self._prosearch_paths = []
        env_path = os.environ.get("PROSEARCH_SCRIPT_PATH", "")
        if env_path:
            self._prosearch_paths.append(env_path)
        config_paths = config.get("prosearch_paths", [])
        if config_paths:
            self._prosearch_paths.extend(config_paths)
        self._prosearch_paths.extend(self.DEFAULT_PROSEARCH_PATHS)

        self._title_min_pattern = re.compile(r"[一-鿿]{2,}|[a-zA-Z]{3,}")
        
        self.log_info(f"Researcher v{AGENT_VERSION} 初始化完成 (engines={self._search_engines})")

    def handle(self, msg: Message) -> dict | None:
        """处理检索请求"""
        self.report(AgentStatus.RUNNING, "开始检索...")
        
        payload = msg.payload
        task_id = payload.get("task_id", "")
        queries = payload.get("queries", [])
        parallel = payload.get("parallel", True)
        max_results = payload.get("max_results", 50)

        # 从流水线配置覆盖运行时参数
        cfg = payload.get("config", {})
        engines_used = self._search_engines
        if isinstance(cfg, dict):
            if "search_engines" in cfg:
                engines_used = cfg["search_engines"]
            if "max_results" in cfg:
                max_results = cfg["max_results"]
        
        if not queries:
            return {"status": "ok", "task_id": task_id, "total": 0,
                    "results": [], "query_count": 0, "engines_used": engines_used}

        # 清洗 query：过滤噪音/验证性/无意义的行
        queries = self._clean_queries(queries)
        # 清洗 query 文本：去 markdown 格式、序号前缀
        queries = [self._clean_query_text(q) for q in queries]
        # 去重清洗后的查询
        queries = list(dict.fromkeys(q for q in queries if q))
        if not queries:
            return {"status": "ok", "task_id": task_id, "total": 0,
                    "results": [], "query_count": 0, "engines_used": engines_used}

        self.log_info(f"任务 {task_id}: {len(queries)} 个查询")
        
        results = []
        
        if parallel and len(queries) > 1:
            results = self._parallel_search(queries, task_id, max_results, engines_used)
        else:
            for i, query in enumerate(queries):
                self.report(AgentStatus.RUNNING, f"[{i+1}/{len(queries)}] {query[:40]}...")
                try:
                    r = self._search(query, task_id, engines_used)
                    results.extend(r)
                except Exception as e:
                    self.log_error(f"搜索失败 {query}: {e}")
        
        results = self._score_and_filter(results)
        
        results = self._deduplicate(results)
        
        results = results[:max_results]
        
        self._record_history(task_id, queries, len(results))
        
        self.report(AgentStatus.RUNNING, f"检索完成，共 {len(results)} 条结果")
        self.log_info(f"最终返回 {len(results)} 条搜索结果")
        
        return {
            "status": "ok",
            "task_id": task_id,
            "total": len(results),
            "results": [r.to_dict() for r in results],
            "query_count": len(queries),
            "engines_used": engines_used,
        }

    def _parallel_search(self, queries: list[str], task_id: str, max_results: int, engines: list[str]) -> list[SearchResult]:
        """并行搜索"""
        results = []
        completed = 0
        
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_query = {
                executor.submit(self._search, query, task_id, engines): query 
                for query in queries
            }
            
            for future in as_completed(future_to_query):
                query = future_to_query[future]
                completed += 1
                self.report(AgentStatus.RUNNING, f"[{completed}/{len(queries)}] 完成: {query[:40]}...")
                
                try:
                    r = future.result()
                    results.extend(r)
                    
                    # 发送部分结果（流式）
                    if completed % 3 == 0:
                        self.publish("researcher.partial", {
                            "task_id": task_id,
                            "completed": completed,
                            "total": len(queries),
                            "partial_count": len(results),
                        })
                        
                except Exception as e:
                    self.log_error(f"并行搜索失败 {query}: {e}")
        
        return results

    def _search(self, query: str, task_id: str, engines: list[str] = None) -> list[SearchResult]:
        """执行单次搜索（含缓存）"""
        if engines is None:
            engines = self._search_engines
        cache_key = f"{task_id}:{query}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.log_debug(f"缓存命中: {query[:40]}")
            return [SearchResult(**r) for r in cached]

        all_results = []

        for engine in engines:
            try:
                # 每引擎独立限流
                rate_cfg = {"duckduckgo": (3, 5), "prosearch": (5, 10), "mock": (100, 100),
                            "sogou": (10, 20), "360": (10, 20)}
                rate, burst = rate_cfg.get(engine, (10, 20))
                limiter = self._rate_limiters.get_or_create(
                    f"search_{engine}", rate=rate, burst=burst,
                )
                if not limiter.acquire(block=True, timeout=30):
                    self.log_warning(f"{engine} 限流等待超时，跳过")
                    continue

                if engine == "prosearch":
                    results = self._prosearch(query)
                elif engine == "duckduckgo":
                    results = self._duckduckgo_search(query)
                elif engine == "bing":
                    results = self._bing_search(query)
                elif engine == "sogou":
                    results = self._sogou_search(query)
                elif engine == "360":
                    results = self._360_search(query)
                else:
                    results = self._mock_search(query, engine)
                all_results.extend(results)
            except Exception as e:
                self.log_error(f"{engine} 搜索失败: {e}")

        # 缓存结果
        self._cache.set(cache_key, [r.to_dict() for r in all_results])

        return all_results

    def _prosearch(self, query: str) -> list[SearchResult]:
        """调用元宝搜索"""
        import subprocess
        
        script = None
        for path in self._prosearch_paths:
            if Path(path).exists():
                script = path
                break
        
        if not script:
            self.log_debug("prosearch.cjs 未找到，跳过 prosearch 引擎")
            return []
        
        try:
            result = subprocess.run(
                ["node", script, json.dumps({"keyword": query})],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return self._normalize_results(data, query)
        except Exception as e:
            self.log_error(f"prosearch 调用失败: {e}")
        
        return []

    def _html_search(self, query: str, engine_name: str, url: str,
                     params: dict, skip_domains: list[str],
                     max_results: int = 10) -> list[SearchResult]:
        """通用 HTML 搜索引擎抓取（Bing/Sogou/360 共用）"""
        import re, requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            with requests.get(url, params=params, headers=headers, timeout=10) as r:
                if r.status_code != 200:
                    self.log_warning(f"{engine_name} 返回 {r.status_code}")
                    return []
                html_text = r.text
            results = []
            seen_urls = set()
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', html_text):
                url_found = m.group(1)
                if url_found in seen_urls:
                    continue
                seen_urls.add(url_found)
                if any(d in url_found for d in skip_domains):
                    continue
                if any(url_found.endswith(ext) for ext in (".css", ".js", ".svg", ".png", ".ico")):
                    continue
                start = max(0, m.start() - 200)
                ctx = html_text[start:m.end()]
                title_match = re.search(r'(?:title|aria-label)="([^"]+)"', ctx)
                title = title_match.group(1) if title_match else url_found.split("/")[-1][:60]
                title = re.sub(r'<[^>]+>', '', title).strip()
                snippet_start = m.end()
                snippet_end = min(snippet_start + 500, len(html_text))
                snippet = re.sub(r'<[^>]+>', '', html_text[snippet_start:snippet_end]).strip()[:200]
                results.append(SearchResult(
                    title=title[:120] if len(title) > 120 else title,
                    url=url_found,
                    snippet=snippet,
                    source=engine_name,
                    query=query,
                    score=0.8,
                ))
                if len(results) >= max_results:
                    break
            self.log_info(f"{engine_name} [{query[:30]}]: {len(results)} 条结果")
            return results
        except Exception as e:
            self.log_warning(f"{engine_name} 搜索失败 ({e})，返回空结果")
            return []

    def _bing_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 Bing 搜索 — 只从 b_algo 容器提取真实搜索结果"""
        import re, requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            with requests.get("https://cn.bing.com/search",
                             params={"q": query, "mkt": "zh-CN"},
                             headers=headers, timeout=10) as r:
                if r.status_code != 200:
                    self.log_warning(f"Bing 返回 {r.status_code}")
                    return []
                html_text = r.text

            results = []
            seen_urls = set()
            for algo in re.finditer(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                                    html_text, re.DOTALL):
                block = algo.group(1)
                link = re.search(r'href="(https?://[^"]+)"', block)
                if not link:
                    continue
                url = link.group(1)
                if any(d in url for d in ("bing.com", "microsoft.com", "live.com",
                                           "w3.org", "schema.org")):
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                title_m = re.search(r'<h2[^>]*>(.*?)</h2>', block, re.DOTALL)
                title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""

                snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()[:200] if snippet_m else ""

                results.append(SearchResult(
                    title=title[:120], url=url, snippet=snippet,
                    source="bing", query=query, score=0.8,
                ))
                if len(results) >= max_results:
                    break

            self.log_info(f"Bing [{query[:30]}]: {len(results)} 条结果")
            return results
        except Exception as e:
            self.log_warning(f"Bing 搜索失败 ({e})，返回空结果")
            return []

    def _sogou_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 Sogou 搜索（搜狗，国内可访问，免费，无需 API Key）"""
        return self._html_search(
            query=query,
            engine_name="sogou",
            url="https://www.sogou.com/web",
            params={"query": query},
            skip_domains=["sogou.com", "weixin.sogou.com", "w3.org", "schema.org"],
            max_results=max_results,
        )

    def _360_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 360 搜索（好搜，国内可访问，免费，无需 API Key）"""
        return self._html_search(
            query=query,
            engine_name="360",
            url="https://www.so.com/s",
            params={"q": query},
            skip_domains=["so.com", "360.cn", "haosou.com", "w3.org", "schema.org"],
            max_results=max_results,
        )


    def _duckduckgo_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 DuckDuckGo 实时搜索（免费，无需 API Key）"""
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                from duckduckgo_search import DDGS
                results = []
                with DDGS(timeout=10) as ddgs:
                    for r in ddgs.text(query, max_results=max_results):
                        results.append(SearchResult(
                            title=r.get("title", ""),
                            url=r.get("href", ""),
                            snippet=r.get("body", ""),
                            source="duckduckgo",
                            query=query,
                            score=0.7,
                        ))
                if not results:
                    self.log_info(f"DuckDuckGo 返回空结果: {query[:40]}")
                self.log_info(f"DuckDuckGo [{query[:30]}]: {len(results)} 条结果")
                return results
            except ImportError:
                self.log_warning("duckduckgo_search 未安装，返回空结果")
                return []
            except Exception as e:
                self.log_warning(f"DuckDuckGo 不可用 ({e})，返回空结果")
                return []

    def _mock_search(self, query: str, engine: str) -> list[SearchResult]:
        """模拟搜索（用于测试）"""
        return [
            SearchResult(
                title=f"{engine} 搜索结果: {query}",
                url=f"https://example.com/search?q={query}",
                snippet=f"这是 {engine} 的搜索结果摘要...",
                source=engine,
                query=query,
                score=0.5,
            )
        ]

    def _normalize_results(self, data: dict, query: str) -> list[SearchResult]:
        """标准化搜索结果"""
        results = []
        items = data.get("results", []) or data if isinstance(data, list) else []
        
        for item in items[:20]:  # 最多20条
            if isinstance(item, dict):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", item.get("content", "")),
                    source=item.get("source", ""),
                    query=query,
                ))
        
        return results

    def _clean_queries(self, queries: list[str]) -> list[str]:
        """清洗查询：过滤噪音/验证性/无实质意义的行"""
        import re
        cleaned = []
        noise_patterns = [
            r"^这是一个测试", r"^用于验证", r"验证流水线", r"是否正常工作",
            r"^test\b", r"^测试\b", r"^\s*$", r"请生成", r"帮我写",
        ]
        for q in queries:
            q = q.strip()
            if len(q) < 4:
                continue
            if any(re.search(p, q, re.I) for p in noise_patterns):
                continue
            # 过滤纯标点/纯停用词
            meaningful = re.findall(r"[一-鿿]{2,}|[a-zA-Z]{2,}", q)
            if not meaningful:
                continue
            cleaned.append(q)
        return cleaned

    @staticmethod
    def _clean_query_text(query: str) -> str:
        """清洗查询文本：去 markdown 格式、序号前缀、特殊符号"""
        import re
        q = query.strip()
        # 去 markdown 加粗/斜体
        q = re.sub(r"\*\*(.+?)\*\*", r"\1", q)
        q = re.sub(r"\*(.+?)\*", r"\1", q)
        # 去 markdown 链接文本 [text](url) → text
        q = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", q)
        # 去编号前缀 "1. **xxx** → xxx"
        q = re.sub(r"^\d+[\.、]\s*", "", q)
        # 去列表前缀
        q = re.sub(r"^[-*]\s+", "", q)
        # 统一中文破折号
        q = re.sub(r"[—–]+", " - ", q)
        return q.strip()

    def _is_spam_url(self, url: str) -> bool:
        """检查 URL 是否属于垃圾域名"""
        import urllib.parse
        try:
            domain = urllib.parse.urlparse(url).hostname or ""
            # 精确匹配
            if domain in self._spam_domains:
                return True
            # 后缀匹配
            for spam in self._spam_domains:
                if domain and domain.endswith("." + spam):
                    return True
            return False
        except Exception:
            return False

    def _normalize_url(self, url: str) -> str:
        """URL 归一化：去跟踪参数，用于更准确的去重"""
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(url)
            # 丢弃常见跟踪参数
            track_params = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                           "utm_content", "fbclid", "gclid", "ref", "source",
                           "trid", "tridChannel", "spm", "scm"}
            clean_query = "&".join(
                f"{k}={v}" for k, v in urllib.parse.parse_qsl(parsed.query)
                if k.lower() not in track_params
            )
            return urllib.parse.urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                parsed.params, clean_query, parsed.fragment
            )).rstrip("?&") or url
        except Exception:
            return url

    def _score_and_filter(self, results: list[SearchResult]) -> list[SearchResult]:
        """质量评分和过滤（含域名黑名单 + query 相关性 + 标题质量）"""
        import re
        filtered = []
        for r in results:
            # 0a. 垃圾域名直接丢弃
            if self._is_spam_url(r.url):
                continue

            # 0b. 跟踪/重定向 URL 丢弃
            url_lower = r.url.lower()
            if any(p in url_lower for p in self._tracking_url_patterns):
                continue

            # 0c. 标题质量检查：至少包含一个真正的内容词
            if not self._title_min_pattern.search(r.title):
                continue

            # 1. 基础质量分（结构完整度）
            struct = 0.0
            if 10 <= len(r.title) <= 120:
                struct += 0.15
            if len(r.snippet) > 50:
                struct += 0.15
            if r.source and r.source in ("bing", "sogou", "360", "prosearch"):
                struct += 0.1
            if r.url and r.url.startswith("http"):
                struct += 0.1

            # 域名质量加成
            import urllib.parse
            try:
                domain = urllib.parse.urlparse(r.url).hostname or ""
                for lq in self._low_quality_domains:
                    if lq in domain:
                        struct -= 0.2
                        break
            except Exception:
                pass

            # 2. query 相关性分
            rel = self._relevance_score(r, r.query)
            r.relevance = rel

            # 综合分：相关性占主导（65%），结构占 35%
            r.score = min(max(struct, 0) * 0.35 + rel * 0.65, 1.0)

            if r.score >= self._min_score and r.relevance > 0:
                filtered.append(r)

        # 按评分排序
        filtered.sort(key=lambda x: x.score, reverse=True)
        return filtered

    def _relevance_score(self, r: SearchResult, query: str) -> float:
        """计算搜索结果与 query 的相关性（0~1）

        策略：从 query 提取关键词（去停用词），统计在标题+摘要中的命中比例。
        完全无命中 → 0；关键词全部命中 → 1。
        """
        if not query:
            return 0.5
        import re
        # query 关键词：去标点、去停用词，取中文 2+ 字词和英文单词
        stop = {"的", "了", "是", "在", "我", "有", "和", "与", "及", "一个", "这份",
                "介绍", "简单", "基本", "概念", "生成", "一份", "文档", "技术", "测试",
                "这是", "用于", "验证", "流水线", "是否", "正常", "工作", "a", "the",
                "of", "to", "and", "is", "for", "this", "that", "with", "in", "on"}
        tokens: list[str] = []
        # 中文：按字/词拆（简单 2-gram 覆盖）
        cn = re.findall(r"[一-鿿]{2,}", query)
        for w in cn:
            if w not in stop and len(w) >= 2:
                tokens.append(w)
        # 英文：单词
        en = re.findall(r"[a-zA-Z]{2,}", query.lower())
        for w in en:
            if w not in stop:
                tokens.append(w)
        if not tokens:
            return 0.5

        text = f"{r.title} {r.snippet}".lower()
        hit = 0
        for t in tokens:
            if t.lower() in text:
                hit += 1
        return hit / len(tokens)

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """基于归一化 URL + 标题去重"""
        with self._dedup_lock:
            seen_urls = set()
            seen_titles = set()
            deduped = []

            for r in results:
                # URL 归一化去重（去跟踪参数后的裸 URL）
                clean_url = self._normalize_url(r.url or "")
                url_sig = clean_url.split("?")[0] if "?" in clean_url else clean_url
                if url_sig in seen_urls:
                    continue
                seen_urls.add(url_sig)

                # 标题去重（取前 30 字，忽略大小写）
                title_sig = (r.title or "").strip().lower()[:30]
                if title_sig and title_sig in seen_titles:
                    continue
                if title_sig:
                    seen_titles.add(title_sig)

                deduped.append(r)

            return deduped

    def _record_history(self, task_id: str, queries: list[str], result_count: int):
        """记录搜索历史"""
        with self._history_lock:
            self._search_history.append({
                "task_id": task_id,
                "queries": queries,
                "result_count": result_count,
                "timestamp": time.time(),
            })
            
            if len(self._search_history) > self._max_history:
                self._search_history = self._search_history[-self._max_history:]

    def get_search_history(self) -> list[dict]:
        """获取搜索历史"""
        with self._history_lock:
            return list(self._search_history)

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        self.log_info("缓存已清空")

    def is_healthy(self) -> bool:
        """健康检查"""
        # 检查缓存是否正常
        try:
            test_key = "health_check"
            self._cache.set(test_key, [])
            self._cache.get(test_key)
            return True
        except Exception:
            return False
