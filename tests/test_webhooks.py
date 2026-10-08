"""入站 webhook（pipeline_core/webhooks.py + AdminHandler 的 HTTP 粘合层）测试。

覆盖：配置校验 / 两种鉴权方案（HMAC 签名与 Token）/ fail-closed（密钥未配置
拒绝一切）/ 审计留痕（成败都落 JSONL，写盘失败如实回报）/ 与手动 run 同一
提交路径（FakeOrch 断言 run_plan 入参）。
"""
import hashlib
import hmac
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline_core import webhooks as wh
from pipeline_core.admin_api import AdminHandler
from pipeline_core.webhooks import (
    SIGNATURE_HEADER,
    TOKEN_HEADER,
    Webhook,
    WebhookConfigError,
    handle_inbound,
    load_webhooks,
    verify_credentials,
)

BASIC = """
webhooks:
  - name: deploy
    pipeline: docgen
    secret_env: TEST_WH_SECRET
"""


def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "webhooks.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _registry(**kw) -> dict:
    return {"deploy": Webhook(name="deploy", pipeline="docgen",
                              secret_env="TEST_WH_SECRET", **kw)}


class FakeOrch:
    def __init__(self, task_id="cafecafecafecafe"):
        self.calls: list[dict] = []
        self._task_id = task_id

    def run_plan(self, plan, input_file=None, task_id=None, wait=True):
        self.calls.append({"plan": plan, "input_file": input_file,
                           "task_id": task_id, "wait": wait})
        return SimpleNamespace(id=self._task_id)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("DOC_PIPELINE_STATE_DIR", str(d))
    return d


def _audit_lines(state_dir: Path) -> list[dict]:
    p = state_dir / "audit" / "webhooks.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


class TestLoadWebhooks:
    def test_loads_valid(self, tmp_path):
        hooks = load_webhooks(_write_cfg(tmp_path, BASIC), available=["docgen"])
        assert set(hooks) == {"deploy"}
        assert hooks["deploy"].pipeline == "docgen"
        assert hooks["deploy"].enabled is True
        assert hooks["deploy"].secret_env == "TEST_WH_SECRET"

    def test_missing_file_is_dormant_not_error(self, tmp_path):
        assert load_webhooks(tmp_path / "nope.yaml", available=["docgen"]) == {}

    def test_missing_key(self, tmp_path):
        with pytest.raises(WebhookConfigError, match="webhooks"):
            load_webhooks(_write_cfg(tmp_path, "x: 1"), available=[])

    def test_empty_list(self, tmp_path):
        with pytest.raises(WebhookConfigError, match="非空"):
            load_webhooks(_write_cfg(tmp_path, "webhooks: []"), available=[])

    def test_unknown_pipeline_lists_available(self, tmp_path):
        with pytest.raises(WebhookConfigError, match="可用: docgen"):
            load_webhooks(_write_cfg(tmp_path, """
webhooks:
  - {name: a, pipeline: nope, secret_env: S}
"""), available=["docgen"])

    def test_secret_env_required(self, tmp_path):
        with pytest.raises(WebhookConfigError, match="secret_env"):
            load_webhooks(_write_cfg(tmp_path, """
webhooks:
  - {name: a, pipeline: docgen}
"""), available=["docgen"])

    def test_duplicate_name(self, tmp_path):
        with pytest.raises(WebhookConfigError, match="重复"):
            load_webhooks(_write_cfg(tmp_path, """
webhooks:
  - {name: a, pipeline: docgen, secret_env: S1}
  - {name: a, pipeline: docgen, secret_env: S2}
"""), available=["docgen"])

    def test_enabled_must_be_bool(self, tmp_path):
        with pytest.raises(WebhookConfigError, match="enabled"):
            load_webhooks(_write_cfg(tmp_path, """
webhooks:
  - {name: a, pipeline: docgen, secret_env: S, enabled: "yes"}
"""), available=["docgen"])


class TestVerifyCredentials:
    SECRET = "topsecret"
    BODY = b'{"a": 1}'

    def test_signature_ok(self):
        h = {SIGNATURE_HEADER: _sign(self.SECRET, self.BODY)}
        assert verify_credentials(self.SECRET, self.BODY, h) == "signature"

    def test_signature_without_prefix_ok(self):
        raw = hmac.new(self.SECRET.encode(), self.BODY, hashlib.sha256).hexdigest()
        assert verify_credentials(self.SECRET, self.BODY,
                                  {SIGNATURE_HEADER: raw}) == "signature"

    def test_lowercase_header_key_ok(self):
        """headers 可能是普通 dict（大小写敏感）——取头要折叠大小写。"""
        h = {SIGNATURE_HEADER.lower(): _sign(self.SECRET, self.BODY)}
        assert verify_credentials(self.SECRET, self.BODY, h) == "signature"

    def test_signature_wrong(self):
        assert verify_credentials(self.SECRET, self.BODY,
                                  {SIGNATURE_HEADER: "sha256=deadbeef"}) == ""

    def test_body_tamper_breaks_signature(self):
        h = {SIGNATURE_HEADER: _sign(self.SECRET, self.BODY)}
        assert verify_credentials(self.SECRET, b'{"a": 2}', h) == ""

    def test_token_ok(self):
        assert verify_credentials(self.SECRET, b"", {TOKEN_HEADER: self.SECRET}) == "token"

    def test_token_wrong(self):
        assert verify_credentials(self.SECRET, b"", {TOKEN_HEADER: "nope"}) == ""

    def test_no_credentials(self):
        assert verify_credentials(self.SECRET, b"", {}) == ""

    def test_signature_precedence_over_token(self):
        h = {SIGNATURE_HEADER: _sign(self.SECRET, self.BODY), TOKEN_HEADER: "nope"}
        assert verify_credentials(self.SECRET, self.BODY, h) == "signature"


class TestHandleInbound:
    def test_unknown_404_and_audited(self, state_dir):
        status, payload = handle_inbound("nope", _registry(), b"", headers={},
                                         remote="1.2.3.4", orch=FakeOrch())
        assert status == 404 and payload["audit"] == "ok"
        rec = _audit_lines(state_dir)[-1]
        assert rec["outcome"] == "rejected" and rec["reason"] == "unknown"
        assert rec["webhook"] == "nope" and rec["remote"] == "1.2.3.4"

    def test_disabled_403(self, state_dir):
        status, _ = handle_inbound("deploy", _registry(enabled=False), b"",
                                   headers={}, remote="r", orch=FakeOrch())
        assert status == 403
        assert _audit_lines(state_dir)[-1]["reason"] == "disabled"

    def test_secret_missing_503_fail_closed(self, state_dir, monkeypatch):
        monkeypatch.delenv("TEST_WH_SECRET", raising=False)
        status, payload = handle_inbound("deploy", _registry(), b"{}", headers={},
                                         remote="r", orch=FakeOrch())
        assert status == 503 and "TEST_WH_SECRET" in payload["error"]
        assert _audit_lines(state_dir)[-1]["reason"] == "secret_missing"

    def test_bad_credentials_401(self, state_dir, monkeypatch):
        monkeypatch.setenv("TEST_WH_SECRET", "s3cret")
        status, _ = handle_inbound("deploy", _registry(), b"{}",
                                   headers={SIGNATURE_HEADER: "sha256=bad"},
                                   remote="r", orch=FakeOrch())
        assert status == 401
        assert _audit_lines(state_dir)[-1]["reason"] == "bad_credentials"

    def test_accepted_json_payload_submits_run(self, state_dir, monkeypatch):
        monkeypatch.setenv("TEST_WH_SECRET", "s3cret")
        body = json.dumps({"topic": "季度营收", "files": ["a.pdf"]}).encode()
        orch = FakeOrch()
        status, payload = handle_inbound(
            "deploy", _registry(), body,
            headers={SIGNATURE_HEADER: _sign("s3cret", body)},
            remote="r", orch=orch)
        assert status == 202
        assert payload["task_id"] == "cafecafecafecafe"
        call = orch.calls[0]
        assert call["wait"] is False                      # 入站不阻塞 HTTP 线程
        text = Path(call["input_file"]).read_text(encoding="utf-8")
        assert "## topic" in text and "季度营收" in text and "- a.pdf" in text
        rec = _audit_lines(state_dir)[-1]
        assert rec["outcome"] == "accepted" and rec["scheme"] == "signature"
        assert rec["task_id"] == "cafecafecafecafe"

    def test_accepted_raw_text_payload(self, state_dir, monkeypatch):
        monkeypatch.setenv("TEST_WH_SECRET", "s3cret")
        body = "第一行主题\n资料.pdf".encode()
        orch = FakeOrch()
        status, _ = handle_inbound("deploy", _registry(), body,
                                   headers={TOKEN_HEADER: "s3cret"},
                                   remote="r", orch=orch)
        assert status == 202
        text = Path(orch.calls[0]["input_file"]).read_text(encoding="utf-8")
        assert "第一行主题" in text
        assert _audit_lines(state_dir)[-1]["scheme"] == "token"

    def test_submit_failure_500_and_audited(self, state_dir, monkeypatch):
        monkeypatch.setenv("TEST_WH_SECRET", "s3cret")

        class BoomOrch(FakeOrch):
            def run_plan(self, *a, **kw):
                raise RuntimeError("engine down")

        status, payload = handle_inbound("deploy", _registry(), b"{}",
                                         headers={TOKEN_HEADER: "s3cret"},
                                         remote="r", orch=BoomOrch())
        assert status == 500 and "submit failed" in payload["error"]
        assert _audit_lines(state_dir)[-1]["reason"] == "submit_failed"

    def test_audit_write_failure_is_reported_not_silent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_WH_SECRET", "s3cret")
        blocker = tmp_path / "blocked"
        blocker.write_text("file", encoding="utf-8")     # 以文件冒充目录 → mkdir 必败
        monkeypatch.setattr(wh, "_audit_path",
                            lambda: blocker / "audit" / "webhooks.jsonl")
        status, payload = handle_inbound("deploy", _registry(), b"{}",
                                         headers={TOKEN_HEADER: "s3cret"},
                                         remote="r", orch=FakeOrch())
        assert status == 202
        assert "audit_error" in payload and "audit" not in payload


class _FakeHTTP:
    """模拟 AdminHandler 的实例接口（_handle_webhook_inbound 只用到这几个属性）。"""

    def __init__(self, path, body=b"", headers=None, webhooks=None, orch=None):
        self.path = path
        merged = {"Content-Length": str(len(body))}
        merged.update(headers or {})
        self.headers = merged
        self.rfile = io.BytesIO(body)
        self.client_address = ("9.9.9.9", 12345)
        self.webhooks = webhooks if webhooks is not None else _registry()
        self.orch = orch if orch is not None else FakeOrch()
        self.calls: list[tuple[int, dict]] = []

    def _json(self, payload, status=200):
        self.calls.append((status, payload))


class TestHandlerGlue:
    def test_dispatches_and_replies_202(self, state_dir, monkeypatch):
        monkeypatch.setenv("TEST_WH_SECRET", "s3cret")
        body = b'{"x": 1}'
        h = _FakeHTTP("/api/webhooks/deploy?token=ignored", body,
                      headers={SIGNATURE_HEADER: _sign("s3cret", body)})
        AdminHandler._handle_webhook_inbound(h)
        assert h.calls and h.calls[0][0] == 202

    def test_missing_name_400(self):
        h = _FakeHTTP("/api/webhooks")
        AdminHandler._handle_webhook_inbound(h)
        assert h.calls[0][0] == 400

    def test_oversized_body_413(self):
        h = _FakeHTTP("/api/webhooks/deploy", b"x",
                      headers={"Content-Length": str(20 * 1024 * 1024)})
        AdminHandler._handle_webhook_inbound(h)
        assert h.calls[0][0] == 413
