"""
Search Engines — 统一搜索引擎接口
==================================
核心特性：
  - 10 个搜索引擎统一接口
  - 自动 fallback：引擎失败自动切换下一个
  - 结果标准化：不同引擎返回统一格式
  - API 引擎：Bocha / Tavily / Serper / Metaso（结构化，稳定不怕改版）
  - HTML 引擎：Bing / Baidu / Sogou / 360（兜底）
  - Firecrawl 网页提取增强（URL → Markdown）
  - 线程安全 + 限流

支持引擎（按优先级）：
  1. bocha      — 博查万象 API（国内直连，免费，语义重排序）
  2. tavily     — Tavily AI Search（英文技术文档质量好，免费 1000 次/月）
  3. serper     — Google Serper（Google 搜索代理，免费 2500 次）
  4. metaso     — 秘塔搜索 API（结构化，需 API Key）
  5. duckduckgo — DuckDuckGo（无需 Key，国际覆盖好）
  6. bing       — 必应 HTML 抓取
  7. baidu      — 百度 HTML 抓取
  8. sogou      — 搜狗 HTML 抓取
  9. 360        — 360 搜索 HTML 抓取
  10. prosearch — 元宝搜索（本地 Node.js 脚本）
"""
import json
import os
import time
import re
import logging
import threading
import asyncio
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

from .fast_json import dumps as _fast_dumps, loads as _fast_loads

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

    @classmethod
    def from_dict(cls, d: dict) -> "SearchItem":
        return cls(
            title=d.get("title", ""), url=d.get("url", ""),
            snippet=d.get("snippet", ""), source=d.get("source", ""),
            query=d.get("query", ""), score=d.get("score", 0.0),
            relevance=d.get("relevance", 0.0),
            fetched_at=d.get("fetched_at", time.time()),
        )


class SearchEngineBase:
    """搜索引擎基类"""

    name: str = "base"

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        raise NotImplementedError

    async def search_async(self, query: str, max_results: int = 10) -> list[SearchItem]:
        """异步搜索 — 默认实现用 run_in_executor 包装同步方法。

        API 类引擎可覆盖此方法用 aiohttp 实现真异步。
        """
        # 优先使用 get_running_loop()（3.10+ 推荐），无运行循环时回退到 get_event_loop()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.search, query, max_results)

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
            payload = _fast_dumps({
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
                body = _fast_loads(resp.read().decode())

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

    async def search_async(self, query: str, max_results: int = 10) -> list[SearchItem]:
        """异步搜索 — 使用 aiohttp 真异步 HTTP"""
        if not self._api_key:
            return []
        try:
            import aiohttp
        except ImportError:
            return await super().search_async(query, max_results)

        try:
            payload = {"q": query, "size": max_results, "type": "web"}
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {self._api_key}"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(self._api_url, json=payload, headers=headers) as resp:
                    body = await resp.json()
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
            logger.debug(f"Metaso 异步搜索失败: {e}")
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


# ─── Bocha（博查万象）API ────────────────────────

class BochaEngine(SearchEngineBase):
    """博查万象搜索 API（国内直连，免费，自带语义重排序）

    API 文档: https://open.bochaai.com
    - 端点: POST https://api.bochaai.com/v1/web-search
    - 认证: Bearer Token
    - 特点: 0.15s 延迟，近百万亿网页，自带 Semantic Reranker
    - 免费额度充足，数据不出海合规
    """

    name = "bocha"

    def __init__(self, api_key: str = "", api_url: str = ""):
        self._api_key = api_key or os.environ.get("BOCHA_API_KEY", "")
        self._api_url = api_url or os.environ.get(
            "BOCHA_API_URL", "https://api.bochaai.com/v1/web-search"
        )

    def is_available(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        if not self._api_key:
            return []
        try:
            payload = _fast_dumps({
                "query": query,
                "count": min(max_results, 20),
                "summary": True,
                "freshness": "noLimit",
            }).encode()
            req = urllib.request.Request(
                self._api_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = _fast_loads(resp.read().decode())

            results = []
            # 博查返回格式: {"data": {"webPages": {"value": [...]}}}
            data = body.get("data", body)
            web_pages = data.get("webPages", data)
            items = web_pages.get("value", web_pages.get("list", []))
            if isinstance(items, dict):
                items = items.get("value", [])
            for item in items[:max_results]:
                results.append(SearchItem(
                    title=item.get("name", item.get("title", "")),
                    url=item.get("url", item.get("link", "")),
                    snippet=item.get("summary", item.get("snippet", item.get("description", ""))),
                    source=self.name,
                    query=query,
                    score=float(item.get("score", 0.0)),
                ))
            return results
        except Exception as e:
            logger.debug(f"Bocha 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 10) -> list[SearchItem]:
        """异步搜索 — 使用 aiohttp 真异步 HTTP"""
        if not self._api_key:
            return []
        try:
            import aiohttp
        except ImportError:
            return await super().search_async(query, max_results)
        try:
            payload = {"query": query, "count": min(max_results, 20),
                       "summary": True, "freshness": "noLimit"}
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {self._api_key}"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(self._api_url, json=payload, headers=headers) as resp:
                    body = await resp.json()
            results = []
            data = body.get("data", body)
            web_pages = data.get("webPages", data)
            items = web_pages.get("value", web_pages.get("list", []))
            if isinstance(items, dict):
                items = items.get("value", [])
            for item in items[:max_results]:
                results.append(SearchItem(
                    title=item.get("name", item.get("title", "")),
                    url=item.get("url", item.get("link", "")),
                    snippet=item.get("summary", item.get("snippet", item.get("description", ""))),
                    source=self.name, query=query,
                    score=float(item.get("score", 0.0)),
                ))
            return results
        except Exception as e:
            logger.debug(f"Bocha 异步搜索失败: {e}")
            return []


# ─── Tavily AI Search API ────────────────────────

class TavilyEngine(SearchEngineBase):
    """Tavily AI 搜索 API（专为 AI Agent 设计，自带摘要）

    API 文档: https://docs.tavily.com
    - 端点: POST https://api.tavily.com/search
    - 认证: Bearer Token
    - 特点: 免费 1000 次/月，英文技术文档搜索质量好
    - 返回自带 AI 摘要 (answer 字段)
    """

    name = "tavily"

    def __init__(self, api_key: str = "", api_url: str = ""):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._api_url = api_url or os.environ.get(
            "TAVILY_API_URL", "https://api.tavily.com/search"
        )

    def is_available(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        if not self._api_key:
            return []
        try:
            payload = _fast_dumps({
                "query": query,
                "max_results": min(max_results, 20),
                "search_depth": "advanced",
                "include_answer": True,
                "include_raw_content": False,
            }).encode()
            req = urllib.request.Request(
                self._api_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = _fast_loads(resp.read().decode())

            results = []
            items = body.get("results", [])
            for item in items[:max_results]:
                results.append(SearchItem(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", item.get("snippet", "")),
                    source=self.name,
                    query=query,
                    score=float(item.get("score", 0.0)),
                ))
            return results
        except Exception as e:
            logger.debug(f"Tavily 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 10) -> list[SearchItem]:
        """异步搜索 — 使用 aiohttp 真异步 HTTP"""
        if not self._api_key:
            return []
        try:
            import aiohttp
        except ImportError:
            return await super().search_async(query, max_results)
        try:
            payload = {"query": query, "max_results": min(max_results, 20),
                       "search_depth": "advanced", "include_answer": True,
                       "include_raw_content": False}
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {self._api_key}"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post(self._api_url, json=payload, headers=headers) as resp:
                    body = await resp.json()
            results = []
            for item in body.get("results", [])[:max_results]:
                results.append(SearchItem(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", item.get("snippet", "")),
                    source=self.name, query=query,
                    score=float(item.get("score", 0.0)),
                ))
            return results
        except Exception as e:
            logger.debug(f"Tavily 异步搜索失败: {e}")
            return []


# ─── Serper Google Search API ────────────────────

class SerperEngine(SearchEngineBase):
    """Serper Google 搜索 API（最便宜的 Google 搜索代理）

    API 文档: https://serper.dev
    - 端点: POST https://google.serper.dev/search
    - 认证: X-API-KEY header
    - 特点: 免费 2500 次，之后 $2/1000 次，支持 Google Scholar
    - 返回 Google 原始搜索结果
    """

    name = "serper"

    def __init__(self, api_key: str = "", api_url: str = ""):
        self._api_key = api_key or os.environ.get("SERPER_API_KEY", "")
        self._api_url = api_url or os.environ.get(
            "SERPER_API_URL", "https://google.serper.dev/search"
        )

    def is_available(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, max_results: int = 10) -> list[SearchItem]:
        if not self._api_key:
            return []
        try:
            payload = _fast_dumps({
                "q": query,
                "num": min(max_results, 20),
            }).encode()
            req = urllib.request.Request(
                self._api_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-KEY": self._api_key,
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = _fast_loads(resp.read().decode())

            results = []
            # Serper 返回 organic 数组
            items = body.get("organic", [])
            for item in items[:max_results]:
                results.append(SearchItem(
                    title=item.get("title", ""),
                    url=item.get("link", item.get("url", "")),
                    snippet=item.get("snippet", ""),
                    source=self.name,
                    query=query,
                    score=float(item.get("score", 0.0)),
                ))
            # 知识卡片（如有）
            kg = body.get("knowledgeGraph", {})
            if kg and kg.get("title"):
                results.insert(0, SearchItem(
                    title=kg.get("title", ""),
                    url=kg.get("website", ""),
                    snippet=kg.get("description", ""),
                    source=f"{self.name}[kg]",
                    query=query,
                    score=1.0,
                ))
            return results
        except Exception as e:
            logger.debug(f"Serper 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 10) -> list[SearchItem]:
        """异步搜索 — 使用 aiohttp 真异步 HTTP"""
        if not self._api_key:
            return []
        try:
            import aiohttp
        except ImportError:
            return await super().search_async(query, max_results)
        try:
            payload = {"q": query, "num": min(max_results, 20)}
            headers = {"Content-Type": "application/json",
                       "X-API-KEY": self._api_key}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(self._api_url, json=payload, headers=headers) as resp:
                    body = await resp.json()
            results = []
            for item in body.get("organic", [])[:max_results]:
                results.append(SearchItem(
                    title=item.get("title", ""),
                    url=item.get("link", item.get("url", "")),
                    snippet=item.get("snippet", ""),
                    source=self.name, query=query,
                    score=float(item.get("score", 0.0)),
                ))
            kg = body.get("knowledgeGraph", {})
            if kg and kg.get("title"):
                results.insert(0, SearchItem(
                    title=kg.get("title", ""), url=kg.get("website", ""),
                    snippet=kg.get("description", ""),
                    source=f"{self.name}[kg]", query=query, score=1.0,
                ))
            return results
        except Exception as e:
            logger.debug(f"Serper 异步搜索失败: {e}")
            return []


# ─── Firecrawl 网页提取 API ──────────────────────

class FirecrawlExtractor:
    """Firecrawl 网页内容提取器（URL → Markdown）

    API 文档: https://docs.firecrawl.dev
    - 端点: POST https://api.firecrawl.dev/v1/scrape
    - 认证: Bearer Token
    - 特点: 将任意网页转为干净 Markdown，支持 JS 渲染
    - 用途: 集成到 fetcher，提升正文提取质量

    不是搜索引擎，而是内容提取增强工具。
    """

    name = "firecrawl"

    def __init__(self, api_key: str = "", api_url: str = ""):
        self._api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")
        self._api_url = api_url or os.environ.get(
            "FIRECRAWL_API_URL", "https://api.firecrawl.dev/v1/scrape"
        )

    def is_available(self) -> bool:
        return bool(self._api_key)

    def scrape(self, url: str, timeout: int = 30) -> dict:
        """提取网页内容，返回 Markdown 格式正文

        Returns:
            {"success": bool, "markdown": str, "title": str, "error": str}
        """
        if not self._api_key:
            return {"success": False, "markdown": "", "title": "", "error": "no API key"}

        try:
            payload = _fast_dumps({
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
            }).encode()
            req = urllib.request.Request(
                self._api_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = _fast_loads(resp.read().decode())

            data = body.get("data", body)
            markdown = data.get("markdown", "")
            title = data.get("metadata", {}).get("title", data.get("title", ""))

            if not markdown:
                return {"success": False, "markdown": "", "title": title,
                        "error": "empty content"}

            return {"success": True, "markdown": markdown, "title": title, "error": ""}
        except Exception as e:
            return {"success": False, "markdown": "", "title": "", "error": str(e)}


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
                ["node", self._script, _fast_dumps({"keyword": query})],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return []
            data = _fast_loads(result.stdout)
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
    "bocha": BochaEngine,
    "tavily": TavilyEngine,
    "serper": SerperEngine,
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
        # 缓存为可选依赖：cache_manager 不可用时降级为无缓存，避免整个管理器无法创建
        self._cache = None
        try:
            from .cache_manager import CacheManager
            self._cache = CacheManager(max_size=500, ttl=3600)
        except Exception as e:
            logger.warning(f"CacheManager 不可用，搜索降级为无缓存: {e}")
        logger.info(f"SearchEngineManager 初始化: {list(self._engines.keys())}")

    def is_available(self) -> bool:
        """是否有可用引擎"""
        return len(self._engines) > 0

    def get_engine_names(self) -> list[str]:
        """返回已注册引擎名称列表（公开接口，替代外部直接访问 _engines）"""
        return list(self._engines.keys())

    def add_engine(self, name: str, engine: SearchEngineBase):
        with self._lock:
            self._engines[name] = engine

    def search(self, query: str, max_results: int = 10,
               engines: list[str] = None) -> list[SearchItem]:
        """搜索（多引擎 fallback）

        引擎按列表顺序尝试，失败自动切换下一个。
        结果合并去重。跨任务缓存（LRU+TTL）。
        """
        if engines is None:
            engines = list(self._engines.keys())

        cache_key = f"{query}|{max_results}|{','.join(engines)}"
        cached = self._cache.get(cache_key) if self._cache is not None else None
        if cached is not None:
            logger.debug(f"搜索缓存命中: {query}")
            return [SearchItem.from_dict(d) for d in cached]

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
                # fail_counts 可能被 search_with_sites 线程池并发修改，加锁保护
                with self._lock:
                    self._fail_counts[engine_name] = self._fail_counts.get(engine_name, 0) + 1
                logger.warning(f"引擎 {engine_name} 搜索失败: {e}")

            # 已有足够结果时停止
            if len(all_results) >= max_results:
                break

        result = all_results[:max_results]
        if result and self._cache is not None:
            # P0 修复：CacheManager 接口是 set() 而非 put()，原代码导致搜索结果无法缓存
            self._cache.set(cache_key, [r.to_dict() for r in result])
        return result

    async def search_async(self, query: str, max_results: int = 10,
                           engines: list[str] = None) -> list[SearchItem]:
        """异步并行搜索 — 多引擎并发执行，不阻塞事件循环。

        与同步 search() 的区别：所有引擎并行发起请求，而非串行 fallback。
        结果合并去重，按完成顺序取前 max_results 条。
        """
        if engines is None:
            engines = list(self._engines.keys())

        async def _try_engine(name: str) -> list[SearchItem]:
            engine = self._engines.get(name)
            if engine is None or not engine.is_available():
                return []
            try:
                return await engine.search_async(query, max_results)
            except Exception as e:
                # fail_counts 可能被并发修改，加锁保护
                with self._lock:
                    self._fail_counts[name] = self._fail_counts.get(name, 0) + 1
                logger.warning(f"引擎 {name} 异步搜索失败: {e}")
                return []

        # 并行发起所有引擎搜索
        tasks = {asyncio.create_task(_try_engine(name)): name for name in engines}
        all_results = []
        seen_urls = set()

        for coro in asyncio.as_completed(tasks.keys()):
            try:
                results = await coro
                for r in results:
                    if r.url and r.url not in seen_urls:
                        seen_urls.add(r.url)
                        all_results.append(r)
            except Exception as e:
                # 记录异常而非 silent pass，便于排查引擎偶发错误
                logger.debug(f"异步搜索任务异常: {e}")
            # 已有足够结果时可取消剩余任务
            if len(all_results) >= max_results:
                pending = [t for t in tasks if not t.done()]
                for t in pending:
                    t.cancel()
                # await 已取消任务，避免 "Task was destroyed but it is pending" 警告
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                break

        return all_results[:max_results]

    async def search_with_sites_async(self, query: str, max_results: int = 10,
                                      sites: list[str] = None,
                                      engines: list[str] = None) -> list[SearchItem]:
        """异步版 search_with_sites — 常规搜索 + 站点搜索全部并行"""
        all_results = []
        seen_urls = set()

        # 常规搜索
        for item in await self.search_async(query, max_results=max_results, engines=engines):
            if item.url and item.url not in seen_urls:
                seen_urls.add(item.url)
                all_results.append(item)

        # 站点搜索并行
        if sites is None:
            sites = self.SITE_TARGETS
        site_queries = [(f"site:{domain} {query}", name) for domain, name in sites]

        tasks = [
            self.search_async(sq, max_results=3, engines=engines)
            for sq, _ in site_queries
        ]
        for coro in asyncio.as_completed(tasks):
            try:
                results = await coro
                for item in results:
                    if item.url and item.url not in seen_urls:
                        seen_urls.add(item.url)
                        all_results.append(item)
            except Exception as e:
                # 记录异常而非 silent pass，便于排查站点搜索偶发错误
                logger.debug(f"站点异步搜索异常: {e}")

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
        """从环境变量加载所有可用引擎

        引擎优先级（从高到低）：
          1. bocha     — 博查万象 API（国内直连，免费，语义重排序）
          2. tavily    — Tavily AI Search（英文技术文档质量好）
          3. serper    — Google Serper（Google 搜索结果代理）
          4. metaso    — 秘塔搜索 API（结构化搜索）
          5. prosearch — 元宝搜索（本地脚本）
          6. bing/baidu/sogou/360 — HTML 抓取兜底
          7. duckduckgo — 国际覆盖（国内可能不可用）
        """
        from pipeline_core.llm_router import _load_env
        env = _load_env(env_path)

        engines = {}

        # API 搜索引擎（优先级最高，按顺序注册）
        # Bocha（博查万象 — 国内首选）
        bocha_key = env.get("BOCHA_API_KEY", "")
        if bocha_key:
            eng = create_engine("bocha", api_key=bocha_key,
                                api_url=env.get("BOCHA_API_URL", ""))
            if eng and eng.is_available():
                engines["bocha"] = eng

        # Tavily（AI Agent 专用搜索）
        tavily_key = env.get("TAVILY_API_KEY", "")
        if tavily_key:
            eng = create_engine("tavily", api_key=tavily_key,
                                api_url=env.get("TAVILY_API_URL", ""))
            if eng and eng.is_available():
                engines["tavily"] = eng

        # Serper（Google 搜索代理）
        serper_key = env.get("SERPER_API_KEY", "")
        if serper_key:
            eng = create_engine("serper", api_key=serper_key,
                                api_url=env.get("SERPER_API_URL", ""))
            if eng and eng.is_available():
                engines["serper"] = eng

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
