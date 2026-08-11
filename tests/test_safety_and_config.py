"""Agent 沙箱安全检查 + 配置热更新测试"""
from unittest.mock import MagicMock

import pytest

from pipeline_core.agent_loader import AgentLoader, SecurityError, _check_safety


class TestSafetyCheck:
    def test_safe_code_no_dangers(self, tmp_path):
        f = tmp_path / "safe_agent.py"
        f.write_text("import json\nx = json.loads('{}')\n")
        assert _check_safety(f, strict=True) == []

    def test_os_system_detected(self, tmp_path):
        f = tmp_path / "evil.py"
        f.write_text("import os\nos.system('rm -rf /')\n")
        dangers = _check_safety(f, strict=False)
        assert len(dangers) == 1
        assert "os.system" in dangers[0]

    def test_subprocess_detected(self, tmp_path):
        f = tmp_path / "evil2.py"
        f.write_text("import subprocess\nsubprocess.Popen(['cat', '/etc/passwd'])\n")
        dangers = _check_safety(f, strict=False)
        assert len(dangers) == 1
        assert "subprocess.Popen" in dangers[0]

    def test_eval_detected(self, tmp_path):
        f = tmp_path / "evil3.py"
        f.write_text("x = eval('__import__(\"os\")')\n")
        dangers = _check_safety(f, strict=False)
        assert len(dangers) >= 1
        assert any("eval" in d for d in dangers)

    def test_strict_mode_raises(self, tmp_path):
        f = tmp_path / "evil4.py"
        f.write_text("import os\nos.system('whoami')\n")
        with pytest.raises(SecurityError):
            _check_safety(f, strict=True)

    def test_non_strict_mode_warns(self, tmp_path):
        f = tmp_path / "evil5.py"
        f.write_text("import os\nos.system('whoami')\n")
        dangers = _check_safety(f, strict=False)
        assert len(dangers) == 1

    def test_syntax_error_returns_empty(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def broken(\n")
        assert _check_safety(f, strict=True) == []

    def test_multiple_dangers(self, tmp_path):
        f = tmp_path / "multi_evil.py"
        f.write_text(
            "import os\n"
            "import subprocess\n"
            "os.system('a')\n"
            "subprocess.run(['b'])\n"
            "exec('c')\n"
        )
        dangers = _check_safety(f, strict=False)
        assert len(dangers) == 3


class TestAgentLoaderTrusted:
    def test_trusted_agents_set(self):
        assert "researcher" in AgentLoader._TRUSTED_AGENTS
        assert "writer" in AgentLoader._TRUSTED_AGENTS
        assert "fetcher" in AgentLoader._TRUSTED_AGENTS

    def test_strict_safety_default_true(self):
        loader = AgentLoader.__new__(AgentLoader)
        assert loader.__class__.__init__.__defaults__ is not None


class TestResearcherConfigUpdate:
    def test_on_config_update_resets_search_manager(self):
        """researcher.on_config_update 应重置 _search_manager"""
        from agents.researcher import ResearcherAgent
        from pipeline_core.base_agent import AgentMeta

        AgentMeta(name="researcher", version="2.0", description="test", priority=10)
        agent = ResearcherAgent.__new__(ResearcherAgent)
        agent._search_manager = MagicMock()
        agent._search_engines = ["bocha"]
        agent._logger = MagicMock()

        agent.on_config_update(changed_keys=["search_engines"])

        assert agent._search_manager is None
        assert agent._search_engines == ["bocha"]
