"""池化语义：查询词按 pool_idx **分片**，不是每个实例复制跑一遍。

实测事故（2026-10-06，本机 keyless）：`docgen.yaml` 给 researcher 配了
`pool_size: 2`，而输入只有一条主题 —— 两个池实例返回**逐字节相同**的 3 条结果，
33.7s 全部花在重复出网上。根因是 `_build_node_payload` 的分片分支要求
`len(all_queries) >= pool_size`，查询词不够多时整体退回"全量复制"；
`EXTRACTS_QUERIES` 本身早在 3674de6 就由 researcher 认领、加载器也会搬进
`AgentMeta`，所以坏的是那个长度门槛，不是声明缺失。下面两条"接线"测试是
回归护栏：将来谁把认领或搬运摘掉，这里会红。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.dag_executor import DAGExecutor  # noqa: E402
from pipeline_core.registry import AgentMeta  # noqa: E402


def _ex(metas: dict[str, AgentMeta]) -> DAGExecutor:
    ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    ex.registry.get_meta.side_effect = lambda name: metas.get(name)
    return ex


def _payload(ex, input_file, base_agent: str, pool_idx: int, pool_size: int) -> dict:
    task = SimpleNamespace(id="t", dag_nodes={})
    node = SimpleNamespace(agent_name=f"{base_agent}_pool_{pool_idx}",
                           dependencies=[],
                           agent_config=SimpleNamespace(config={}))
    plan = SimpleNamespace(pipeline_name="probe", raw={}, levels=[
        [SimpleNamespace(agent_name=f"{base_agent}_pool_{pool_idx}")]])
    return ex._build_node_payload(task, node, input_file, plan, base_agent, pool_idx, pool_size)


@pytest.fixture
def queries_file(tmp_path) -> tuple[str, list[str]]:
    qs = ["向量检索的召回率", "嵌入模型的选型", "重排器的收益", "分块策略的影响"]
    p = tmp_path / "in.md"
    p.write_text("# 主题\n\n" + "\n".join(qs) + "\n", encoding="utf-8")
    return str(p), qs


class TestPoolQuerySharding:
    def test_opted_in_agent_splits_queries(self, queries_file):
        path, qs = queries_file
        metas = {"researcher": AgentMeta(name="researcher", version="2.0",
                                         extracts_queries=True)}
        ex = _ex(metas)
        a = _payload(ex, path, "researcher", 0, 2)["queries"]
        b = _payload(ex, path, "researcher", 1, 2)["queries"]
        assert a and b, "两个实例都该分到活"
        assert set(a) & set(b) == set(), f"池实例之间不许重复领取同一条 query：{a} / {b}"
        assert sorted(a + b) == sorted(qs), f"分片合起来必须等于全量：{a} + {b}"

    def test_surplus_pool_instance_gets_nothing_inst_of_rerunning(self, tmp_path):
        """查询词比实例少时，多出来的实例领空活，而不是把全量再跑一遍。"""
        p = tmp_path / "one.md"
        p.write_text("# 主题\n\n只有一条主题\n", encoding="utf-8")
        metas = {"researcher": AgentMeta(name="researcher", version="2.0",
                                         extracts_queries=True)}
        ex = _ex(metas)
        first = _payload(ex, str(p), "researcher", 0, 2)["queries"]
        second = _payload(ex, str(p), "researcher", 1, 2)["queries"]
        assert first == ["只有一条主题"], first
        assert second == [], (
            f"第二个实例领到 {second} —— 这正是「两个池跑同一条 query」的现场，"
            "它应领空活并如实回报零结果")

    def test_agent_that_did_not_opt_in_still_sees_all_queries(self, queries_file):
        """分片只属于认领了标记的 Agent；writer 这类要看到完整上下文，不许切。"""
        path, qs = queries_file
        metas = {"writer": AgentMeta(name="writer", version="2.0", extracts_queries=False)}
        ex = _ex(metas)
        got = _payload(ex, path, "writer", 1, 2)["queries"]
        assert sorted(got) == sorted(qs)

    def test_researcher_actually_claims_the_flag(self):
        """机制存在但没人认领 = 没有这个能力。researcher 必须自己声明。"""
        import agents.researcher as r
        assert getattr(r, "EXTRACTS_QUERIES", False) is True, (
            "researcher 未认领 EXTRACTS_QUERIES ⇒ pool_size>1 会退化成重复检索")

    def test_declaration_reaches_registry_meta(self):
        """声明必须真进到 AgentMeta —— 引擎读的是 meta.extracts_queries，不是模块变量。

        这条是上面四条的"接线"证明：模块里写了 True 但加载器没取，效果等于没写。
        """
        from pipeline_core.agent_loader import AgentLoader
        from pipeline_core.registry import Registry

        reg = Registry()
        loader = AgentLoader(reg, MagicMock(),
                             agents_dir=str(Path(__file__).parent.parent / "agents"))
        loader.discover()
        loader.register(["researcher"])
        meta = reg.get_meta("researcher")
        assert meta is not None, "researcher 没进注册表，注册环节先断了"
        assert getattr(meta, "extracts_queries", False) is True, (
            f"AgentMeta.extracts_queries={getattr(meta, 'extracts_queries', None)!r}，"
            "加载器没把模块声明搬进来 ⇒ 分片分支永远走不到")
