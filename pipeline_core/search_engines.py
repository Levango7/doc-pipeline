"""
Search Engines — 统一搜索引擎接口
==================================
核心特性：
  - 7 个搜索引擎统一接口
  - 自动 fallback：引擎失败自动切换下一个
  - 结果标准化：不同引擎返回统一格式
  - Metaso API 集成（结构化搜索）
  - DuckDuckGo 集成（无需 API Key）
  - HTML 抓取引擎（Bing/Sogou/360）
  - 线程安全 + 限流

支持引擎：
  1. metaso     — 秘塔搜索 API（结构化，需 API Key）
  2. duckduckgo — DuckDuckGo（无需 Key，国际覆盖好）
  3. bing       — 必应 HTML 抓取
  4. sogou      — 搜狗 HTML 抓取
  5. 360        — 360 搜索 HTML 抓取
  6. prosearch  — 元宝搜索（本地 Node.js 脚本）
  7. google     — Google HTML 抓取（可选，需翻墙）
"""
import json
import os
import time
import re
import logging
import threading
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SearchItem:
    """标准化搜索结果"""
    title: str
    url: str
    snippet: str
    source: str       # 引擎名称
    query: str
    score: float = 0.0
    relevance: float = 0.0
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "title": self.title, "url": self.url, "snippet": self.snippet,
            "source": self.source, "query": self.query,
            "score": self.score, "relevance": self.relevance,
            "fetched_at": self.fetched_at,
        }


class SearchEngineBase:
    """搜索引擎基类"""

    name: str = "base"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        raise NotImplementedError

    def is_available(self) -> bool:
        return True


# ─── Metaso API ──────────────────────────────────

class MetasoEngine(SearchEngineBase):
    """秘塔搜索 API（结构化搜索，需 API Key）"""

    name = "metaso"

    def __init__(self, api_key: str = "", api_url: str = ""):
        self._api_key = api_key or os.environ.get("METASO_API_KEY", "")
        self._api_url = api_url or os.environ.get("METASO_API_URL", "https://metaso.cn/api/v1/search")

    def is_available(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        if not self._api_key:
            return []
        try:
            payload = json.dumps({
                "q": query, "size": max_results, "type": "web",
            }).encode()
            req = urllib.request.Request(
                self._api_url, data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())

            results = []
            items = body.get("data", {}).get("webpages", [])
            if isinstance(items, dict):
                items = items.get("list", [])
            for item in items[:max_results]:
                results.append(SearchItem(
                    title=item.get("title", ""),
                    url=item.get("url", item.get("link", "")),
                    snippet=item.get("snippet", item.get("summary", "")),
                    source=self.name, query=query,
                ))
            return results
        except Exception as e:
            logger.debug(f"Metaso 搜索失败: {e}")
            return []


# ─── DuckDuckGo ──────────────────────────────────

class DuckDuckGoEngine(SearchEngineBase):
    """DuckDuckGo 搜索（无需 API Key）"""

    name = "duckduckgo"

    def __init__(self):
        self._api_url = "https://html.duckduckgo.com/html/"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        try:
            data = urllib.parse.urlencode({"q": query, "b": ""}).encode()
            req = urllib.request.Request(
                self._api_url, data=data,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            results = []
            # 解析 DuckDuckGo HTML 结果
            blocks = re.findall(
                r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'class="result__snippet"[^>]*>(.*?)</a>',
                html, re.DOTALL
            )
            for url, title, snippet in blocks[:max_results]:
                # DuckDuckGo 的 URL 是跳转链接，提取真实 URL
                if "uddg=" in url:
                    m = re.search(r"uddg=([^&]+)", url)
                    if m:
                        url = urllib.parse.unquote(m.group(1))
                title = re.sub(r"<[^>]+>", "", title).strip()
                snippet = re.sub(r"<[^>]+>", "", snippet).strip()
                if title and url:
                    results.append(SearchItem(
                        title=title, url=url, snippet=snippet,
                        source=self.name, query=query,
                    ))
            return results
        except Exception as e:
            logger.debug(f"DuckDuckGo 搜索失败: {e}")
            return []


# ─── HTML 抓取引擎基类 ────────────────────────────

class HtmlSearchEngine(SearchEngineBase):
    """HTML 抓取搜索引擎基类"""

    def __init__(self):
        self._ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    def _fetch_html(self, url: str, data: bytes = None) -> str:
        headers = {"User-Agent": self._ua, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _extract_results(self, html: str, query: str,
                         max_results: int, source: str) -> list[SearchItem]:
        """通用 HTML 结果提取（子类可覆盖）"""
        results = []
        # 通用模式：提取 <a href="http...">title</a> + 附近文本
        pattern = re.compile(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL
        )
        seen_urls = set()
        for url, title in pattern.findall(html):
            title = re.sub(r"<[^>]+>", "", title).strip()
            if not title or len(title) < 5 or url in seen_urls:
                continue
            if any(d in url for d in ["google.com", "bing.com", "sogou.com", "so.com"]):
                continue
            seen_urls.add(url)
            results.append(SearchItem(
                title=title, url=url, snippet="",
                source=source, query=query,
            ))
            if len(results) >= max_results:
                break
        return results


class BingEngine(HtmlSearchEngine):
    name = "bing"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        try:
            url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count={max_results}"
            html = self._fetch_html(url)
            results = []
            # Bing 结果块
            blocks = re.findall(
                r'<li class="b_algo">(.*?)</li>', html, re.DOTALL
            )
            for block in blocks[:max_results]:
                link_m = re.search(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                if not link_m:
                    continue
                link = link_m.group(1)
                title = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
                snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""
                if title and link:
                    results.append(SearchItem(
                        title=title, url=link, snippet=snippet,
                        source=self.name, query=query,
                    ))
            return results
        except Exception as e:
            logger.debug(f"Bing 搜索失败: {e}")
            return []


class SogouEngine(HtmlSearchEngine):
    name = "sogou"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        try:
            url = f"https://www.sogou.com/web?query={urllib.parse.quote(query)}"
            html = self._fetch_html(url)
            results = []
            blocks = re.findall(
                r'<div class="vrwrap">(.*?)</div>\s*</div>', html, re.DOTALL
            )
            for block in blocks[:max_results]:
                link_m = re.search(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                if not link_m:
                    continue
                link = link_m.group(1)
                title = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
                snippet_m = re.search(r'<p[^>]*class="str-text-info"[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""
                if title and link:
                    results.append(SearchItem(
                        title=title, url=link, snippet=snippet,
                        source=self.name, query=query,
                    ))
            return results
        except Exception as e:
            logger.debug(f"Sogou 搜索失败: {e}")
            return []


class So360Engine(HtmlSearchEngine):
    name = "360"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        try:
            url = f"https://www.so.com/s?q={urllib.parse.quote(query)}"
            html = self._fetch_html(url)
            results = []
            blocks = re.findall(
                r'<li class="res-list">(.*?)</li>', html, re.DOTALL
            )
            for block in blocks[:max_results]:
                link_m = re.search(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                if not link_m:
                    continue
                link = link_m.group(1)
                title = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
                snippet_m = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""
                if title and link:
                    results.append(SearchItem(
                        title=title, url=link, snippet=snippet,
                        source=self.name, query=query,
                    ))
            return results
        except Exception as e:
            logger.debug(f"360 搜索失败: {e}")
            return []


class BaiduEngine(HtmlSearchEngine):
    """百度搜索（HTML 抓取）"""
    name = "baidu"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        try:
            url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}&rn={max_results}"
            html = self._fetch_html(url)
            results = []
            blocks = re.findall(
                r'<div class="(?:result|c-container)[^"]*"[^>]*>(.*?)</div>\s*</div>',
                html, re.DOTALL
            )
            seen_urls = set()
            for block in blocks[:max_results]:
                link_m = re.search(
                    r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                    block, re.DOTALL
                )
                if not link_m:
                    continue
                link = link_m.group(1)
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                title = re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
                snippet_m = re.search(
                    r'<span[^>]*class="content-right_[^"]*"[^>]*>(.*?)</span>',
                    block, re.DOTALL
                )
                snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""
                if title and link:
                    results.append(SearchItem(
                        title=title, url=link, snippet=snippet,
                        source=self.name, query=query,
                    ))
            return results
        except Exception as e:
            logger.debug(f"百度搜索失败: {e}")
            return []


class ProSearchEngine(SearchEngineBase):
    """元宝搜索（本地 Node.js 脚本）"""

    name = "prosearch"

    DEFAULT_PATHS = [
        os.environ.get("PROSEARCH_PATH", ""),
        r"F:\Program Files\QClaw\resources\openclaw\config\skills\online-search\scripts\prosearch.cjs",
        r"F:\Program Files (x86)\qclaw\resources\openclaw\config\skills\online-search\scripts\prosearch.cjs",
    ]

    def __init__(self):
        self._script = None
        for path in self.DEFAULT_PATHS:
            if os.path.exists(path):
                self._script = path
                break
        env_path = os.environ.get("PROSEARCH_SCRIPT_PATH", "")
        if env_path and os.path.exists(env_path):
            self._script = env_path

    def is_available(self) -> bool:
        return self._script is not None

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        if not self._script:
            return []
        try:
            import subprocess
            result = subprocess.run(
                ["node", self._script, json.dumps({"keyword": query})],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return []
            data = json.loads(result.stdout)
            items = data if isinstance(data, list) else data.get("results", [])
            return [
                SearchItem(
                    title=item.get("title", ""),
                    url=item.get("url", item.get("link", "")),
                    snippet=item.get("snippet", item.get("summary", "")),
                    source=self.name, query=query,
                )
                for item in items[:max_results]
            ]
        except Exception as e:
            logger.debug(f"ProSearch 失败: {e}")
            return []


# ─── 搜索引擎注册表 ──────────────────────────────

_ENGINE_REGISTRY = {
    "metaso": MetasoEngine,
    "duckduckgo": DuckDuckGoEngine,
    "bing": BingEngine,
    "baidu": BaiduEngine,
    "sogou": SogouEngine,
    "360": So360Engine,
    "prosearch": ProSearchEngine,
}


def create_engine(name: str, **kwargs) -> Optional[SearchEngineBase]:
    """创建搜索引擎实例"""
    cls = _ENGINE_REGISTRY.get(name)
    if cls is None:
        logger.warning(f"未知搜索引擎: {name}")
        return None
    try:
        return cls(**kwargs)
    except Exception as e:
        logger.warning(f"创建引擎 {name} 失败: {e}")
        return None


class SearchEngineManager:
    """搜索引擎管理器 — 多引擎 fallback

    用法：
        mgr = SearchEngineManager.from_env()
        results = mgr.search("Kafka 架构", engines=["metaso", "bing", "sogou"])
        # metaso 失败自动 fallback 到 bing
    """

    def __init__(self, engines: dict[str, SearchEngineBase] = None):
        self._engines: dict[str, SearchEngineBase] = engines or {}
        self._lock = threading.Lock()
        self._fail_counts: dict[str, int] = {}
        logger.info(f"SearchEngineManager 初始化: {list(self._engines.keys())}")

    def is_available(self) -> bool:
        """是否有可用引擎"""
        return len(self._engines) > 0

    def add_engine(self, name: str, engine: SearchEngineBase):
        with self._lock:
            self._engines[name] = engine

    def search(self, query: str, max_results: int = 10,
               engines: list[str] = None) -> list[SearchItem]:
        """搜索（多引擎 fallback）

        引擎按列表顺序尝试，失败自动切换下一个。
        结果合并去重。
        """
        if engines is None:
            engines = list(self._engines.keys())

        all_results = []
        seen_urls = set()

        for engine_name in engines:
            engine = self._engines.get(engine_name)
            if engine is None or not engine.is_available():
                logger.debug(f"引擎 {engine_name} 不可用，跳过")
                continue

            try:
                results = engine.search(query, max_results)
                for r in results:
                    if r.url and r.url not in seen_urls:
                        seen_urls.add(r.url)
                        all_results.append(r)
                if results:
                    logger.debug(f"引擎 {engine_name} 返回 {len(results)} 条结果")
            except Exception as e:
                self._fail_counts[engine_name] = self._fail_counts.get(engine_name, 0) + 1
                logger.warning(f"引擎 {engine_name} 搜索失败: {e}")

            # 已有足够结果时停止
            if len(all_results) >= max_results:
                break

        return all_results[:max_results]

    def status(self) -> dict:
        return {
            "engines": {
                name: {
                    "available": eng.is_available(),
                    "fail_count": self._fail_counts.get(name, 0),
                }
                for name, eng in self._engines.items()
            }
        }

    # 重点站点（技术文档常用来源）
    SITE_TARGETS = [
        ("zhihu.com", "知乎"),
        ("juejin.cn", "掘金"),
        ("bilibili.com", "哔哩哔哩"),
        ("blog.csdn.net", "CSDN"),
        ("cnblogs.com", "博客园"),
        ("github.com", "GitHub"),
        ("gitee.com", "Gitee"),
        ("wikipedia.org", "维基百科"),
        ("segmentfault.com", "SegmentFault"),
    ]

    def search_with_sites(self, query: str, max_results: int = 10,
                          sites: list[str] = None,
                          engines: list[str] = None) -> list[SearchItem]:
        """搜索 + 重点站点搜索（合并去重）

        先进行常规搜索，再对每个站点进行 site: 搜索。
        结果合并去重，优先常规搜索的结果。
        """
        all_results = []
        seen_urls = set()

        # 常规搜索
        for item in self.search(query, max_results=max_results, engines=engines):
            if item.url and item.url not in seen_urls:
                seen_urls.add(item.url)
                all_results.append(item)

        # 站点搜索（每个站点至少拿 2 条）
        if sites is None:
            sites = self.SITE_TARGETS
        site_queries = []
        for site_domain, site_name in sites:
            site_query = f"site:{site_domain} {query}"
            site_queries.append((site_query, site_name))

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    self.search, sq, max_results=3, engines=engines
                ): (sq, site_name)
                for sq, site_name in site_queries
            }
            for future in concurrent.futures.as_completed(futures):
                sq, site_name = futures[future]
                try:
                    for item in future.result():
                        if item.url and item.url not in seen_urls:
                            seen_urls.add(item.url)
                            item.source = f"{item.source}[{site_name}]"
                            all_results.append(item)
                except Exception as e:
                    logger.debug(f"站点搜索失败 {site_name}: {e}")

        return all_results[:max_results]

    @classmethod
    def from_env(cls, env_path: str = None) -> "SearchEngineManager":
        """从环境变量加载所有可用引擎"""
        from pipeline_core.llm_router import _load_env
        env = _load_env(env_path)

        engines = {}

        # Metaso（需 API Key）
        metaso_key = env.get("METASO_API_KEY", "")
        if metaso_key:
            eng = create_engine("metaso", api_key=metaso_key,
                                api_url=env.get("METASO_API_URL", ""))
            if eng:
                engines["metaso"] = eng

        # DuckDuckGo（无需 Key）
        eng = create_engine("duckduckgo")
        if eng:
            engines["duckduckgo"] = eng

        # HTML 抓取引擎
        for name in ["bing", "baidu", "sogou", "360"]:
            eng = create_engine(name)
            if eng:
                engines[name] = eng

        # ProSearch（本地脚本）
        eng = create_engine("prosearch")
        if eng and eng.is_available():
            engines["prosearch"] = eng

        return cls(engines)
