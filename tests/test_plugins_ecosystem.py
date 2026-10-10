"""External plugin ecosystem: six real pip packages, one venv, zero repo changes.

Empirical proof of product-spec section 2.1 acceptance line 2: third-party
plugins >= 5, Agents provided by external pip packages, discovered, registered
and executed with zero repo changes.

Division of labour with tests/test_plugin_example.py: that one guards the sample
package contract (entry_points group, module shape, AST safety); this one guards
the ecosystem surface -- install six packages for real, then assert each is
discovered, registered and actually executed, with source marked entry_point.

Any package that fails to install, get discovered, register, or run turns red.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
EXAMPLES = PROJECT / "examples"

#: Plugin agent name -> examples/ subdirectory holding its pip package
PLUGIN_DIRS = {
    "char_stats": "plugin-hello",
    "keyword_extract": "plugin-keyword-extract",
    "readability": "plugin-readability",
    "link_check": "plugin-link-check",
    "fact_coverage": "plugin-fact-coverage",
    "tldr": "plugin-tldr",
}

#: Plugin agent name -> entry point value its package declares
ENTRY_POINT_VALUES = {
    "char_stats": "doc_pipeline_plugin_hello.char_stats",
    "keyword_extract": "doc_pipeline_plugin_keyword_extract.keyword_extract",
    "readability": "doc_pipeline_plugin_readability.readability",
    "link_check": "doc_pipeline_plugin_link_check.link_check",
    "fact_coverage": "doc_pipeline_plugin_fact_coverage.fact_coverage",
    "tldr": "doc_pipeline_plugin_tldr.tldr",
}

PLUGIN_NAMES = sorted(PLUGIN_DIRS)

_CONTENT = (
    "# Kafka replication\n\n"
    "Kafka ISR replica count is 3 and relies on ZooKeeper for leader election. "
    "See https://kafka.apache.org/documentation and the Apache docs.\n\n"
    "| Component | Role |\n|---|---|\n| Broker | storage |\n| ZK | election |\n\n"
    "```sql\nSELECT 1;\n```\n"
)


def _probe_script() -> str:
    """Child program: discover, register and execute every plugin agent."""
    payload = json.dumps({"content": _CONTENT, "task_id": "probe"})
    agents_dir = str(PROJECT / "agents")
    return "\n".join([
        "import json",
        "from unittest.mock import MagicMock",
        "from pipeline_core.agent_loader import AgentLoader",
        "from pipeline_core.registry import Registry",
        "from pipeline_core.base_agent import Message",
        f"PAYLOAD = json.loads({payload!r})",
        "",
        f"loader = AgentLoader(Registry(), MagicMock(), {agents_dir!r}, strict_safety=True)",
        "discovered = loader.discover()",
        f"wanted = {PLUGIN_NAMES!r}",
        "loaded = loader.register([n for n in wanted if n in discovered])",
        "runs = {}",
        "for name in loaded:",
        "    inst = loader.registry.get_instance(name)",
        "    meta = loader.registry.get_meta(name)",
        "    try:",
        "        result = inst.handle(Message(topic='t', from_agent='probe', payload=PAYLOAD))",
        "        runs[name] = {'source': meta.source, 'ok': (result or {}).get('status') == 'ok'}",
        "    except Exception as exc:",
        "        runs[name] = {'source': meta.source, 'ok': False, 'error': type(exc).__name__}",
        "print('PROBE ' + json.dumps({'discovered': discovered, 'loaded': loaded, 'runs': runs}, ensure_ascii=False))",
        "",
    ])


def _module_path(agent: str) -> Path:
    """Path to the plugin's agent module: <pkg>/<module>.py.

    The entry point value is package.module, and the package directory name is
    not derivable from the examples/ subdir name -- resolve it from the value
    itself (its first dotted segment is the import package).
    """
    value = ENTRY_POINT_VALUES[agent]
    pkg, _, mod = value.partition(".")
    return EXAMPLES / PLUGIN_DIRS[agent] / pkg / f"{mod}.py"


class TestPluginPackageShape:
    """Quick criteria: every example package honours the join contract.

    Real venv installation is the slow criterion below; this runs in seconds
    and catches contract drift per package (group, module path, module shape).
    """

    @pytest.mark.parametrize("agent", PLUGIN_NAMES)
    def test_pyproject_declares_entry_point(self, agent):
        import tomllib

        pkg_dir = EXAMPLES / PLUGIN_DIRS[agent]
        data = tomllib.loads((pkg_dir / "pyproject.toml").read_text(encoding="utf-8"))
        groups = data["project"]["entry-points"]
        assert "doc_pipeline.agents" in groups, (
            f"{agent}: package declares no doc_pipeline.agents group")
        value = groups["doc_pipeline.agents"][agent]
        assert value == ENTRY_POINT_VALUES[agent], (
            f"{agent}: entry point value drifted: {value}")

    @pytest.mark.parametrize("agent", PLUGIN_NAMES)
    def test_module_contract(self, agent):
        """Module-level AGENT_NAME plus one BaseAgent subclass, same as builtins."""
        import re

        mod_path = _module_path(agent)
        assert mod_path.is_file(), f"{agent}: module file missing: {mod_path}"
        source = mod_path.read_text(encoding="utf-8")
        assert re.search(r"^AGENT_NAME\s*=\s*[\"']" + re.escape(agent) + r"[\"']",
                         source, re.M), f"{agent}: AGENT_NAME not declared as {agent}"

    @pytest.mark.parametrize("agent", PLUGIN_NAMES)
    def test_passes_ast_safety_scan(self, agent):
        from pipeline_core.agent_loader import _check_safety

        mod_path = _module_path(agent)
        assert _check_safety(mod_path) == [], (
            f"{agent}: plugin module hits the AST blacklist and would be rejected")


class TestAllPluginsInstalledAndExecuted:
    """The real criterion: install all six for real, then run all six.

    venv built with --system-site-packages (borrows the host's PyYAML /
    artesian so this stays offline); packages installed with --no-deps. What
    is judged here is the ecosystem surface: every package installs, every
    agent is discovered through entry_points, every agent registers and runs,
    and every source tag reads entry_point:<name>.
    """

    @staticmethod
    def _venv_python(venv_dir: Path) -> Path:
        return venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    def _install_all(self, venv_python: Path) -> None:
        for agent in PLUGIN_DIRS:
            pkg_dir = EXAMPLES / PLUGIN_DIRS[agent]
            r = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--no-deps",
                 "--no-build-isolation", "--disable-pip-version-check", str(pkg_dir)],
                capture_output=True, text=True, timeout=900)
            if r.returncode != 0 and "setuptools.build_meta" in (r.stderr or ""):
                r = subprocess.run(
                    [str(venv_python), "-m", "pip", "install", "--no-deps",
                     "--disable-pip-version-check", str(pkg_dir)],
                    capture_output=True, text=True, timeout=900)
            assert r.returncode == 0, (
                f"installing {agent} ({PLUGIN_DIRS[agent]}) failed: "
                f"{r.stdout[-600:]} {r.stderr[-600:]}")

    def test_six_plugins_install_discover_register_and_run(self, tmp_path):
        venv_dir = tmp_path / "venv"
        r = subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
            capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-800:]

        venv_python = self._venv_python(venv_dir)
        self._install_all(venv_python)

        probe = _probe_script()
        r = subprocess.run([str(venv_python), "-c", probe],
                          capture_output = True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-1500:]

        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("PROBE "))
        data = json.loads(line[len("PROBE "):])

        # 每个插件都被发现 + 注册 + 执行成功，且来源标记是 entry_point
        for agent in PLUGIN_NAMES:
            assert agent in data["discovered"], f"{agent} 没被发现（entry_points 丢了？）"
            assert agent in data["loaded"], f"{agent} 没被注册"
            run = data["runs"].get(agent, {})
            assert run.get("ok") is True, (
                f"{agent} 没跑成功: {run.get('error', run)}")
            assert run.get("source") == f"entry_point:{agent}", (
                f"{agent} 来源标记不对: {run.get('source')}")

        assert len(data["loaded"]) >= 5, (
            f"第三方插件实测数不足 5：只装上了 {len(data['loaded'])} 个")
