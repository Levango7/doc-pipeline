"""ConfigCenter 单元测试——级联配置、环境变量覆盖、JSON 容器支持。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from pipeline_core.config import ConfigCenter


@pytest.fixture()
def tmp_json_config(tmp_path: Path) -> Path:
    """创建一个含嵌套结构的 JSON 配置文件。"""
    data = {
        "llm": {"model": "gpt-4", "api_key_env": "LLM_KEY", "temperature": 0.7},
        "researcher": {"search_engines": ["ddg"], "max_workers": 3},
        "execution": {"fail_fast": True, "max_retries": 2},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestDefaults:
    def test_defaults_loaded(self):
        c = ConfigCenter()
        assert c.get("agents.dir") == "agents"
        assert c.get("checkpoint.dir") == "checkpoints"
        assert c.get("execution.fail_fast") is True
        assert c.get("llm.model") == "@cf/moonshotai/kimi-k2.6"

    def test_get_missing_key_returns_default(self):
        c = ConfigCenter()
        assert c.get("nonexistent.key", "fallback") == "fallback"
        assert c.get("nonexistent.key") is None


class TestFileLoading:
    def test_json_config_loaded(self, tmp_json_config: Path):
        c = ConfigCenter(config_file=str(tmp_json_config))
        assert c.get("llm.model") == "gpt-4"
        assert c.get("llm.temperature") == 0.7
        assert c.get("researcher.search_engines") == ["ddg"]
        assert c.get("execution.fail_fast") is True

    def test_yaml_config_loaded(self, tmp_path: Path):
        if importlib.util.find_spec("yaml") is None:
            pytest.skip("pyyaml not installed")
        import yaml  # noqa: PLC0415, F401
        p = tmp_path / "config.yaml"
        p.write_text('llm:\n  model: claude-3\n  temperature: 0.3\n', encoding="utf-8")
        c = ConfigCenter(config_file=str(p))
        assert c.get("llm.model") == "claude-3"
        assert c.get("llm.temperature") == 0.3

    def test_missing_config_file_fallback_to_defaults(self, tmp_path: Path):
        nonexist = tmp_path / "no_such_file.json"
        c = ConfigCenter(config_file=str(nonexist))
        assert c.get("llm.model") == "@cf/moonshotai/kimi-k2.6"

    def test_deep_merge_preserves_unmatched_keys(self, tmp_json_config: Path):
        """配置文件只覆盖部分 key，其余 defaults 仍保留。"""
        c = ConfigCenter(config_file=str(tmp_json_config))
        # 配置文件中没有 quality_gate，应保留 default
        assert c.get("quality_gate.min_score") == 70


class TestEnvOverride:
    def setup_method(self) -> None:
        self._cleaned: list[str] = []

    def teardown_method(self) -> None:
        for k in self._cleaned:
            os.environ.pop(k, None)

    def _set(self, **env: str) -> list[str]:
        keys: list[str] = []
        for k, v in env.items():
            os.environ[k] = v
            keys.append(k)
        self._cleaned.extend(keys)
        return keys

    def test_simple_scalar_override(self):
        self._set(DOCPIPE_LLM__MODEL="claude-3-opus")
        c = ConfigCenter()
        assert c.get("llm.model") == "claude-3-opus"

    def test_boolean_override(self):
        self._set(DOCPIPE_EXECUTION__FAIL_FAST="false")
        c = ConfigCenter()
        assert c.get("execution.fail_fast") is False

    def test_integer_override(self):
        self._set(DOCPIPE_RESEARCHER__MAX_WORKERS="8")
        c = ConfigCenter()
        assert c.get("researcher.max_workers") == 8

    def test_json_array_override(self):
        """DOCPIPE_RESEARCHER__SEARCH_ENGINES='["mock","bing"]' → 覆盖为 list。"""
        self._set(DOCPIPE_RESEARCHER__SEARCH_ENGINES='["mock", "bing"]')
        c = ConfigCenter()
        assert c.get("researcher.search_engines") == ["mock", "bing"]

    def test_json_object_override(self):
        """DOCPIPE_LLM__EXTRA='{"timeout":30}' → 覆盖为 dict。"""
        self._set(DOCPIPE_LLM__EXTRA='{"timeout": 30}')
        c = ConfigCenter()
        assert c.get("llm.extra") == {"timeout": 30}

    def test_invalid_json_falls_back_to_string(self):
        self._set(DOCPIPE_LLM__MODEL="[bad json")
        c = ConfigCenter()
        assert c.get("llm.model") == "[bad json"

    def test_empty_list_override(self):
        self._set(DOCPIPE_RESEARCHER__SEARCH_ENGINES="[]")
        c = ConfigCenter()
        assert c.get("researcher.search_engines") == []

    def test_empty_dict_override(self):
        self._set(DOCPIPE_LLM__EXTRA="{}")
        c = ConfigCenter()
        assert c.get("llm.extra") == {}


class TestCoerce:
    def test_true_values(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce("true") is True
        assert c._coerce("True") is True
        assert c._coerce("yes") is True
        assert c._coerce("1") is True

    def test_false_values(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce("false") is False
        assert c._coerce("no") is False
        assert c._coerce("0") is False

    def test_int_and_float(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce("42") == 42
        assert c._coerce("-7") == -7
        assert c._coerce("3.14") == 3.14

    def test_string_passthrough(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce("hello") == "hello"
        assert c._coerce("") == ""

    def test_json_array(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce('["a", "b"]') == ["a", "b"]
        assert c._coerce("[]") == []
        assert c._coerce('[1, 2, 3]') == [1, 2, 3]

    def test_json_object(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce('{"x": 1}') == {"x": 1}
        assert c._coerce("{}") == {}

    def test_invalid_json_stays_string(self):
        c = ConfigCenter.__new__(ConfigCenter)
        assert c._coerce("[invalid") == "[invalid"
        assert c._coerce("{broken") == "{broken"


class TestSetAndGet:
    def test_set_runtime_override(self):
        c = ConfigCenter()
        c.set("llm.model", "custom-model")
        assert c.get("llm.model") == "custom-model"

    def test_set_creates_intermediate_dicts(self):
        c = ConfigCenter()
        c.set("new.nested.value", 42)
        assert c.get("new.nested.value") == 42

    def test_to_dict_returns_copy(self):
        c = ConfigCenter()
        d1 = c.to_dict()
        d1["llm"]["model"] = "hacked"
        assert c.get("llm.model") == "@cf/moonshotai/kimi-k2.6"


class TestReload:
    def test_reload_reflects_file_changes(self, tmp_json_config: Path):
        c = ConfigCenter(config_file=str(tmp_json_config))
        assert c.get("llm.model") == "gpt-4"
        # 修改文件
        tmp_json_config.write_text(
            json.dumps({"llm": {"model": "gpt-5"}}, indent=2),
            encoding="utf-8",
        )
        c.reload()
        assert c.get("llm.model") == "gpt-5"
