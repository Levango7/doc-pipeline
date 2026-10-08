"""外部插件示例的端到端判据：真 venv + 真 pip install + 本仓零改动即被发现/执行。

这是 product-spec §2.1 验收线第 2 条（"由外部 pip 包提供 Agent，本仓零改动即被
发现、注册、执行"）的可复现证明——不是打桩 entry_points()，而是让 pip 把元数据
真的写进 site-packages、由 importlib.metadata 读出来。

venv 用 --system-site-packages 建（pip/setuptools 借宿主机）、插件本体 --no-deps
安装——整条判据离线可跑（CI 与无网环境行为一致）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "examples" / "plugin-hello"


class TestExamplePackageShape:
    """快判据：示例包的元数据与契约（venv 那条是慢判据，这里先把形状钉住）。"""

    def test_entry_point_group_matches_loader_constant(self):
        import tomllib

        from pipeline_core.agent_loader import ENTRY_POINT_GROUP

        data = tomllib.loads((PLUGIN_DIR / "pyproject.toml").read_text(encoding="utf-8"))
        groups = data["project"]["entry-points"]
        assert ENTRY_POINT_GROUP in groups, (
            f"示例包的 group 与加载器常量不一致：{list(groups)} vs {ENTRY_POINT_GROUP}")
        values = groups[ENTRY_POINT_GROUP]
        assert set(values) == {"char_stats"}
        assert "." in values["char_stats"], "entry point 的值须是模块路径"

    def test_module_contract(self):
        """与内置件同构：模块级 AGENT_NAME + 一个 BaseAgent 子类。"""
        import importlib.util

        from pipeline_core.base_agent import BaseAgent

        mod_path = PLUGIN_DIR / "doc_pipeline_plugin_hello" / "char_stats.py"
        spec = importlib.util.spec_from_file_location("_plugin_example_probe", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert isinstance(mod.AGENT_NAME, str) and mod.AGENT_NAME
        classes = [v for v in vars(mod).values()
                   if isinstance(v, type) and issubclass(v, BaseAgent)
                   and v is not BaseAgent]
        assert classes, "示例模块里没有 BaseAgent 子类"

    def test_passes_ast_safety_scan(self):
        from pipeline_core.agent_loader import _check_safety

        assert _check_safety(PLUGIN_DIR / "doc_pipeline_plugin_hello" / "char_stats.py") == []


class TestRealPipInstallDiscovery:
    """慢判据：真 venv 里 pip install → 被发现/注册/执行（验收线第 2 条的实证）。

    安装走两段式：**优先 `--no-build-isolation`**（借用宿主机 setuptools，离线可跑）；
    宿主机没有可借的 setuptools 时（Python 3.12+ 的 ensurepip 不再自带，CI 镜像
    常见）**回退到隔离构建**——由 pip 临时安装构建依赖，需要一次网络访问。
    两条路径都会把包真装进 site-packages，判据本身不打折。
    """

    @staticmethod
    def _venv_python(venv_dir: Path) -> Path:
        return venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    def test_installed_plugin_is_discovered_registered_and_executable(self, tmp_path):
        if os.environ.get("DOC_PIPELINE_SKIP_PLUGIN_E2E"):
            import pytest

            pytest.skip("显式跳过（DOC_PIPELINE_SKIP_PLUGIN_E2E）")
        venv_dir = tmp_path / "venv"
        r = subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv_dir)],
            capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-800:]
        vpy = self._venv_python(venv_dir)
        base = [str(vpy), "-m", "pip", "install", "--no-deps",
                "--disable-pip-version-check"]
        r = subprocess.run(base + ["--no-build-isolation", str(PLUGIN_DIR)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 and "setuptools.build_meta" in (r.stderr or ""):
            # 宿主机没 setuptools 可借（3.12+ 常见）→ 回退隔离构建（要一次网络）
            r = subprocess.run(base + [str(PLUGIN_DIR)],
                               capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, f"pip install 失败：\n{r.stdout[-800:]}\n{r.stderr[-800:]}"

        # 空 agents 目录 + 仓库根为 cwd（'' 进 sys.path，pipeline_core 从本仓导入）——
        # 发现的来源只可能是 pip 写进 site-packages 的那条 entry point 元数据。
        empty_agents = tmp_path / "empty_agents"
        empty_agents.mkdir()
        probe = (
            "import json\n"
            "from unittest.mock import MagicMock\n"
            "from pipeline_core.agent_loader import AgentLoader\n"
            "from pipeline_core.registry import Registry\n"
            "from pipeline_core.base_agent import Message\n"
            f"loader = AgentLoader(Registry(), MagicMock(), r'{empty_agents}', strict_safety=True)\n"
            "names = loader.discover()\n"
            "loaded = loader.register(['char_stats'])\n"
            "source = None\n"
            "if 'char_stats' in loaded:\n"
            "    source = loader.registry.get_meta('char_stats').source\n"
            "inst = loader.registry.get_instance('char_stats')\n"
            "out = inst.handle(Message(topic='t', from_agent='probe',\n"
            "                          payload={'content': 'abc汉字'}))\n"
            "print('PROBE ' + json.dumps({'names': names, 'loaded': loaded,\n"
            "                             'source': source, 'out': out}, ensure_ascii=False))\n"
        )
        r = subprocess.run([str(vpy), "-c", probe], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-1200:]
        line = next(ln for ln in r.stdout.splitlines() if ln.startswith("PROBE "))
        data = json.loads(line[len("PROBE "):])
        assert "char_stats" in data["names"]                 # 被发现
        assert data["loaded"] == ["char_stats"]              # 被注册
        assert data["source"] == "entry_point:char_stats"    # 来源标记正确
        assert data["out"]["chars"] == 5                     # 被真的执行（'abc汉字'）
        assert data["out"]["cjk_chars"] == 2
