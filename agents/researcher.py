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
import time
import hashlib
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


@dataclass
class SearchResult:
    """搜索结果"""
    title: str
    url: str
    snippet: str
    source: str
    query: str
    score: float = 0.0  # 质量评分
    fetched_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "query": self.query,
            "score": self.score,
            "fetched_at": self.fetched_at,
        }


class LRUCache:
    """LRU 缓存（带 TTL）"""
    
    def __init__(self, max_size: int = 1000, ttl: int = 86400):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: dict[str, dict] = {}
        self._access_order: list[str] = []
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[list]:
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            
            # 检查 TTL
            if time.time() - entry["ts"] > self.ttl:
                self._remove(key)
                return None
            
            # 更新访问顺序
            self._access_order.remove(key)
            self._access_order.append(key)
            
            return entry["data"]
    
    def set(self, key: str, data: list):
        with self._lock:
            # 淘汰旧条目
            while len(self._cache) >= self.max_size:
                oldest = self._access_order.pop(0)
                self._cache.pop(oldest, None)
            
            self._cache[key] = {"data": data, "ts": time.time()}
            self._access_order.append(key)
    
    def _remove(self, key: str):
        self._cache.pop(key, None)
        if key in self._access_order:
            self._access_order.remove(key)
    
    def clear(self):
        with self._lock:
            self._cache.clear()
            self._access_order.clear()


class ResearcherAgent(BaseAgent):
    """增强型内容检索 Agent"""

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        
        # 缓存
        cache_size = config.get("cache_size", 1000)
        self._cache = LRUCache(max_size=cache_size, ttl=self.meta.cache_ttl or CACHE_TTL)
        
        # 去重集合
        self.dedup_set: set = set()
        self._dedup_lock = threading.Lock()
        
        # 搜索历史
        self._search_history: list[dict] = []
        self._max_history = config.get("max_history", 100)
        
        # 并发控制
        self._max_workers = config.get("max_workers", 3)
        
        # 搜索引擎配置
        self._search_engines = config.get("search_engines", ["bing", "sogou", "360"])
        
        # 限流器（每引擎独立限流）
        from pipeline_core.rate_limiter import RateLimiterRegistry
        self._rate_limiters = RateLimiterRegistry()
        
        # 质量评分配置
        self._min_score = config.get("min_score", 0.3)
        
        self.log_info(f"Researcher v{AGENT_VERSION} 初始化完成")

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
        if isinstance(cfg, dict):
            if "search_engines" in cfg:
                self._search_engines = cfg["search_engines"]
            if "max_results" in cfg:
                max_results = cfg["max_results"]
        
        if not queries:
            return {"status": "ok", "task_id": task_id, "total": 0,
                    "results": [], "query_count": 0, "engines_used": self._search_engines}
        
        self.log_info(f"任务 {task_id}: {len(queries)} 个查询")
        
        results = []
        
        if parallel and len(queries) > 1:
            # 并行搜索
            results = self._parallel_search(queries, task_id, max_results)
        else:
            # 串行搜索
            for i, query in enumerate(queries):
                self.report(AgentStatus.RUNNING, f"[{i+1}/{len(queries)}] {query[:40]}...")
                try:
                    r = self._search(query, task_id)
                    results.extend(r)
                except Exception as e:
                    self.log_error(f"搜索失败 {query}: {e}")
        
        # 质量评分和过滤
        results = self._score_and_filter(results)
        
        # 去重
        results = self._deduplicate(results)
        
        # 限制结果数
        results = results[:max_results]
        
        # 记录历史
        self._record_history(task_id, queries, len(results))
        
        self.report(AgentStatus.RUNNING, f"检索完成，共 {len(results)} 条结果")
        
        return {
            "status": "ok",
            "task_id": task_id,
            "total": len(results),
            "results": [r.to_dict() for r in results],
            "query_count": len(queries),
            "engines_used": self._search_engines,
        }

    def _parallel_search(self, queries: list[str], task_id: str, max_results: int) -> list[SearchResult]:
        """并行搜索"""
        results = []
        completed = 0
        
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_query = {
                executor.submit(self._search, query, task_id): query 
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

    def _search(self, query: str, task_id: str) -> list[SearchResult]:
        """执行单次搜索（含缓存）"""
        cache_key = f"{task_id}:{query}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self.log_debug(f"缓存命中: {query[:40]}")
            return [SearchResult(**r) for r in cached]

        all_results = []

        # 多引擎搜索（限流）
        for engine in self._search_engines:
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
        
        # 尝试多个可能的路径
        possible_paths = [
            r"F:\Program Files\QClaw\resources\openclaw\config\skills\online-search\scripts\prosearch.cjs",
            r"F:\Program Files (x86)\qclaw\resources\openclaw\config\skills\online-search\scripts\prosearch.cjs",
        ]
        
        script = None
        for path in possible_paths:
            if Path(path).exists():
                script = path
                break
        
        if not script:
            self.log_warning("prosearch.cjs 未找到，使用模拟数据")
            return self._mock_search(query, "prosearch")
        
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

    def _bing_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 Bing 搜索（国内可访问，免费，无需 API Key）"""
        import re, requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            r = requests.get("https://cn.bing.com/search",
                             params={"q": query, "mkt": "zh-CN"},
                             headers=headers, timeout=10)
            if r.status_code != 200:
                self.log_warning(f"Bing 返回 {r.status_code}")
                return []
            results = []
            seen_urls = set()
            # Bing 搜索结果链接特征：target="_blank" 的 <a href="https://...">
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', r.text):
                url = m.group(1)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                # 跳过搜索引擎自身和静态资源
                if any(d in url for d in ("bing.com", "microsoft.com", "live.com",
                                           "w3.org", "schema.org")):
                    continue
                if any(url.endswith(ext) for ext in (".css", ".js", ".svg", ".png", ".ico")):
                    continue
                # 获取周围的文字作为标题
                start = max(0, m.start() - 200)
                ctx = r.text[start:m.end()]
                # 从上下文提取中文/英文标题
                title_match = re.search(r'(?:title|aria-label)="([^"]+)"', ctx)
                title = title_match.group(1) if title_match else url.split("/")[-1][:60]
                title = re.sub(r'<[^>]+>', '', title).strip()
                # 从 snippet 取一段摘要
                snippet_start = m.end()
                snippet_end = min(snippet_start + 500, len(r.text))
                snippet = re.sub(r'<[^>]+>', '', r.text[snippet_start:snippet_end]).strip()[:200]
                results.append(SearchResult(
                    title=title[:120] if len(title) > 120 else title,
                    url=url,
                    snippet=snippet,
                    source="bing",
                    query=query,
                    score=0.8,
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
        import re, requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            r = requests.get("https://www.sogou.com/web",
                             params={"query": query},
                             headers=headers, timeout=10)
            if r.status_code != 200:
                self.log_warning(f"Sogou 返回 {r.status_code}")
                return []
            results = []
            seen_urls = set()
            # Sogou results: <div class="vrwrap"> with <a href="..."> links
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', r.text):
                url = m.group(1)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                # 跳过搜索引擎自身和静态资源
                if any(d in url for d in ("sogou.com", "weixin.sogou.com",
                                           "w3.org", "schema.org")):
                    continue
                if any(url.endswith(ext) for ext in (".css", ".js", ".svg", ".png", ".ico")):
                    continue
                # 获取周围的文字作为标题
                start = max(0, m.start() - 200)
                ctx = r.text[start:m.end()]
                # 从上下文提取标题
                title_match = re.search(r'(?:title|aria-label)="([^"]+)"', ctx)
                title = title_match.group(1) if title_match else url.split("/")[-1][:60]
                title = re.sub(r'<[^>]+>', '', title).strip()
                # 从 snippet 取一段摘要
                snippet_start = m.end()
                snippet_end = min(snippet_start + 500, len(r.text))
                snippet = re.sub(r'<[^>]+>', '', r.text[snippet_start:snippet_end]).strip()[:200]
                results.append(SearchResult(
                    title=title[:120] if len(title) > 120 else title,
                    url=url,
                    snippet=snippet,
                    source="sogou",
                    query=query,
                    score=0.8,
                ))
                if len(results) >= max_results:
                    break
            self.log_info(f"Sogou [{query[:30]}]: {len(results)} 条结果")
            return results
        except Exception as e:
            self.log_warning(f"Sogou 搜索失败 ({e})，返回空结果")
            return []

    def _360_search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """调用 360 搜索（好搜，国内可访问，免费，无需 API Key）"""
        import re, requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            r = requests.get("https://www.so.com/s",
                             params={"q": query},
                             headers=headers, timeout=10)
            if r.status_code != 200:
                self.log_warning(f"360 搜索返回 {r.status_code}")
                return []
            results = []
            seen_urls = set()
            # 360 搜索结果：<a href="https://..."> 或 data-url 属性
            for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>', r.text):
                url = m.group(1)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                # 跳过搜索引擎自身和静态资源
                if any(d in url for d in ("so.com", "360.cn", "haosou.com",
                                           "w3.org", "schema.org")):
                    continue
                if any(url.endswith(ext) for ext in (".css", ".js", ".svg", ".png", ".ico")):
                    continue
                # 获取周围的文字作为标题
                start = max(0, m.start() - 200)
                ctx = r.text[start:m.end()]
                # 从上下文提取标题
                title_match = re.search(r'(?:title|aria-label)="([^"]+)"', ctx)
                title = title_match.group(1) if title_match else url.split("/")[-1][:60]
                title = re.sub(r'<[^>]+>', '', title).strip()
                # 从 snippet 取一段摘要
                snippet_start = m.end()
                snippet_end = min(snippet_start + 500, len(r.text))
                snippet = re.sub(r'<[^>]+>', '', r.text[snippet_start:snippet_end]).strip()[:200]
                results.append(SearchResult(
                    title=title[:120] if len(title) > 120 else title,
                    url=url,
                    snippet=snippet,
                    source="360",
                    query=query,
                    score=0.8,
                ))
                if len(results) >= max_results:
                    break
            self.log_info(f"360 [{query[:30]}]: {len(results)} 条结果")
            return results
        except Exception as e:
            self.log_warning(f"360 搜索失败 ({e})，返回空结果")
            return []

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

    def _score_and_filter(self, results: list[SearchResult]) -> list[SearchResult]:
        """质量评分和过滤"""
        for r in results:
            # 简单评分算法
            score = 0.0
            
            # 标题长度适中
            if 10 <= len(r.title) <= 100:
                score += 0.3
            
            # 摘要非空
            if len(r.snippet) > 50:
                score += 0.3
            
            # 有来源
            if r.source:
                score += 0.2
            
            # URL 有效
            if r.url and r.url.startswith("http"):
                score += 0.2
            
            r.score = min(score, 1.0)
        
        # 过滤低质量结果
        filtered = [r for r in results if r.score >= self._min_score]
        
        # 按评分排序
        filtered.sort(key=lambda x: x.score, reverse=True)
        
        return filtered

    def _deduplicate(self, results: list[SearchResult]) -> list[SearchResult]:
        """基于 URL + 标题去重"""
        with self._dedup_lock:
            seen = set()
            deduped = []
            
            for r in results:
                sig = (r.url or "") + "|" + (r.title or "")
                if sig not in seen:
                    seen.add(sig)
                    deduped.append(r)
            
            return deduped

    def _record_history(self, task_id: str, queries: list[str], result_count: int):
        """记录搜索历史"""
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
        return self._search_history

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
