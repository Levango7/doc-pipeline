"""pipeline.started 事件载荷敏感信息脱敏测试"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.pipeline import PipelineOrchestrator, _redact_config


class TestRedactConfig:
    def test_nested_api_key_redacted(self):
        cfg = {
            "llm": {"api_key": "sk-live-123", "model": "test-model"},
            "db_password": "p@ss",
            "webhook_token": "tok-1",
            "timeout": 30,
            "credentials": {"secret": "s3cret"},
            "items": [{"api-key": "k-1", "note": "ok"}],
        }
        out = _redact_config(cfg)
        assert out["llm"]["api_key"] == "***redacted***"
        assert out["llm"]["model"] == "test-model"
        assert out["db_password"] == "***redacted***"
        assert out["webhook_token"] == "***redacted***"
        assert out["timeout"] == 30
        assert out["credentials"]["secret"] == "***redacted***"
        assert out["items"][0]["api-key"] == "***redacted***"
        assert out["items"][0]["note"] == "ok"

    def test_original_config_untouched(self):
        cfg = {"llm": {"api_key": "sk-live-123"}}
        _redact_config(cfg)
        assert cfg["llm"]["api_key"] == "sk-live-123"

    def test_non_dict_passthrough(self):
        assert _redact_config(None) is None
        assert _redact_config("x") == "x"
        assert _redact_config([1, 2]) == [1, 2]


class TestPipelineStartedRedaction:
    def test_started_event_payload_redacted(self, tmp_path):
        o = PipelineOrchestrator(checkpoint_dir=str(tmp_path / "ckpt"))
        captured = []

        def spy(topic, from_a, payload):
            captured.append((topic, payload))
            return {"status": "sent"}

        o.bus.publish = spy
        cfg = {"llm": {"api_key": "sk-secret-value", "model": "demo"}, "retries": 3}
        o.run(task_id="redact-test", pipeline_name="__no_such_pipeline__",
              input_file="", config=cfg, wait=True)
        started = [p for t, p in captured if t == "pipeline.started"]
        assert started, "pipeline.started 未发布"
        payload_cfg = started[0]["config"]
        assert payload_cfg["llm"]["api_key"] == "***redacted***"
        assert payload_cfg["llm"]["model"] == "demo"
        assert payload_cfg["retries"] == 3
        assert cfg["llm"]["api_key"] == "sk-secret-value"
