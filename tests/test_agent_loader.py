"""tests/test_agent_loader.py — AgentLoader + AST 安全扫描单元测试。"""
import sys
from pathlib import Path
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


_DEMO_SRC = '''from pipeline_core.base_agent import BaseAgent
AGENT_NAME = "{name}"
AGENT_VERSION = "{version}"
class DemoAgent(BaseAgent):
    def handle(self, msg):
        return {{"status": "ok", "marker": "{marker}"}}
'''


class TestModuleIdentity:
    """同一个 Agent 文件在进程里只能有一份模块对象。

    原来 register() 每次都 module_from_spec + exec_module 并覆写
    sys.modules["agents.<name>"]，于是同一个 Agent 存在两份类对象：
    测试里 `patch("agents.writer.WriterAgent.handle")` 打的是先导入的那一份，
    注册器造的实例用的是另一份——补丁空转，E2E 照样绿。这条判据不是假想的，
    本项目已经因此收回过两次"绿了"的结论。
    """

    def _demo(self, tmp_path, name="demo_identity", version="1.0", marker="A"):
        (tmp_path / f"{name}.py").write_text(
            _DEMO_SRC.format(name=name, version=version, marker=marker), encoding="utf-8")
        return name

    def _import_first(self, tmp_path, name):
        """模拟测试收集期就 `import agents.xxx` 的情形。"""
        import importlib.util

        key = f"agents.{name}"
        spec = importlib.util.spec_from_file_location(key, tmp_path / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_pre_imported_module_is_reused_not_replaced(self, tmp_path, monkeypatch):
        name = self._demo(tmp_path)
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)
        monkeypatch.syspath_prepend(str(tmp_path))
        first = self._import_first(tmp_path, name)
        loader = _make_loader(tmp_path)
        loader.register([name])
        assert sys.modules[f"agents.{name}"] is first, "注册器换了模块对象，补丁就会空转"
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)

    def test_patch_before_register_is_honoured(self, tmp_path, monkeypatch):
        """注册之前打的补丁必须真的作用在被实例化的那个类上。"""
        name = self._demo(tmp_path)
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)
        first = self._import_first(tmp_path, name)

        def patched(self, msg):
            return {"status": "ok", "marker": "patched"}

        monkeypatch.setattr(first.DemoAgent, "handle", patched)
        loader = _make_loader(tmp_path)
        loader.register([name])
        instance = loader.registry.get_instance(name)
        assert instance is not None
        assert instance.handle(object())["marker"] == "patched"
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)

    def test_registering_twice_does_not_stack_module_objects(self, tmp_path, monkeypatch):
        name = self._demo(tmp_path)
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)
        loader = _make_loader(tmp_path)
        loader.register([name])
        first = sys.modules[f"agents.{name}"]
        loader.register([name])
        assert sys.modules[f"agents.{name}"] is first
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)

    def test_same_name_from_another_directory_is_not_reused(self, tmp_path, monkeypatch):
        """复用条件是"同一个文件"，不是"同一个名字"。

        不同 agents_dir 下的同名 Agent（测试夹具与插件目录都常见）要是被当成
        同一份，注册到的就是别的目录的代码——那比补丁空转更糟。
        """
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        for d, marker in ((dir_a, "A"), (dir_b, "B")):
            d.mkdir()
            (d / "twin.py").write_text(
                _DEMO_SRC.format(name="twin", version="1.0", marker=marker), encoding="utf-8")
        monkeypatch.delitem(sys.modules, "agents.twin", raising=False)
        _make_loader(dir_a).register(["twin"])
        loader_b = _make_loader(dir_b)
        loader_b.register(["twin"])
        got = loader_b.registry.get_instance("twin").handle(object())
        assert got["marker"] == "B", f"注册到了另一个目录的同名模块: {got}"
        monkeypatch.delitem(sys.modules, "agents.twin", raising=False)

    def test_reload_true_forces_a_fresh_module(self, tmp_path, monkeypatch):
        """热插拔的口子要真的能用，否则就是个装饰性参数。"""
        name = self._demo(tmp_path)
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)
        loader = _make_loader(tmp_path)
        loader.register([name])
        first = sys.modules[f"agents.{name}"]
        loader.register([name], reload=True)
        assert sys.modules[f"agents.{name}"] is not first
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)

    def test_safety_scan_runs_even_when_the_module_is_reused(self, tmp_path, monkeypatch):
        """AST 安全扫描与是否复用无关。

        缓存里那一份可能是普通 import 带进来的，从没走过这道扫描；
        只在"新加载"时检查等于给已导入的模块开了免检通道。
        """
        name = self._demo(tmp_path)
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)
        loader = _make_loader(tmp_path)
        loader.register([name])
        calls = []
        real = __import__("pipeline_core.agent_loader", fromlist=["_check_safety"])._check_safety

        def spy(file_path, strict=False):
            calls.append((file_path.name, strict))
            return real(file_path, strict)

        monkeypatch.setattr("pipeline_core.agent_loader._check_safety", spy)
        monkeypatch.setattr("pipeline_core.agent_loader.declares_sandbox_trust",
                            lambda p: False)
        loader.register([name])          # 复用路径
        assert calls, "复用模块时跳过了安全扫描"
        monkeypatch.delitem(sys.modules, f"agents.{name}", raising=False)

    def test_real_agents_dir_is_sandbox_trusted_so_no_spurious_scan(self, tmp_path):
        """出厂 Agent 自带 SANDBOX_TRUSTED，走的是"跳过黑名单"而不是"没检查"。"""
        from pipeline_core.agent_loader import declares_sandbox_trust

        agents = Path(__file__).resolve().parent.parent / "agents"
        assert declares_sandbox_trust(agents / "writer.py") is True
        assert declares_sandbox_trust(tmp_path / "nope.py") is False
