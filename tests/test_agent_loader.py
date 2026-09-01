"""tests/test_agent_loader.py — AgentLoader + AST 安全扫描单元测试。"""
from unittest.mock import MagicMock

import pytest

from pipeline_core.agent_loader import (
    AgentLoader,
    SecurityError,
    _check_safety,
)
from pipeline_core.registry import Registry


def _make_loader(tmp_path):
    reg = Registry()
    bus = MagicMock()
    return AgentLoader(reg, bus, str(tmp_path), strict_safety=True)


class TestCheckSafety:
    """AST 安全检查"""

    def test_clean_file_no_dangers(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("def foo(): return 42\n", encoding="utf-8")
        assert _check_safety(f) == []

    def test_detects_os_system(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text('import os\nos.system("rm -rf /")\n', encoding="utf-8")
        dangers = _check_safety(f)
        assert any("os.system" in d for d in dangers)

    def test_detects_eval(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text('eval("1+1")\n', encoding="utf-8")
        dangers = _check_safety(f)
        assert any("eval" in d for d in dangers)

    def test_detects_from_os_import_remove(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text('from os import remove\nremove("/etc/passwd")\n', encoding="utf-8")
        dangers = _check_safety(f)
        assert any("remove" in d for d in dangers)

    def test_detects_from_subprocess_import_popen(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text('from subprocess import Popen\nPopen(["ls"])\n', encoding="utf-8")
        dangers = _check_safety(f)
        assert any("Popen" in d for d in dangers)

    def test_detects_import_ctypes(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text('import ctypes\nctypes.CDLL("libc")\n', encoding="utf-8")
        dangers = _check_safety(f)
        assert any("ctypes" in d for d in dangers)

    def test_strict_mode_raises(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text('import os\nos.system("x")\n', encoding="utf-8")
        with pytest.raises(SecurityError, match="危险调用"):
            _check_safety(f, strict=True)

    def test_syntax_error_returns_empty(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def foo(\n", encoding="utf-8")
        assert _check_safety(f) == []


class TestAgentLoaderDiscover:
    def test_discovers_python_files(self, tmp_path):
        (tmp_path / "foo.py").write_text("# agent", encoding="utf-8")
        (tmp_path / "bar.py").write_text("# agent", encoding="utf-8")
        (tmp_path / "_private.py").write_text("# skip", encoding="utf-8")
        (tmp_path / "readme.txt").write_text("skip", encoding="utf-8")
        loader = _make_loader(tmp_path)
        discovered = loader.discover()
        assert sorted(discovered) == ["bar", "foo"]

    def test_missing_dir_returns_empty(self, tmp_path):
        loader = _make_loader(tmp_path / "nonexistent")
        assert loader.discover() == []


class TestAgentLoaderRegister:
    def test_register_loads_real_agent(self, tmp_path):
        # 写一个最小合法 Agent
        (tmp_path / "demo_agent.py").write_text(
            'from pipeline_core.base_agent import BaseAgent, Message\n'
            'AGENT_NAME = "demo_agent"\n'
            'AGENT_VERSION = "1.0"\n'
            'class DemoAgent(BaseAgent):\n'
            '    def handle(self, msg): return {"status": "ok"}\n',
            encoding="utf-8",
        )
        loader = _make_loader(tmp_path)
        loaded = loader.register(["demo_agent"])
        assert "demo_agent" in loaded
        assert loader.registry.get("demo_agent") is not None

    def test_register_skips_unsafe_agent_in_strict_mode(self, tmp_path):
        (tmp_path / "unsafe_agent.py").write_text(
            'from pipeline_core.base_agent import BaseAgent, Message\n'
            'AGENT_NAME = "unsafe_agent"\n'
            'class UnsafeAgent(BaseAgent):\n'
            '    def handle(self, msg):\n'
            '        import os\n'
            '        os.system("rm -rf /")\n'
            '        return {"status": "ok"}\n',
            encoding="utf-8",
        )
        loader = _make_loader(tmp_path)
        loaded = loader.register(["unsafe_agent"])
        assert "unsafe_agent" not in loaded

    def test_register_extracts_meta(self, tmp_path):
        (tmp_path / "meta_agent.py").write_text(
            'from pipeline_core.base_agent import BaseAgent, Message\n'
            'AGENT_NAME = "meta_agent"\n'
            'AGENT_VERSION = "2.0"\n'
            'AGENT_DESC = "A test agent"\n'
            'AGENT_PRIORITY = 10\n'
            'DEPENDENCIES = ["researcher"]\n'
            'class MetaAgent(BaseAgent):\n'
            '    def handle(self, msg): return {"status": "ok"}\n',
            encoding="utf-8",
        )
        loader = _make_loader(tmp_path)
        loader.register(["meta_agent"])
        meta = loader.registry.get_meta("meta_agent")
        assert meta is not None
        assert meta.name == "meta_agent"
        assert meta.version == "2.0"
        assert meta.priority == 10
        assert meta.dependencies == ["researcher"]
