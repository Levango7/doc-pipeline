"""FetcherAgent 补充测试 — 补齐覆盖率薄弱区（原 54%）。

与 test_fetcher_security.py 互补（后者覆盖 SSRF/重定向/原子写入），本文件覆盖：
- handle / handle_async 主流程与统计聚合
- _fetch_all_sync / _fetch_all_async 并发下载
- _fetch_article 同步/异步：Firecrawl 优先路径、内容过滤、重试与中止
- _download_html_sync 状态码分支（缺 Location / 4xx 中止 / 429 重试）
- _extract_text（selectolax）与 _extract_text_regex（正则回退）
- _is_content_usable / _content_relevance / cleanup_stale_temp / on_stop

网络隔离：与本库 test_fetcher_security.py 的 _allow_dns 模式一致，
fixture 级 mock url_guard 的 DNS 解析为确定性映射，测试不依赖外网 DNS。
"""
import asyncio
import os
import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agents.fetcher as fetcher_mod
import pipeline_core.url_guard as url_guard
from agents.fetcher import (
    FetcherAgent,
    _DownloadAbort,
    _DownloadRetry,
)
from pipeline_core.base_agent import AgentMeta, Message

# 测试域名 → 公网 IP 的确定性映射（url_guard SSRF 校验用，不真实出网）
_DNS_MAP = {
    "good.com": ["93.184.216.34"],
    "a.com": ["93.184.216.34"],
}


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """把 url_guard 的 DNS 解析替换为确定性映射，防止依赖外网（CI 网络受限时红）"""
    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in _DNS_MAP:
            raise socket.gaierror(-2, f"stub dns: {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
                for ip in _DNS_MAP[host]]
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)


def _make_agent(tmp_path, **cfg) -> FetcherAgent:
    config = {"temp_dir": str(tmp_path / "tmp"), "quiet": True, "retry": 1}
    config.update(cfg)
    return FetcherAgent(
        "fetcher", AgentMeta(name="fetcher", version="1.0"), config, None, None)


def _msg(payload: dict) -> Message:
    return Message(topic="fetcher.input", payload=payload, from_agent="test")


def _usable_html(word="kafka"):
    # 每段 >30 字符（通过密度筛选），总长 >200（MIN_CONTENT_LENGTH），且含查询词
    para = (
        f"{word} 是分布式消息队列，这是足够长的正文段落，用于满足最小长度阈值，"
        f"包含足够多的字符以通过内容可用性检查，并且明确提及了 {word} 关键词。"
    )
    return (
        "<html><head><title>t</title></head><body><article>"
        + "".join(f"<p>{para}</p>" for _ in range(4))
        + "</article></body></html>"
    )


class TestHandle:
    def test_no_results_returns_empty(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg({"task_id": "t1", "results": []}))
        assert res["status"] == "ok" and res["articles"] == []

    def test_sync_path_aggregates_stats(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        monkeypatch.setattr(fetcher_mod, "USE_ASYNC", False)
        # mock 下载层而非 _fetch_article，让真实 _fetch_article 累计 attempted/success
        agent._download_html_sync = MagicMock(return_value=_usable_html())
        res = agent.handle(_msg({
            "task_id": "t2",
            "query": "kafka",
            "results": [{"url": "https://good.com/a", "title": "A"},
                        {"url": "", "title": "B"}],  # 空 URL 提前返回，不计 attempted
        }))
        assert res["stats"]["total"] == 2
        assert len(res["articles"]) == 1
        assert res["stats"]["attempted"] == 1
        assert res["stats"]["success"] == 1

    def test_max_downloads_truncates(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        monkeypatch.setattr(fetcher_mod, "USE_ASYNC", False)
        agent._fetch_article = MagicMock(return_value=None)
        res = agent.handle(_msg({
            "task_id": "t3", "max_downloads": 1,
            "results": [{"url": f"https://x{i}.com"} for i in range(5)],
        }))
        assert res["stats"]["total"] == 1

    def test_handle_async(self, tmp_path):
        agent = _make_agent(tmp_path)

        async def _fake_fetch(session, semaphore, result, query, task_id, task_dir, idx=0):
            return {"title": "AS", "url": result["url"], "local_path": "x",
                    "content_length": 1, "source": "s", "relevance": 0.1}

        agent._fetch_article_async = _fake_fetch
        res = asyncio.run(agent.handle_async(_msg({
            "task_id": "t4", "query": "q",
            "results": [{"url": "https://a.com", "title": "A"}],
        })))
        assert res["status"] == "ok"
        assert len(res["articles"]) == 1

    def test_handle_async_no_results(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = asyncio.run(agent.handle_async(_msg({"task_id": "t5", "results": []})))
        assert res["articles"] == []


class TestFetchArticleSync:
    def test_empty_url_returns_none(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent._fetch_article({"url": "", "title": "T"}, "q", "t",
                                    tmp_path) is None

    def test_private_url_skipped(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent._fetch_article({"url": "http://127.0.0.1/x", "title": "T"},
                                    "q", "t", tmp_path) is None

    def test_firecrawl_success(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._firecrawl = MagicMock()
        agent._firecrawl.is_available.return_value = True
        agent._firecrawl.scrape.return_value = {
            "success": True, "markdown": "kafka kafka kafka " * 40,
            "title": "FC 标题", "error": ""}
        out = agent._fetch_article({"url": "https://good.com/a", "title": ""},
                                   "kafka", "t", tmp_path)
        assert out is not None
        assert out["title"] == "FC 标题"  # 标题取自 firecrawl
        assert Path(out["local_path"]).exists()

    def test_firecrawl_filtered_falls_back_to_html(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._firecrawl = MagicMock()
        agent._firecrawl.is_available.return_value = True
        agent._firecrawl.scrape.return_value = {
            "success": True, "markdown": "too short", "title": "", "error": ""}
        agent._download_html_sync = MagicMock(return_value=_usable_html())
        out = agent._fetch_article({"url": "https://good.com/a", "title": "T"},
                                   "kafka", "t", tmp_path)
        assert out is not None  # HTML 回退成功
        agent._download_html_sync.assert_called()

    def test_firecrawl_error_logged_then_fallback(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._firecrawl = MagicMock()
        agent._firecrawl.is_available.return_value = True
        agent._firecrawl.scrape.side_effect = RuntimeError("fc down")
        agent._download_html_sync = MagicMock(return_value=_usable_html())
        out = agent._fetch_article({"url": "https://good.com/a", "title": "T"},
                                   "kafka", "t", tmp_path)
        assert out is not None

    def test_html_success(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._download_html_sync = MagicMock(return_value=_usable_html())
        out = agent._fetch_article({"url": "https://good.com/a", "title": "T"},
                                   "kafka", "t", tmp_path)
        assert out is not None and out["content_length"] > 200

    def test_content_filtered(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._download_html_sync = MagicMock(
            return_value="<html><body>404 not found</body></html>")
        assert agent._fetch_article({"url": "https://good.com/a", "title": "T"},
                                    "kafka", "t", tmp_path) is None

    def test_abort_no_retry(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._download_html_sync = MagicMock(
            side_effect=_DownloadAbort("HTTP 404"))
        assert agent._fetch_article({"url": "https://good.com/a", "title": "T"},
                                    "kafka", "t", tmp_path) is None
        agent._download_html_sync.assert_called_once()  # 中止不重试

    def test_retry_then_fail(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._download_html_sync = MagicMock(side_effect=RuntimeError("flaky"))
        assert agent._fetch_article({"url": "https://good.com/a", "title": "T"},
                                    "kafka", "t", tmp_path) is None
        assert agent._download_html_sync.call_count == 2  # retry=1 → 共 2 次


class TestDownloadHtmlSyncStatusCodes:
    class _Resp:
        def __init__(self, status_code, headers=None, text=""):
            self.status_code = status_code
            self.headers = headers or {}
            self.text = text

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_redirect_missing_location_aborts(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch("requests.get", return_value=self._Resp(302, {})), pytest.raises(_DownloadAbort, match="缺少 Location"):
            agent._download_html_sync("https://good.com/a", {})

    def test_404_aborts(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch("requests.get", return_value=self._Resp(404)), pytest.raises(_DownloadAbort, match="404"):
            agent._download_html_sync("https://good.com/a", {})

    def test_429_retries(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch("requests.get", return_value=self._Resp(429)), pytest.raises(_DownloadRetry):
            agent._download_html_sync("https://good.com/a", {})

    def test_200_returns_text(self, tmp_path):
        agent = _make_agent(tmp_path)
        with patch("requests.get",
                   return_value=self._Resp(200, text="<html>ok</html>")):
            assert agent._download_html_sync("https://good.com/a", {}) == "<html>ok</html>"


class TestExtractText:
    def test_empty_html(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent._extract_text("") == ""

    def test_selectolax_path(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent._use_selectolax  # 环境已安装 selectolax
        html = (
            "<html><head><script>var x=1;</script></head><body>"
            "<nav>导航 首页 新闻 体育</nav>"
            "<article>"
            "<p>这是文章的第一段正文，内容足够长，密度也足够高，应当被提取出来。</p>"
            "<p>这是文章的第二段正文，同样足够长，应当被提取出来进入结果。</p>"
            "</article></body></html>"
        )
        text = agent._extract_text(html)
        assert "第一段正文" in text
        assert "var x=1" not in text

    def test_selectolax_fallback_to_full_text(self, tmp_path):
        agent = _make_agent(tmp_path)
        # 无块级标签 → 密度法提取为空 → 回退全文档纯文本
        html = "<html><body>" + "连续纯文本没有标签包裹但是足够长。" * 30 + "</body></html>"
        text = agent._extract_text(html)
        assert "连续纯文本" in text

    def test_regex_fallback_extraction(self, tmp_path):
        agent = _make_agent(tmp_path)
        html = (
            "<html><head><style>.a{color:red}</style>"
            "<script>console.log(1)</script></head><body>"
            "<article>"
            "<p>正则提取的第一段正文，长度足够，实体 &amp; 会被清理掉。</p>"
            "<p>短</p>"
            "<p>正则提取的第二段正文，长度足够，应当出现在最终结果里面。</p>"
            "</article></body></html>"
        )
        text = agent._extract_text_regex(html)
        assert "第一段正文" in text
        assert "console.log" not in text
        assert "color:red" not in text

    def test_regex_density_filters_link_heavy_block(self, tmp_path):
        agent = _make_agent(tmp_path)
        # 好段落足够长（scored ≥ MIN_CONTENT_LENGTH，不触发全文兜底），
        # 链接堆砌块被 link_density 过滤掉
        good = ("kafka 是分布式消息队列，这是足够长的正文段落，用于满足最小长度阈值，"
                "包含足够多的字符以通过密度筛选。") * 6
        links = " ".join(f"https://spam.com/{i}" for i in range(30))
        html = f"<div>{links}</div><p>{good}</p>"
        text = agent._extract_text_regex(html)
        assert "spam.com" not in text
        assert "kafka" in text

    def test_regex_fallback_full_text(self, tmp_path):
        agent = _make_agent(tmp_path)
        html = "<html><body>" + "无标签纯文本内容足够长。" * 40 + "</body></html>"
        text = agent._extract_text_regex(html)
        assert "无标签纯文本" in text


class TestContentUsability:
    def test_short_text_unusable(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert not agent._is_content_usable("short", "https://x.com", "q")

    def test_bad_pattern_unusable(self, tmp_path):
        agent = _make_agent(tmp_path)
        text = "x" * 300 + " 404 not found " + "y" * 300
        assert not agent._is_content_usable(text, "https://x.com", "q")

    def test_missing_query_token_unusable(self, tmp_path):
        agent = _make_agent(tmp_path)
        text = "这是一段与查询完全无关的中文内容，长度足够。" * 20
        assert not agent._is_content_usable(text, "https://x.com", "Kafka 架构")

    def test_good_content_usable(self, tmp_path):
        agent = _make_agent(tmp_path)
        text = "Kafka 是分布式消息队列。" * 50
        assert agent._is_content_usable(text, "https://x.com", "Kafka 架构")

    def test_content_relevance(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent._content_relevance("", "q") == 0.0
        assert agent._content_relevance("text", "") == 0.0
        score = agent._content_relevance("kafka 架构 设计 实践", "kafka 架构")
        assert score == 1.0


class TestCleanupStaleTemp:
    def test_stale_files_removed_fresh_kept(self, tmp_path):
        agent = _make_agent(tmp_path)
        stale_dir = agent._temp_dir / "old_task"
        stale_dir.mkdir(parents=True)
        stale_file = stale_dir / "a.txt"
        stale_file.write_text("x", encoding="utf-8")
        old = time.time() - 48 * 3600
        os.utime(stale_dir, (old, old))
        os.utime(stale_file, (old, old))

        fresh_dir = agent._temp_dir / "new_task"
        fresh_dir.mkdir()

        count = agent.cleanup_stale_temp(max_age_hours=24)
        assert count >= 1
        assert not stale_file.exists()
        assert fresh_dir.exists()

    def test_missing_temp_dir_returns_zero(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent._temp_dir = tmp_path / "ghost"
        assert agent.cleanup_stale_temp() == 0


class TestOnStop:
    def test_on_stop_without_session(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.on_stop()  # 不应抛异常
        assert agent._aio_session is None

    def test_on_stop_closes_open_session(self, tmp_path):
        agent = _make_agent(tmp_path)
        closed = []

        class _FakeSession:
            closed = False

            async def close(self):
                closed.append(True)

        agent._aio_session = _FakeSession()
        agent.on_stop()
        assert closed and agent._aio_session is None
