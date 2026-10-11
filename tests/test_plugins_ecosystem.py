"""Agent 生态判据 —— 整合后的形态（2026-10-11 单发行物决策）。

2026-10-10 曾把五支实用插件做成独立 pip 包（examples/plugin-*），
用户拍板"整合成一个项目"（发行名 nexus-loom，见 spec §7.1）后，
五支并回 agents/ 成为内置件，examples/ 只留 plugin-hello 作
**外部作者模板**（第三方插件机制的参照实现与 E2E 证明）。

本文件现在钉三件事：
1. 五支内置 agent 从 agents/ glob 被发现、注册、真执行（与其它内置件同路径）；
2. plugin-hello 模板仍是合法的外部包形状（entry_points group / 模块契约）；
3. 真实第三方插件实测数为 0——诚实记录，等真实生态。
"""
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
EXAMPLES = PROJECT / "examples"

#: 2026-10-11 从 examples/plugin-* 并入 agents/ 的五支内置件
FOLDED_IN = ["fact_coverage", "keyword_extract", "link_check", "readability", "tldr"]


class TestFoldedInAgents:
    """内置化后的五支：与其它内置件同路径发现/注册/执行。"""

    def test_discovered_via_builtin_glob(self):
        from pipeline_core.agent_loader import AgentLoader
        from pipeline_core.registry import Registry

        loader = AgentLoader(Registry(), None, str(PROJECT / "agents"))
        names = loader.discover()
        for agent in FOLDED_IN:
            assert agent in names, f"{agent} 没有被 agents/ glob 发现"

    def test_registered_and_executable(self):
        from unittest.mock import MagicMock

        from pipeline_core.agent_loader import AgentLoader
        from pipeline_core.base_agent import Message
        from pipeline_core.registry import Registry

        loader = AgentLoader(Registry(), MagicMock(), str(PROJECT / "agents"))
        loaded = loader.register(FOLDED_IN)
        assert sorted(loaded) == sorted(FOLDED_IN)
        for agent in FOLDED_IN:
            meta = loader.registry.get_meta(agent)
            assert meta.source == "builtin", f"{agent} 来源应为 builtin"
            inst = loader.registry.get_instance(agent)
            result = inst.handle(Message(topic="t", from_agent="probe", payload={
                "content": "Kafka ISR replica count is 3. See https://kafka.apache.org/ .\n"
                           "| A | B |\n|---|---|\n| 1 | 2 |\n\n```sql\nSELECT 1;\n```\n"
                           "# 标题\n第一节内容足够长。\n## 第二节\n也足够长的内容。",
                "task_id": "probe"}))
            assert (result or {}).get("status") == "ok", f"{agent} 执行失败"

    def test_declares_sandbox_trusted_like_other_builtins(self):
        from pipeline_core.agent_loader import declares_sandbox_trust

        for agent in FOLDED_IN:
            assert declares_sandbox_trust(PROJECT / "agents" / f"{agent}.py"), (
                f"{agent} 是内置件但没声明 SANDBOX_TRUSTED（与其它内置件口径不一致）")

    def test_no_name_clash_among_agents(self):
        """内置 AGENT_NAME 两两不重（并入时最容易出的错）。"""
        from pipeline_core.agent_loader import AgentLoader
        from pipeline_core.registry import Registry

        loader = AgentLoader(Registry(), None, str(PROJECT / "agents"))
        names = loader.discover()
        assert len(names) == len(set(names)), "AGENT_NAME 有重复"


class TestPluginHelloTemplate:
    """外部作者模板（examples/plugin-hello）：机制参照，不随产品发布。"""

    def test_template_shape_intact(self):
        import tomllib

        pkg = EXAMPLES / "plugin-hello"
        data = tomllib.loads((pkg / "pyproject.toml").read_text(encoding="utf-8"))
        groups = data["project"]["entry-points"]
        assert "doc_pipeline.agents" in groups
        assert groups["doc_pipeline.agents"]["char_stats"] == \
            "doc_pipeline_plugin_hello.char_stats"

    def test_examples_dir_has_no_ghost_dirs(self):
        """并入后 examples/ 只留模板——五个已删目录不许回魂。"""
        ghost = [d.name for d in EXAMPLES.glob("plugin-*") if d.name != "plugin-hello"]
        assert not ghost, f"已并入 agents/ 的插件目录又出现在 examples/：{ghost}"


class TestHonestEcosystemCount:
    """验收线第 2 条的诚实口径：机制已钉住，真实第三方 = 0。"""

    def test_real_third_party_count_is_zero(self):
        """本机环境里没有我们的插件发行物（它们不是第三方）。

        机制证明 = tests/test_plugin_example.py 的真 venv E2E；
        这里钉的是"不许把内置件包装成第三方"的诚实口径。
        """
        from importlib.metadata import distributions

        ours = [d.metadata["Name"] for d in distributions()
                if (d.metadata["Name"] or "").startswith("doc-pipeline-plugin-")]
        assert not ours, (
            f"检测到已安装的 doc-pipeline-plugin-* 发行物：{ours}——"
            "五支已并入产品为内置件，不许再以'第三方插件'名义单独发行")
