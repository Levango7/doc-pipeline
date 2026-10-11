"""fast_json 单元测试——orjson 优先、json 回退、bytes 输出。"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from artesian.fast_json import HAS_ORJSON, dumps, dumps_bytes, loads


class TestDumps:
    def test_dict(self):
        assert loads(dumps({"a": 1})) == {"a": 1}

    def test_list(self):
        assert loads(dumps([1, 2, 3])) == [1, 2, 3]

    def test_nested(self):
        obj = {"x": [1, {"y": True}]}
        assert loads(dumps(obj)) == obj

    def test_default_callback(self):
        class MyObj:
            def __init__(self, v: int) -> None:
                self.v = v

        obj = MyObj(42)
        result = dumps(obj, default=lambda o: {"v": o.v} if hasattr(o, "v") else str(o))
        assert loads(result) == {"v": 42}

    def test_returns_str(self):
        assert isinstance(dumps({}), str)

    @pytest.mark.skipif(not HAS_ORJSON, reason="orjson not installed")
    def test_orjson_path_used_when_available(self):
        """orjson 可用时 dumps 走 orjson 路径（不抛异常即通过）。"""
        result = dumps({"key": "value"})
        assert '"key"' in result or "'key'" in result


class TestLoads:
    def test_simple_object(self):
        assert loads('{"a": 1}') == {"a": 1}

    def test_array(self):
        assert loads("[1, 2, 3]") == [1, 2, 3]

    def test_string_value(self):
        assert loads('"hello"') == "hello"

    def test_null(self):
        assert loads("null") is None

    def test_numeric(self):
        assert loads("3.14") == 3.14
        assert loads("-7") == -7

    def test_bytes_input(self):
        assert loads(b'{"a": 1}') == {"a": 1}

    def test_str_input(self):
        assert loads('{"a": 1}') == {"a": 1}


class TestDumpsBytes:
    def test_returns_bytes(self):
        result = dumps_bytes({"x": 1})
        assert isinstance(result, bytes)

    def test_roundtrip(self):
        obj = {"nested": [1, 2, 3], "flag": True}
        raw = dumps_bytes(obj)
        assert json.loads(raw) == obj

    def test_utf_8_content(self):
        result = dumps_bytes({"msg": "你好"})
        assert b"\xe4\xbd\xa0\xe5\xa5\xbd" in result or "你好" in result.decode("utf-8")


class TestFallback:
    @pytest.mark.skipif(HAS_ORJSON, reason="this test requires orjson to be absent")
    def test_fallback_to_stdlib_json(self):
        result = dumps({"a": 1})
        assert result == json.dumps({"a": 1})

    @pytest.mark.skipif(not HAS_ORJSON, reason="orjson 未安装，无 fallback 模拟目标")
    def test_fallback_simulated(self):
        """模拟 orjson 抛出异常时回退到 stdlib json。"""
        with patch("artesian.fast_json.orjson.dumps", side_effect=Exception("boom")):
            result = dumps({"key": "value"})
            assert json.loads(result) == {"key": "value"}

    @pytest.mark.skipif(not HAS_ORJSON, reason="orjson 未安装，无 fallback 模拟目标")
    def test_loads_fallback_simulated(self):
        with patch("artesian.fast_json.orjson.loads", side_effect=Exception("boom")):
            result = loads('{"k": "v"}')
            assert result == {"k": "v"}

    @pytest.mark.skipif(not HAS_ORJSON, reason="orjson 未安装，无 fallback 模拟目标")
    def test_dumps_bytes_fallback_simulated(self):
        with patch("artesian.fast_json.orjson.dumps", side_effect=Exception("boom")):
            result = dumps_bytes({"中": 1})
        assert isinstance(result, bytes)
        assert json.loads(result) == {"中": 1}


class TestStdlibOnlyEnvironment:
    """orjson 缺席时模块必须整体退到标准库路径。

    本机/CI 都装了 orjson，模块级 `except ImportError` 那段静态不可达——
    用 sys.modules 钉桩（值为 None 时 import 直接报 ImportError）强制重载，
    让「无 orjson 环境」真跑一遍，而不是只在覆盖率报告里声明它存在。
    """

    def test_reload_without_orjson_uses_stdlib_paths(self, monkeypatch):
        import importlib
        import sys

        import artesian.fast_json as mod

        monkeypatch.setitem(sys.modules, "orjson", None)
        try:
            stdlib_mod = importlib.reload(mod)
            assert stdlib_mod.HAS_ORJSON is False
            assert "中" in stdlib_mod.dumps({"中": 1})  # ensure_ascii=False 语义
            assert stdlib_mod.loads(stdlib_mod.dumps({"中": 1})) == {"中": 1}
            assert json.loads(stdlib_mod.dumps_bytes({"a": 1})) == {"a": 1}
        finally:
            monkeypatch.undo()
            importlib.reload(mod)
