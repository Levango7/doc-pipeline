"""搜索层的 SSRF 防线（2026-07-22 评审旧账 #5 的收口处）。

三条判据分别盯三个不同的失效面，缺一条就有一段是"声明了但没验"：

1. 重定向每一跳都校验（handler 单元 + **真 socket 本地服务**端到端）；
2. 端到端那条同时跑一条**正对照**——用原生 `urlopen` 打同一个跳转服务，
   必须真的取回内网内容。没有这条，"被拦下"在"服务压根没重定向"的夹具下也成立；
3. `FirecrawlExtractor.scrape` 对不可信目标 URL 先拒后发，且**没有发出任何请求**
   （`assert_not_called`），同时用一个公网字面量 URL 证明它不是一刀切。
"""
from __future__ import annotations

import http.server
import threading
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from artesian.search_engines import (
    FirecrawlExtractor,
    _guarded_urlopen,
    _SsrfRedirectHandler,
)

# ────────────────────── 1. 重定向 handler 的判定 ──────────────────────

class TestSsrfRedirectHandler:
    def _req(self, url: str = "http://example.com/search?q=1") -> urllib.request.Request:
        return urllib.request.Request(url)

    def test_private_redirect_is_refused_with_reason(self):
        h = _SsrfRedirectHandler()
        with pytest.raises(urllib.error.HTTPError) as ei:
            h.redirect_request(self._req(), None, 302, "Found", {},
                               "http://169.254.169.254/latest/meta-data/")
        assert "SSRF" in str(ei.value.msg)
        assert "私有" in str(ei.value.msg) or "保留" in str(ei.value.msg)

    def test_loopback_redirect_is_refused(self):
        h = _SsrfRedirectHandler()
        with pytest.raises(urllib.error.HTTPError):
            h.redirect_request(self._req(), None, 301, "Moved", {},
                               "http://127.0.0.1:8080/admin")

    def test_public_redirect_is_forwarded_as_request(self):
        """放行侧也要验：否则 handler 写成"永远拒绝"也能让前两条绿。"""
        h = _SsrfRedirectHandler()
        out = h.redirect_request(self._req(), None, 302, "Found", {},
                                 "http://8.8.8.8/next")
        assert isinstance(out, urllib.request.Request)
        assert out.full_url == "http://8.8.8.8/next"


# ────────────────── 2. 真服务的端到端跳转拦截（含正对照）──────────────────

class _JumpHandler(http.server.BaseHTTPRequestHandler):
    """`/jump` 回 302 指向本机的 /secret；`/secret` 回一段"内网机密"。"""

    def do_GET(self):  # noqa: N802
        if self.path == "/jump":
            host, port = self.server.server_address[:2]
            self.send_response(302)
            self.send_header("Location", f"http://{host}:{port}/secret")
            self.end_headers()
            return
        body = b"INTERNAL-SECRET"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):    # 静音，别把 CI 日志刷脏
        pass


@pytest.fixture()
def jump_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _JumpHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


class TestGuardedUrlopenEndToEnd:
    def test_plain_urlopen_would_have_followed_the_redirect(self, jump_server):
        """正对照：不加护栏时这条跳板真能把内网内容取回来——
        证明下面那条"被拦下"拦的是真实行为，不是夹具自己没跳。"""
        with urllib.request.urlopen(f"{jump_server}/jump", timeout=5) as resp:
            assert resp.read() == b"INTERNAL-SECRET"

    def test_guarded_urlopen_blocks_the_redirect(self, jump_server):
        req = urllib.request.Request(f"{jump_server}/jump")
        with pytest.raises(urllib.error.HTTPError) as ei:
            _guarded_urlopen(req, timeout=5)
        assert "SSRF" in str(ei.value.msg)


# ────────────────── 3. Firecrawl 的不可信目标 URL ──────────────────

class TestFirecrawlTargetGuard:
    def _ext(self):
        return FirecrawlExtractor(api_key="k")

    def test_metadata_endpoint_refused_and_no_egress(self):
        ext = self._ext()
        with patch("artesian.search_engines._guarded_urlopen") as open_mock:
            out = ext.scrape("http://169.254.169.254/latest/meta-data/")
        assert out["success"] is False
        assert "私有" in out["error"] or "保留" in out["error"]
        open_mock.assert_not_called()        # 关键：一个包都没发出去

    def test_localhost_and_file_scheme_refused(self):
        ext = self._ext()
        with patch("artesian.search_engines._guarded_urlopen") as open_mock:
            assert ext.scrape("http://localhost:8080/x")["success"] is False
            assert ext.scrape("file:///etc/passwd")["success"] is False
        open_mock.assert_not_called()

    def test_public_target_still_goes_out(self):
        """放行侧：公网字面量目标照常发请求，拒的是不可信地址而不是一刀切。"""
        ext = self._ext()
        resp = MagicMock()
        resp.read.return_value = b'{"data": {"markdown": "# ok", "title": "t"}}'
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda *a: False
        with patch("artesian.search_engines._guarded_urlopen",
                   return_value=resp) as open_mock:
            out = ext.scrape("http://8.8.8.8/page")
        open_mock.assert_called_once()
        assert out["success"] is True and out["markdown"] == "# ok"

    def test_no_key_short_circuits_before_guard(self):
        """无 key 的旧行为不变：先报 no API key，不去解析目标。"""
        out = FirecrawlExtractor(api_key="").scrape("http://169.254.169.254/")
        assert out["error"] == "no API key"
