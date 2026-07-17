"""
Fetcher Agent v1 - 知识内容获取器
=================================
功能：
  - 从搜索结果 URL 下载完整文章内容
  - 提取正文（去 HTML 标签、导航、广告）
  - 保存到本地临时文本文件
  - 内容质量识别：可用 vs 不可用
  - 返回处理后内容的元数据
"""
import os
import re
import json
import time
import asyncio
import hashlib
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse
from pipeline_core.base_agent import BaseAgent, Message, AgentStatus, AgentMeta

# Async I/O 支持（可选）
try:
    import aiohttp
    USE_ASYNC = True
except ImportError:
    aiohttp = None
    USE_ASYNC = False

AGENT_NAME = "fetcher"
AGENT_VERSION = "1.0"
AGENT_DESC = "知识内容获取器 - 下载、提取正文、保存到本地、质量识别"
AGENT_AUTHOR = "doc-pipeline"
AGENT_PRIORITY = 20
INPUT_TOPICS = ["fetcher.input", "researcher.done"]
OUTPUT_TOPICS = ["fetcher.done", "fetcher.progress"]
DEPENDENCIES = ["researcher"]
CACHE_TTL = 0
RESPAWN = False

# 不可用内容的关键词/模式
BAD_PATTERNS = [
    r"404|not found|page not found|页面不存在",
    r"captcha|验证码|security check|安全验证",
    r"access denied|access forbidden|403|access blocked",
    r"please enable javascript|请启用 javascript",
    r"too many requests|请求过于频繁",
    r"under maintenance|维护中",
]

# 最小可用内容长度
MIN_CONTENT_LENGTH = 200
# 最大下载数
MAX_DOWNLOADS = 20
# 单页面超时
PAGE_TIMEOUT = 10


class FetcherAgent(BaseAgent):
    """知识内容获取器"""

    def __init__(self, name, meta, config, message_bus, registry):
        super().__init__(name, meta, config, message_bus, registry)
        self._temp_dir = Path(config.get("temp_dir", tempfile.gettempdir())) / "doc_pipeline_fetcher"
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        # 多 UA 池：规避基础反爬
        self._ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        ]
        self._base_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._retry = int(config.get("retry", 2))
        # 下载量统计
        self._stats = {"attempted": 0, "success": 0, "failed": 0, "filtered": 0}
        self._stats_lock = threading.Lock()
        # 持久化 aiohttp 连接池（复用 TCP 连接，减少握手开销）
        self._aio_session: "aiohttp.ClientSession | None" = None
        self._aio_session_lock = threading.Lock()
        try:
            from selectolax.parser import HTMLParser  # noqa: F401
            self._use_selectolax = True
        except ImportError:
            self._use_selectolax = False
        self.log_info(f"Fetcher v{AGENT_VERSION} 初始化完成，临时目录: {self._temp_dir}"
                      f" | Async I/O: {'启用' if USE_ASYNC else '未安装 aiohttp，使用同步模式'}"
                      f" | HTML解析: {'selectolax' if self._use_selectolax else 'regex'}"
                      f" | UA池: {len(self._ua_pool)} | 重试: {self._retry}")

    def handle(self, msg: Message) -> dict | None:
        """处理搜索结果，下载文章内容"""
        self.report(AgentStatus.RUNNING, "开始获取文章内容...")
        payload = msg.payload
        task_id = payload.get("task_id", "")
        query = payload.get("query", payload.get("queries", [""])[0] if payload.get("queries") else "")
        results = payload.get("results", [])
        max_downloads = payload.get("max_downloads", MAX_DOWNLOADS)

        if not results:
            self.log_info(f"任务 {task_id}: 无搜索结果")
            self.cleanup_task_temp(task_id)
            return {"status": "ok", "task_id": task_id, "articles": [], "query": query}

        results = results[:max_downloads]
        self.log_info(f"任务 {task_id}: 开始下载 {len(results)}/{len(results)} 个页面")

        # 为这个任务创建独立的子目录
        task_dir = self._temp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        with self._stats_lock:
            self._stats = {"attempted": 0, "success": 0, "failed": 0, "filtered": 0}

        use_async = USE_ASYNC
        try:
            asyncio.get_running_loop()
            use_async = False
        except RuntimeError:
            pass
        if use_async:
            articles = asyncio.run(self._fetch_all_async(results, query, task_id, task_dir))
        else:
            articles = self._fetch_all_sync(results, query, task_id, task_dir)

        with self._stats_lock:
            stats_snapshot = dict(self._stats)

        self.log_info(f"任务 {task_id}: 下载完成 ({stats_snapshot['success']}成功/{stats_snapshot['failed']}失败/{stats_snapshot['filtered']}过滤)")

        result = {
            "status": "ok",
            "task_id": task_id,
            "query": query,
            "articles": articles,
            "stats": {
                "total": len(results),
                "attempted": stats_snapshot["attempted"],
                "success": stats_snapshot["success"],
                "failed": stats_snapshot["failed"],
                "filtered": stats_snapshot["filtered"],
            },
        }
        # 注意：不在此处清理临时文件，因为下游 agent (writer) 需要读取 local_path。
        # 临时文件由 orchestrator 在流水线完成后统一清理（cleanup_stale_temp）。
        return result

    async def handle_async(self, msg: Message) -> dict | None:
        """异步处理入口 —— 供异步调用方直接 await，避免 asyncio.run 嵌套。

        与 handle() 逻辑相同，但在已有事件循环中使用。
        """
        self.report(AgentStatus.RUNNING, "开始获取文章内容...")
        payload = msg.payload
        task_id = payload.get("task_id", "")
        query = payload.get("query", payload.get("queries", [""])[0] if payload.get("queries") else "")
        results = payload.get("results", [])
        max_downloads = payload.get("max_downloads", MAX_DOWNLOADS)

        if not results:
            self.log_info(f"任务 {task_id}: 无搜索结果")
            self.cleanup_task_temp(task_id)
            return {"status": "ok", "task_id": task_id, "articles": [], "query": query}

        results = results[:max_downloads]
        self.log_info(f"任务 {task_id}: 开始下载 {len(results)} 个页面")

        task_dir = self._temp_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        with self._stats_lock:
            self._stats = {"attempted": 0, "success": 0, "failed": 0, "filtered": 0}

        if USE_ASYNC:
            articles = await self._fetch_all_async(results, query, task_id, task_dir)
        else:
            articles = await asyncio.to_thread(
                self._fetch_all_sync, results, query, task_id, task_dir
            )

        with self._stats_lock:
            stats_snapshot = dict(self._stats)

        self.log_info(f"任务 {task_id}: 下载完成 ({stats_snapshot['success']}成功/{stats_snapshot['failed']}失败/{stats_snapshot['filtered']}过滤)")

        return {
            "status": "ok",
            "task_id": task_id,
            "query": query,
            "articles": articles,
            "stats": {
                "total": len(results),
                "attempted": stats_snapshot["attempted"],
                "success": stats_snapshot["success"],
                "failed": stats_snapshot["failed"],
                "filtered": stats_snapshot["filtered"],
            },
        }

    def _fetch_all_sync(self, results: list, query: str, task_id: str, task_dir: Path) -> list:
        """同步模式：线程池并发下载"""
        from concurrent.futures import ThreadPoolExecutor
        from functools import partial

        workers = self.config.get("download_workers", 5)
        articles = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(self._fetch_article, r, query, task_id, task_dir, i)
                for i, r in enumerate(results)
            ]
            for i, fut in enumerate(futures):
                self.report(AgentStatus.RUNNING, f"[{i+1}/{len(results)}] 下载中...")
                article = fut.result()
                if article:
                    articles.append(article)
        return articles

    async def _fetch_all_async(self, results: list, query: str, task_id: str, task_dir: Path) -> list:
        """异步模式：aiohttp 并发下载（复用持久化连接池）"""
        workers = self.config.get("download_workers", 5)
        semaphore = asyncio.Semaphore(workers)
        timeout = aiohttp.ClientTimeout(total=PAGE_TIMEOUT)

        # 复用持久化 ClientSession（减少 TCP 握手开销）
        session = await self._get_or_create_session(timeout)
        own_session = False
        if session is None:
            session = aiohttp.ClientSession(
                headers=self._base_headers, timeout=timeout
            )
            own_session = True

        try:
            tasks = [
                self._fetch_article_async(session, semaphore, r, query, task_id, task_dir, idx)
                for idx, r in enumerate(results)
            ]
            articles = await asyncio.gather(*tasks)
        finally:
            if own_session:
                await session.close()
        return [a for a in articles if a]

    async def _get_or_create_session(self, timeout) -> "aiohttp.ClientSession | None":
        """获取或创建持久化 aiohttp ClientSession（线程安全）"""
        if not USE_ASYNC:
            return None
        if self._aio_session is not None and not self._aio_session.closed:
            return self._aio_session
        # 创建新 session（在事件循环中调用）
        self._aio_session = aiohttp.ClientSession(
            headers=self._base_headers, timeout=timeout,
            connector=aiohttp.TCPConnector(
                limit=20,           # 最大并发连接
                limit_per_host=5,   # 单主机最大连接
                ttl_dns_cache=300,  # DNS 缓存 5 分钟
            ),
        )
        return self._aio_session

    async def _close_aio_session(self):
        """关闭持久化连接池"""
        if self._aio_session is not None and not self._aio_session.closed:
            await self._aio_session.close()
        self._aio_session = None

    async def _fetch_article_async(self, session, semaphore, result, query, task_id, task_dir, idx=0):
        """异步下载单页（带 UA 轮换 + 重试）"""
        url = result.get("url", "") if isinstance(result, dict) else getattr(result, "url", "")
        title = result.get("title", "") if isinstance(result, dict) else getattr(result, "title", "")
        if not url:
            return None

        with self._stats_lock:
            self._stats["attempted"] += 1
        last_err = None
        for attempt in range(self._retry + 1):
            ua = self._ua_pool[(idx + attempt) % len(self._ua_pool)]
            try:
                headers = {**self._base_headers, "User-Agent": ua}
                async with semaphore:
                    async with session.get(url, headers=headers, allow_redirects=True) as resp:
                        if resp.status != 200:
                            last_err = f"HTTP {resp.status}"
                            if resp.status in (403, 429, 503):
                                continue
                            with self._stats_lock:
                                self._stats["failed"] += 1
                            self.log_debug(f"  {last_err}: {url[:60]}")
                            return None
                        html = await resp.text()

                plain_text = self._extract_text(html)
                if not self._is_content_usable(plain_text, url, query):
                    with self._stats_lock:
                        self._stats["filtered"] += 1
                    self.log_debug(f"  过滤不可用内容: {url[:60]}")
                    return None

                safe_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff-]', '_', title)[:60] or hashlib.sha256(url.encode()).hexdigest()[:12]
                file_path = task_dir / f"{safe_name}.txt"
                # 异步文件写入（避免阻塞事件循环）
                file_content = (
                    f"标题: {title}\n"
                    f"来源: {url}\n"
                    f"下载时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'='*60}\n\n"
                    f"{plain_text}"
                )
                await asyncio.to_thread(
                    lambda: file_path.write_text(file_content, encoding="utf-8")
                )

                with self._stats_lock:
                    self._stats["success"] += 1
                return {
                    "title": title,
                    "url": url,
                    "local_path": str(file_path),
                    "content_length": len(plain_text),
                    "source": urlparse(url).netloc,
                    "relevance": round(self._content_relevance(plain_text, query), 3),
                }
            except asyncio.TimeoutError:
                last_err = "超时"
                continue
            except Exception as e:
                last_err = str(e)
                continue
        with self._stats_lock:
            self._stats["failed"] += 1
        self.log_debug(f"  下载失败(重试{self._retry}次): {url[:60]} - {last_err}")
        return None

    def _fetch_article(self, result, query: str, task_id: str, task_dir: Path, idx=0) -> dict | None:
        """下载单个页面（同步），提取正文，保存到本地（带 UA 轮换 + 重试）"""
        import requests

        url = result.get("url", "") if isinstance(result, dict) else getattr(result, "url", "")
        title = result.get("title", "") if isinstance(result, dict) else getattr(result, "title", "")
        if not url:
            return None

        with self._stats_lock:
            self._stats["attempted"] += 1
        last_err = None
        for attempt in range(self._retry + 1):
            ua = self._ua_pool[(idx + attempt) % len(self._ua_pool)]
            try:
                headers = {**self._base_headers, "User-Agent": ua}
                with requests.get(url, timeout=PAGE_TIMEOUT, headers=headers,
                                 allow_redirects=True) as r:
                    if r.status_code != 200:
                        last_err = f"HTTP {r.status_code}"
                        if r.status_code in (403, 429, 503):
                            continue
                        with self._stats_lock:
                            self._stats["failed"] += 1
                        self.log_debug(f"  {last_err}: {url[:60]}")
                        return None

                    html = r.text
                    plain_text = self._extract_text(html)

                if not self._is_content_usable(plain_text, url, query):
                    with self._stats_lock:
                        self._stats["filtered"] += 1
                    self.log_debug(f"  过滤不可用内容: {url[:60]}")
                    return None

                safe_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff-]', '_', title)[:60] or hashlib.sha256(url.encode()).hexdigest()[:12]
                file_path = task_dir / f"{safe_name}.txt"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(f"标题: {title}\n")
                    f.write(f"来源: {url}\n")
                    f.write(f"下载时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"{'='*60}\n\n")
                    f.write(plain_text)

                with self._stats_lock:
                    self._stats["success"] += 1
                return {
                    "title": title,
                    "url": url,
                    "local_path": str(file_path),
                    "content_length": len(plain_text),
                    "source": urlparse(url).netloc,
                    "relevance": round(self._content_relevance(plain_text, query), 3),
                }
            except requests.Timeout:
                last_err = "超时"
                continue
            except Exception as e:
                last_err = str(e)
                continue
        with self._stats_lock:
            self._stats["failed"] += 1
        self.log_debug(f"  下载失败(重试{self._retry}次): {url[:60]} - {last_err}")
        return None

    def _extract_text(self, html: str) -> str:
        """从 HTML 中提取可读正文（优先 selectolax C-bindings 解析，回退到正则启发式）"""
        if not html:
            return ""

        # ── 优先：selectolax 解析（C-bindings，比正则快 5-10x）──
        try:
            from selectolax.parser import HTMLParser
            tree = HTMLParser(html)

            # 移除无正文节点
            for tag in ("script", "style", "noscript", "svg", "head", "nav", "footer", "aside"):
                for node in tree.css(tag):
                    node.decompose()

            # 优先 <article> / <main> 容器
            container = tree.css_first("article") or tree.css_first("main") or tree

            # 按块级标签提取段落，用文本密度筛选
            scored = []
            for node in container.css("p, div, section, li, td, blockquote, article, h1, h2, h3, h4, pre"):
                blk = node.text(separator=" ", strip=True)
                if not blk or len(blk) < 30:
                    continue
                # 文本密度：可见字符占比
                text_ratio = len(re.sub(r'\s', '', blk)) / max(len(blk), 1)
                if text_ratio < 0.4:
                    continue
                # 惩罚纯链接行
                link_density = len(re.findall(r'https?://', blk)) / max(len(blk) // 50, 1)
                if link_density > 0.5:
                    continue
                scored.append(blk)

            text = '\n'.join(scored)

            # 兜底：若密度法提取过少，退回到全文档纯文本
            if len(text) < MIN_CONTENT_LENGTH:
                fallback = tree.text(separator=" ", strip=True)
                # 清理 Unicode 噪声字符
                fallback = re.sub(r'[¶§†‡•·]', ' ', fallback)
                fallback = re.sub(r'\s+', ' ', fallback).strip()
                if len(fallback) > len(text):
                    text = fallback

            if text:
                return text[:80000]  # 单页上限 80KB
        except Exception:
            pass  # selectolax 解析失败，回退到正则

        # ── 回退：正则启发式提取（无外部依赖）──
        return self._extract_text_regex(html)

    def _extract_text_regex(self, html: str) -> str:
        """正则启发式 HTML 正文提取（selectolax 不可用时的 fallback）"""
        # 1. 移除明显无正文区域
        html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<noscript[^>]*>.*?</noscript>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<svg[^>]*>.*?</svg>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<head[^>]*>.*?</head>', ' ', html, flags=re.DOTALL | re.IGNORECASE)

        # 2. 优先尝试 <article> / <main> 容器
        main_match = re.search(r'<(article|main)[^>]*>(.*?)</\1>', html, re.DOTALL | re.IGNORECASE)
        region = main_match.group(2) if main_match else html

        # 3. 按块级标签切分为候选段落，用文本密度筛选
        blocks = re.split(r'</?(?:p|div|section|li|td|article|blockquote)[^>]*>', region, flags=re.IGNORECASE)
        scored = []
        for blk in blocks:
            blk = re.sub(r'<[^>]+>', '', blk)          # 去标签
            blk = re.sub(r'&[a-zA-Z]+;', ' ', blk)      # 去命名 HTML 实体（含大写）
            blk = re.sub(r'&#\d+;', ' ', blk)            # 去数字实体
            blk = re.sub(r'&#x[0-9a-fA-F]+;', ' ', blk) # 去十六进制实体
            blk = re.sub(r'[¶§†‡•·]', ' ', blk)         # 去常见 Unicode 噪声字符
            blk = re.sub(r'\s+', ' ', blk).strip()
            if len(blk) < 30:
                continue
            # 文本密度：可见字符占比
            text_ratio = len(re.sub(r'\s', '', blk)) / max(len(blk), 1)
            if text_ratio < 0.4:   # 多半是标签/链接噪声
                continue
            # 惩罚纯链接行（链接文字占比过高）
            link_density = len(re.findall(r'https?://', blk)) / max(len(blk) // 50, 1)
            if link_density > 0.5:
                continue
            scored.append(blk)

        text = '\n'.join(scored)

        # 4. 兜底：若密度法提取过少，退回到全文档去标签
        if len(text) < MIN_CONTENT_LENGTH:
            fallback = re.sub(r'<[^>]+>', ' ', html)
            fallback = re.sub(r'&[a-zA-Z]+;', ' ', fallback)       # 命名实体（含大写）
            fallback = re.sub(r'&#\d+;', ' ', fallback)             # 数字实体
            fallback = re.sub(r'&#x[0-9a-fA-F]+;', ' ', fallback)  # 十六进制实体
            fallback = re.sub(r'[¶§†‡•·]', ' ', fallback)          # Unicode 噪声字符
            fallback = re.sub(r'\s+', ' ', fallback).strip()
            if len(fallback) > len(text):
                text = fallback

        return text[:80000]  # 单页上限 80KB

    def _is_content_usable(self, text: str, url: str, query: str) -> bool:
        """识别内容是否可用（宽松策略：宁可保留，下游质量门控再筛）"""
        # 1. 长度下限
        if len(text) < MIN_CONTENT_LENGTH:
            return False

        # 2. 硬拒绝：明确的错误/拦截页
        for pattern in BAD_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return False

        # 3. 内容级主题相关性：query 中的英文专有名词必须在正文中出现
        #    例如 query="Apache Kafka 核心架构" → "kafka" 必须在正文中出现
        #    这能过滤掉搜索结果中"Apache"匹配但实际是 Apache HTTP Server 的无关页面
        query_en_tokens = re.findall(r'[a-zA-Z]{3,}', query)
        # 排除通用词
        generic_en = {"apache", "the", "and", "for", "with", "from", "that", "this", "are", "was"}
        specific_en = [t.lower() for t in query_en_tokens if t.lower() not in generic_en]
        if specific_en:
            text_lower = text.lower()
            # 至少 1 个专有名词要在正文中出现
            if not any(tok in text_lower for tok in specific_en):
                return False

        return True

    def _content_relevance(self, text: str, query: str) -> float:
        """计算正文与查询的相关度（0~1），供质量评估与排序使用"""
        if not query or not text:
            return 0.0
        q_tokens = set(re.findall(r'[\w\u4e00-\u9fff]{2,}', query.lower()))
        if not q_tokens:
            return 0.0
        t = text.lower()
        hit = sum(1 for tok in q_tokens if tok in t)
        return min(hit / len(q_tokens), 1.0)

    def cleanup_task_temp(self, task_id: str) -> int:
        """清理指定任务的临时文件，返回清理的文件数量"""
        import shutil
        count = 0
        if not self._temp_dir.exists():
            return 0
        for item in self._temp_dir.iterdir():
            if task_id in item.name:
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    count += 1
                except OSError:
                    pass
        if count:
            self.log_info(f"清理任务 {task_id} 的临时文件: {count} 个")
        return count

    def cleanup_stale_temp(self, max_age_hours: int = 24) -> int:
        """清理过期的临时文件（默认 24 小时前）"""
        import shutil
        import time as _time
        cutoff = _time.time() - max_age_hours * 3600
        count = 0
        if not self._temp_dir.exists():
            return 0
        for item in self._temp_dir.iterdir():
            try:
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                    count += 1
            except OSError:
                pass
        if count:
            self.log_info(f"清理过期临时文件: {count} 个（>{max_age_hours}h）")
        return count