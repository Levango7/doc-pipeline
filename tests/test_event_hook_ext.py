"""EventHook 补充测试 — 补齐覆盖率薄弱区（原 49%）。

覆盖范围：
- Hook.matches 通配符语义 / to_dict 序列化
- EventHookManager：register/unregister/enable/disable/list/emit（callback 模式）
- webhook 异步引擎：fake aiohttp 注入 + SSRF 校验桩 → 投递、重定向跟随、SSRF 拒绝、
  队列满丢弃、优雅关闭（shutdown_webhook）
- 全局单例 get_hook_manager / emit_event
"""
import asyncio
import sys
import time
import types

import pytest

import pipeline_core.event_hook as eh
from pipeline_core.event_hook import (
    EventHookManager,
    Hook,
    _safe_enqueue,
    emit_event,
    get_hook_manager,
    shutdown_webhook,
)


class TestHookMatching:
    def test_exact_match(self):
        h = Hook(id="1", event="task.completed")
        assert h.matches("task.completed")
        assert not h.matches("task.failed")

    def test_global_wildcard(self):
        h = Hook(id="1", event="*")
        assert h.matches("anything.at.all")

    def test_prefix_wildcard(self):
        h = Hook(id="1", event="task.*")
        assert h.matches("task.completed")
        assert h.matches("task.failed")
        assert not h.matches("agent.started")
        assert not h.matches("taskx.completed")  # 前缀必须带点边界

    def test_disabled_never_matches(self):
        h = Hook(id="1", event="*", enabled=False)
        assert not h.matches("task.completed")

    def test_to_dict_excludes_callback(self):
        h = Hook(id="1", event="e", callback=lambda e, p: None)
        d = h.to_dict()
        assert "callback" not in d
        assert d["event"] == "e"


class TestManagerCallbackMode:
    def test_register_emit_unregister(self):
        mgr = EventHookManager()
        got = []
        hook_id = mgr.register("task.completed", callback=lambda e, p: got.append((e, p)))
        assert mgr.emit("task.completed", {"x": 1}) == 1
        assert got == [("task.completed", {"x": 1})]
        assert mgr.unregister(hook_id) is True
        assert mgr.unregister(hook_id) is False  # 重复注销
        assert mgr.emit("task.completed", {}) == 0

    def test_enable_disable(self):
        mgr = EventHookManager()
        got = []
        hook_id = mgr.register("e", callback=lambda e, p: got.append(1))
        assert mgr.disable(hook_id) is True
        assert mgr.emit("e", {}) == 0 and not got
        assert mgr.enable(hook_id) is True
        assert mgr.emit("e", {}) == 1 and got
        assert mgr.disable("ghost") is False
        assert mgr.enable("ghost") is False

    def test_list_hooks(self):
        mgr = EventHookManager()
        mgr.register("e1", url="https://x.com")
        hooks = mgr.list_hooks()
        assert len(hooks) == 1 and hooks[0]["event"] == "e1"

    def test_emit_no_match_returns_zero(self):
        mgr = EventHookManager()
        assert mgr.emit("nobody.listens", {}) == 0

    def test_callback_exception_counted_as_failure(self):
        mgr = EventHookManager()

        def _boom(e, p):
            raise RuntimeError("cb down")

        ok = []
        mgr.register("e", callback=_boom)
        mgr.register("e", callback=lambda e, p: ok.append(1))
        assert mgr.emit("e", {}) == 1  # 仅成功的计入
        assert ok

    def test_clear(self):
        mgr = EventHookManager()
        mgr.register("e", callback=lambda e, p: None)
        mgr.clear()
        assert mgr.list_hooks() == []


class TestSingleton:
    def test_get_hook_manager_is_singleton(self):
        assert get_hook_manager() is get_hook_manager()

    def test_emit_event_convenience(self):
        mgr = get_hook_manager()
        got = []
        hook_id = mgr.register("conv.test", callback=lambda e, p: got.append(1))
        try:
            assert emit_event("conv.test", {}) == 1
        finally:
            mgr.unregister(hook_id)


# ─── webhook 异步引擎（fake aiohttp 注入，无真实网络） ──────

class _RecordingAiohttp:
    """记录所有 post 请求的假 aiohttp 模块工厂"""

    def __init__(self, redirect_map=None):
        self.posts = []  # [(url, body)]
        self.redirect_map = redirect_map or {}  # url -> (status, location)

    def module(self):
        mod = types.ModuleType("aiohttp")
        outer = self

        class _Resp:
            def __init__(self, url):
                if url in outer.redirect_map:
                    self.status, loc = outer.redirect_map[url]
                    self.headers = {"Location": loc}
                else:
                    self.status = 200
                    self.headers = {}

            async def json(self):
                return {}

        class _PostCM:
            def __init__(self, url):
                self._url = url

            async def __aenter__(self):
                outer.posts.append(self._url)
                return _Resp(self._url)

            async def __aexit__(self, *a):
                return False

        class _Session:
            def __init__(self, timeout=None, connector=None):
                self.closed = False

            def post(self, url, data=None, headers=None, allow_redirects=True):
                return _PostCM(url)

            async def close(self):
                self.closed = True

        mod.ClientSession = _Session
        mod.ClientTimeout = lambda total=None: None
        mod.TCPConnector = lambda **kw: None
        return mod


def _wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


@pytest.fixture
def clean_engine():
    """确保每个 webhook 引擎用例前后引擎处于关闭状态"""
    shutdown_webhook(timeout_s=5)
    yield
    shutdown_webhook(timeout_s=5)


class TestWebhookEngine:
    def test_webhook_delivered(self, monkeypatch, clean_engine):
        rec = _RecordingAiohttp()
        monkeypatch.setitem(sys.modules, "aiohttp", rec.module())
        monkeypatch.setattr(eh, "validate_public_http_url", lambda u: (True, ""))
        mgr = EventHookManager()
        mgr.register("task.done", url="https://hooks.example.com/cb",
                     headers={"X-Extra": "1"})
        assert mgr.emit("task.done", {"k": "v"}) == 1
        assert _wait_for(lambda: "https://hooks.example.com/cb" in rec.posts)

    def test_webhook_follows_redirect(self, monkeypatch, clean_engine):
        rec = _RecordingAiohttp(redirect_map={
            "https://hooks.example.com/a": (302, "https://hooks.example.com/b")})
        monkeypatch.setitem(sys.modules, "aiohttp", rec.module())
        monkeypatch.setattr(eh, "validate_public_http_url", lambda u: (True, ""))
        mgr = EventHookManager()
        mgr.register("e", url="https://hooks.example.com/a")
        mgr.emit("e", {})
        assert _wait_for(lambda: "https://hooks.example.com/b" in rec.posts)

    def test_webhook_redirect_missing_location_stops(self, monkeypatch, clean_engine):
        rec = _RecordingAiohttp(redirect_map={
            "https://hooks.example.com/a": (302, "")})
        monkeypatch.setitem(sys.modules, "aiohttp", rec.module())
        monkeypatch.setattr(eh, "validate_public_http_url", lambda u: (True, ""))
        mgr = EventHookManager()
        mgr.register("e", url="https://hooks.example.com/a")
        mgr.emit("e", {})
        assert _wait_for(lambda: len(rec.posts) >= 1)
        time.sleep(0.2)
        assert rec.posts.count("https://hooks.example.com/a") == 1  # 无后续跳转

    def test_webhook_ssrf_rejected_before_post(self, monkeypatch, clean_engine):
        rec = _RecordingAiohttp()
        monkeypatch.setitem(sys.modules, "aiohttp", rec.module())
        monkeypatch.setattr(eh, "validate_public_http_url",
                            lambda u: (False, "private ip"))
        mgr = EventHookManager()
        mgr.register("e", url="https://internal.example.com/x")
        mgr.emit("e", {})
        time.sleep(0.5)  # 给引擎处理时间
        assert rec.posts == []  # SSRF 拒绝，不发起请求

    def test_safe_enqueue_full_queue_drops(self):
        q = asyncio.Queue(maxsize=1)
        q.put_nowait("x")
        _safe_enqueue(q, "y")  # 满 → 仅告警，不抛异常
        assert q.qsize() == 1

    def test_shutdown_without_engine_is_noop(self):
        shutdown_webhook()  # 引擎未启动 → 直接返回
        shutdown_webhook()
