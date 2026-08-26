"""Fetcher 安全与健壮性测试：SSRF 校验 / 手动重定向 / cleanup 空串防护 / 原子落盘"""

import asyncio
import socket
from pathlib import Path

import pytest

import agents.fetcher as fetcher_mod
from agents.fetcher import AGENT_DESC, AGENT_PRIORITY, AGENT_VERSION, FetcherAgent
from pipeline_core.base_agent import AgentMeta

PUBLIC_HOST = "public.example.com"
PUBLIC_IP = "93.184.216.34"


def _allow_dns(monkeypatch, mapping=None):
    """把 fetcher 所用 url_guard 的 DNS 解析替换为确定性映射"""
    import pipeline_core.url_guard as url_guard
    mapping = mapping or {}

    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(-2, f"mock dns: {host}")
        entries = []
        for ip in mapping[host]:
            if ":" in ip:
                entries.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 80, 0, 0)))
            else:
                entries.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)))
        return entries

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)


def _make_agent(tmp_path) -> FetcherAgent:
    meta = AgentMeta(name="fetcher", version=AGENT_VERSION,
                     description=AGENT_DESC, priority=AGENT_PRIORITY)
    return FetcherAgent(
        name="fetcher", meta=meta,
        config={
            "cache_dir": str(tmp_path / "cache"),
            "temp_dir": str(tmp_path),
            "log_dir": str(tmp_path / "logs"),
            "quiet": True,
        },
        message_bus=None, registry=None,
    )


class _SyncResp:
    def __init__(self, status_code=200, headers=None, text=""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _AsyncResp:
    def __init__(self, status=200, headers=None, text=""):
        self.status = status
        self.headers = headers or {}
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _AsyncSessionSpy:
    """记录 get() 调用的假 aiohttp session，按序返回响应"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)

    @property
    def requested_urls(self):
        return [u for u, _ in self.calls]


def _usable_html(query_word="kafka"):
    body = f"{query_word} 架构详解。" + "这是一段足够长的正文内容，用于通过长度与文本密度校验阈值。" * 20
    return f"<html><body><article><p>{body}</p></article></body></html>"


@pytest.fixture(autouse=True)
def _no_firecrawl(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)


class TestSyncSSRF:
    def test_direct_private_url_rejected_without_request(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)

        def _must_not_request(url, **kwargs):
            raise AssertionError(f"对被拒 URL 发起了请求: {url}")

        monkeypatch.setattr("requests.get", _must_not_request)
        article = agent._fetch_article(
            {"url": "http://127.0.0.1:8910/admin", "title": "内网"},
            "q", "t1", tmp_path, 0)
        assert article is None

    def test_cloud_metadata_ip_rejected_without_request(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)

        def _must_not_request(url, **kwargs):
            raise AssertionError(f"对云元数据端点发起了请求: {url}")

        monkeypatch.setattr("requests.get", _must_not_request)
        article = agent._fetch_article(
            {"url": "http://169.254.169.254/latest/meta-data/", "title": "metadata"},
            "q", "t1", tmp_path, 0)
        assert article is None

    def test_redirect_to_private_aborted_before_second_request(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})
        requested = []

        def fake_get(url, **kwargs):
            requested.append(url)
            assert kwargs.get("allow_redirects") is False
            return _SyncResp(302, {"Location": "http://127.0.0.1:8910/admin"})

        monkeypatch.setattr("requests.get", fake_get)
        article = agent._fetch_article(
            {"url": f"http://{PUBLIC_HOST}/jump", "title": "跳转"},
            "q", "t1", tmp_path, 0)
        assert article is None
        assert requested == [f"http://{PUBLIC_HOST}/jump"]
        assert agent._stats["failed"] == 1

    def test_redirect_chain_within_limit_succeeds(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})
        html = _usable_html()

        def fake_get(url, **kwargs):
            hops = {
                f"http://{PUBLIC_HOST}/l1": _SyncResp(301, {"Location": "/l2"}),
                f"http://{PUBLIC_HOST}/l2": _SyncResp(302, {"Location": "/final"}),
            }
            return hops.get(url, _SyncResp(200, {}, html))

        monkeypatch.setattr("requests.get", fake_get)
        article = agent._fetch_article(
            {"url": f"http://{PUBLIC_HOST}/l1", "title": "Kafka 指南"},
            "kafka", "t1", tmp_path, 0)
        assert article is not None
        assert Path(article["local_path"]).exists()
        assert article["content_length"] > 0

    def test_redirect_loop_exceeds_limit_aborts(self, tmp_path, monkeypatch):
        from agents.fetcher import MAX_REDIRECTS

        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})
        requested = []

        def fake_get(url, **kwargs):
            requested.append(url)
            return _SyncResp(302, {"Location": f"/loop{len(requested)}"})

        monkeypatch.setattr("requests.get", fake_get)
        article = agent._fetch_article(
            {"url": f"http://{PUBLIC_HOST}/start", "title": "循环"},
            "q", "t1", tmp_path, 0)
        assert article is None
        assert len(requested) == MAX_REDIRECTS + 1

    def test_redirect_to_file_scheme_aborted(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})

        def fake_get(url, **kwargs):
            return _SyncResp(302, {"Location": "file:///etc/passwd"})

        monkeypatch.setattr("requests.get", fake_get)
        article = agent._fetch_article(
            {"url": f"http://{PUBLIC_HOST}/tofile", "title": "x"},
            "q", "t1", tmp_path, 0)
        assert article is None


class TestAsyncSSRFAndSessionLifecycle:
    def test_async_private_url_rejected_without_request(self, tmp_path):
        agent = _make_agent(tmp_path)
        session = _AsyncSessionSpy([])
        result = asyncio.run(agent._fetch_article_async(
            session, asyncio.Semaphore(1),
            {"url": "http://192.168.1.1/router", "title": "内网"},
            "q", "t1", tmp_path, 0))
        assert result is None
        assert session.calls == []

    def test_async_redirect_to_metadata_ip_aborted(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})
        session = _AsyncSessionSpy([
            _AsyncResp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})
        ])
        result = asyncio.run(agent._fetch_article_async(
            session, asyncio.Semaphore(1),
            {"url": f"http://{PUBLIC_HOST}/jump", "title": "跳转"},
            "q", "t1", tmp_path, 0))
        assert result is None
        assert session.requested_urls == [f"http://{PUBLIC_HOST}/jump"]
        for _, kwargs in session.calls:
            assert kwargs.get("allow_redirects") is False

    def test_async_download_success(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})
        session = _AsyncSessionSpy([_AsyncResp(200, {}, _usable_html())])
        result = asyncio.run(agent._fetch_article_async(
            session, asyncio.Semaphore(1),
            {"url": f"http://{PUBLIC_HOST}/doc", "title": "Kafka 文档"},
            "kafka", "t1", tmp_path, 0))
        assert result is not None
        assert Path(result["local_path"]).read_text(encoding="utf-8")

    def _stub_aiohttp(self, monkeypatch, responses):
        created = []

        class _StubSession:
            def __init__(self, **kwargs):
                self.closed_flag = False
                self.kwargs = kwargs
                created.append(self)

            def get(self, url, **kwargs):
                return responses.pop(0)

            async def close(self):
                self.closed_flag = True

        monkeypatch.setattr(fetcher_mod.aiohttp, "ClientSession", _StubSession)
        monkeypatch.setattr(fetcher_mod.aiohttp, "TCPConnector", lambda **kw: object())
        monkeypatch.setattr(fetcher_mod.aiohttp, "ClientTimeout", lambda **kw: object())
        return created

    def test_per_call_session_created_and_closed_each_invocation(self, tmp_path, monkeypatch):
        """回归：session 不再跨事件循环复用，两次独立调用均正常创建并关闭"""
        agent = _make_agent(tmp_path)
        _allow_dns(monkeypatch, {PUBLIC_HOST: [PUBLIC_IP]})
        results = [{"url": f"http://{PUBLIC_HOST}/doc", "title": "T"}]

        responses = [_AsyncResp(200, {}, _usable_html()), _AsyncResp(200, {}, _usable_html())]
        sessions = self._stub_aiohttp(monkeypatch, responses)

        task_dir = tmp_path / "tid"
        task_dir.mkdir(parents=True, exist_ok=True)
        arts1 = asyncio.run(agent._fetch_all_async(results, "kafka", "tid", task_dir))
        arts2 = asyncio.run(agent._fetch_all_async(results, "kafka", "tid", task_dir))

        assert len(arts1) == 1 and len(arts2) == 1
        assert len(sessions) == 2
        assert all(s.closed_flag for s in sessions)

    def test_session_closed_even_when_all_results_rejected(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        sessions = self._stub_aiohttp(monkeypatch, [])
        bad_results = [{"url": "http://127.0.0.1:8910/", "title": "内网"}]
        task_dir = tmp_path / "tid2"
        task_dir.mkdir(parents=True, exist_ok=True)
        articles = asyncio.run(agent._fetch_all_async(bad_results, "q", "tid2", task_dir))
        assert articles == []
        assert len(sessions) == 1
        assert sessions[0].closed_flag


class TestCleanupTaskTempGuard:
    def test_empty_task_id_cleans_nothing(self, tmp_path):
        agent = _make_agent(tmp_path)
        temp = agent._temp_dir
        keep = temp / "some_task"
        keep.mkdir(parents=True)
        (keep / "f.txt").write_text("x", encoding="utf-8")
        loose = temp / "loose.txt"
        loose.write_text("x", encoding="utf-8")
        assert agent.cleanup_task_temp("") == 0
        assert keep.exists() and (keep / "f.txt").exists()
        assert loose.exists()

    def test_exact_and_underscore_prefix_matched_only(self, tmp_path):
        agent = _make_agent(tmp_path)
        temp = agent._temp_dir
        exact = temp / "taskA"
        exact.mkdir(parents=True)
        prefixed = temp / "taskA_extra"
        prefixed.mkdir()
        prefixed_file = temp / "taskA_note.txt"
        prefixed_file.write_text("x", encoding="utf-8")
        other = temp / "taskB"
        other.mkdir()
        count = agent.cleanup_task_temp("taskA")
        assert not exact.exists()
        assert not prefixed.exists()
        assert not prefixed_file.exists()
        assert other.exists()
        assert count == 3

    def test_partial_substring_not_matched(self, tmp_path):
        agent = _make_agent(tmp_path)
        temp = agent._temp_dir
        suffix_num = temp / "taskA2"
        suffix_num.mkdir(parents=True)
        embedded = temp / "xtaskAx"
        embedded.mkdir()
        count = agent.cleanup_task_temp("taskA")
        assert suffix_num.exists(), "'taskA' 子串匹配不应清理 taskA2"
        assert embedded.exists(), "'taskA' 子串匹配不应清理 xtaskAx"
        assert count == 0


class TestAtomicArticleSave:
    def test_same_title_collision_gets_distinct_files(self, tmp_path):
        agent = _make_agent(tmp_path)
        task_dir = tmp_path / "t9"
        task_dir.mkdir(parents=True)
        a1 = agent._save_article("同一标题", "https://a.example/1", "内容一", "q", task_dir)
        a2 = agent._save_article("同一标题", "https://a.example/2", "内容二", "q", task_dir)
        p1, p2 = Path(a1["local_path"]), Path(a2["local_path"])
        assert p1.exists() and p2.exists()
        assert p1 != p2
        assert "同一标题" in p1.read_text(encoding="utf-8")
        assert "内容二" in p2.read_text(encoding="utf-8")
        assert list(task_dir.glob("*.tmp")) == []

    def test_no_tmp_residue_on_success(self, tmp_path):
        agent = _make_agent(tmp_path)
        task_dir = tmp_path / "t9"
        task_dir.mkdir(parents=True)
        agent._save_article("标题", "https://a.example/1", "内容", "q", task_dir)
        assert list(task_dir.iterdir())[0].suffix == ".txt"

    def test_failed_replace_leaves_no_tmp_or_target(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path)
        task_dir = tmp_path / "t9"
        task_dir.mkdir(parents=True)

        def _boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(fetcher_mod.os, "replace", _boom)
        with pytest.raises(OSError):
            agent._save_article("标题", "https://a.example/1", "内容", "q", task_dir)
        assert list(task_dir.iterdir()) == []
