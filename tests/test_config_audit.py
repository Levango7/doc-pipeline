"""配置变更审计（docs/product-spec.md §5.4：每次 /api/config 变更有审计记录）。

被测契约：
- 变更成功 → `<状态目录>/audit/config.jsonl` 追加一行（key / 新旧值 / 客户端 / 凭证指纹）；
- 敏感键名（含 token/secret/password/api_key）的值**只记脱敏形状**，不落明文；
- 审计写盘失败不静默：响应带 `audit_error`；
- `GET /api/config/audit` 读回最近记录（新→旧），坏行跳过不炸整份读取。

另有一条跨模块一致性判据：webhooks 与 config 两个通道共用
`pipeline_core.audit` 的同一份实现（不做第二份写盘代码）。
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline_core import audit as audit_mod
from pipeline_core.admin_api import AdminHandler


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("DOC_PIPELINE_STATE_DIR", str(d))
    return d


@pytest.fixture
def handler():
    h = AdminHandler.__new__(AdminHandler)
    h.orch = MagicMock()
    h.orch.config.get.return_value = "old-model"
    h.client_address = ("10.0.0.7", 5555)
    h.path = "/api/config"
    h._json = MagicMock()
    h.headers = {"Authorization": "Bearer secret-token-abcdef"}
    return h


def _audit_lines(state_dir: Path) -> list[dict]:
    p = state_dir / "audit" / "config.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


class TestConfigSetAudited:
    def test_change_writes_audit_record(self, state_dir, handler):
        handler._handle_config_set(b'{"key": "llm.model", "value": "new-model"}')
        recs = _audit_lines(state_dir)
        assert len(recs) == 1, "配置变更没有落审计"
        rec = recs[0]
        assert rec["event"] == "config.set"
        assert rec["key"] == "llm.model"
        assert rec["old_value"] == "old-model"
        assert rec["new_value"] == "new-model"
        assert rec["client"] == "10.0.0.7"
        assert rec["ts"]
        payload = handler._json.call_args[0][0]
        assert payload["audit"] == "ok" and payload["applied"] is True
        assert payload["new_value"] == "new-model"

    def test_sensitive_value_redacted_in_audit(self, state_dir, handler):
        handler._handle_config_set(
            b'{"key": "llm.api_key", "value": "sk-super-secret-value"}')
        rec = _audit_lines(state_dir)[-1]
        dumped = json.dumps(rec, ensure_ascii=False)
        assert "sk-super-secret-value" not in dumped, "密钥明文进了审计文件"
        assert rec["new_value"]["redacted"] is True
        assert rec["new_value"]["length"] == len("sk-super-secret-value")
        handler.orch.config.set.assert_called_once_with(
            "llm.api_key", "sk-super-secret-value")

    def test_non_sensitive_key_keeps_value(self, state_dir, handler):
        handler._handle_config_set(b'{"key": "writer.max_words", "value": 800}')
        rec = _audit_lines(state_dir)[-1]
        assert rec["new_value"] == 800, "非敏感键不该被脱敏"

    def test_audit_write_failure_reported(self, tmp_path, monkeypatch, handler):
        blocker = tmp_path / "blocked"
        blocker.write_text("file", encoding="utf-8")
        monkeypatch.setattr(audit_mod, "audit_path",
                            lambda channel: blocker / "audit" / f"{channel}.jsonl")
        handler._handle_config_set(b'{"key": "llm.model", "value": "x"}')
        payload = handler._json.call_args[0][0]
        assert "audit_error" in payload and "audit" not in payload
        assert payload["applied"] is True, "审计失败不该回滚配置变更本身"

    def test_bad_requests_not_audited(self, state_dir, handler):
        handler._handle_config_set(b'{"value": "no-key"}')
        handler._handle_config_set(b'not-json')
        assert _audit_lines(state_dir) == [], "被拒的请求不该留下变更记录"


class TestConfigAuditEndpoint:
    def test_reads_back_newest_first(self, state_dir):
        for value in ("m1", "m2", "m3"):
            audit_mod.write_audit("config", {"event": "config.set",
                                             "key": "llm.model", "new_value": value})
        h = AdminHandler.__new__(AdminHandler)
        h.path = "/api/config/audit?limit=2"
        h._json = MagicMock()
        h._handle_config_audit()
        payload = h._json.call_args[0][0]
        assert payload["count"] == 2
        assert payload["records"][0]["new_value"] == "m3"   # 新→旧
        assert payload["records"][1]["new_value"] == "m2"

    def test_bad_limit_falls_back_to_default(self, state_dir):
        h = AdminHandler.__new__(AdminHandler)
        h.path = "/api/config/audit?limit=abc"
        h._json = MagicMock()
        h._handle_config_audit()
        assert h._json.call_args[0][0]["count"] == 0

    def test_corrupt_line_skipped(self, state_dir):
        p = state_dir / "audit" / "config.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"event": "config.set", "key": "a"}\n{半行\n'
                     '{"event": "config.set", "key": "b"}\n', encoding="utf-8")
        records = audit_mod.read_audit("config")
        assert [r["key"] for r in records] == ["a", "b"], "坏行应跳过而非炸整份读取"


class TestAuditChannelShared:
    """webhooks 与 config 共用同一实现——不做第二份写盘代码会漂移。"""

    def test_webhooks_delegates_to_shared_module(self, state_dir):
        from pipeline_core import webhooks as wh

        assert wh._audit_path() == audit_mod.audit_path("webhooks")
        assert wh.write_audit({"event": "x"}) is None
        data = (state_dir / "audit" / "webhooks.jsonl").read_text(encoding="utf-8")
        assert json.loads(data.splitlines()[0])["event"] == "x"
