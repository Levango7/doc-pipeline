"""tests/test_subpipeline.py — 子流水线内联（call）。

展开式设计的核心断言只有一条：**内联之后仍是普通节点**，所以幂等键、检查点、
重试、when、产物契约一律照旧；代价是图变大。这里既测展开正确性，也测那几条
"看起来会自己好"的边界：循环、深度、被引方漂移。
"""
import pytest

from pipeline_core.naming import agent_of
from pipeline_core.scheduler import ExecutionPlan, LockfileMismatchError, Scheduler

PROJECT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _sched(tmp_path):
    return Scheduler(agents_dir=str(PROJECT / "agents"), pipeline_dir=str(tmp_path))


def _write(tmp_path, name, agents, levels, edges=None):
    lines = [f"_name: {name}", "_version: '1.0'", "description: test fixture", "agents:"]
    for a in agents:
        lines.append(f"  - name: {a['name']}")
        lines.append('    version: "1.0"')
        if a.get("call"):
            lines.append(f"    call: {a['call']}")
        if a.get("pool_size"):
            lines.append(f"    pool_size: {a['pool_size']}")
        lines.append(f"    dependencies: {a.get('dependencies', [])}")
    lines.append("topology:")
    lines.append("  type: dag")
    lines.append("  levels:")
    for lvl in levels:
        lines.append(f"    - {lvl}")
    if edges is not None:
        lines.append("  edges:")
        for e in edges:
            lines.append(f"    - {list(e)}")
    (tmp_path / f"{name}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


SUB_AGENTS = [
    {"name": "checker", "dependencies": []},
    {"name": "fact_checker", "dependencies": ["checker"]},
]


def _sub(tmp_path, name="sub_review", agents=None, levels=None):
    agents = agents or SUB_AGENTS
    if levels is None:
        # 默认按 agents 声明顺序串成一条链（避免 levels 与 agents 不一致，
        # 那会让子流水线在解析期就报"Agent 未定义"，测试测的根本不是被测路径）
        levels = [[a["name"]] for a in agents]
    _write(tmp_path, name, agents, levels)
    return name


class TestExpansion:
    def test_call_expands_into_aliased_nodes(self, tmp_path):
        _sub(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
            {"name": "layout", "dependencies": ["review"]},
        ], [["writer"], ["review"], ["layout"]])

        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        ids = [n.agent_name for lvl in plan.levels for n in lvl]
        assert "review" not in ids, "call 节点本身不是 Agent，不能留在图里"
        assert "checker__review" in ids and "fact_checker__review" in ids
        assert {agent_of(i) for i in ids} == {"writer", "checker", "fact_checker", "layout"}

    def test_parent_edges_move_to_sub_boundary(self, tmp_path):
        """call 的前置接到子图入口，依赖 call 的接到子图出口。"""
        _sub(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
            {"name": "layout", "dependencies": ["review"]},
        ], [["writer"], ["review"], ["layout"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        by_id = {n.agent_name: n for lvl in plan.levels for n in lvl}

        assert "writer" in by_id["checker__review"].dependencies
        assert by_id["layout"].dependencies == ["fact_checker__review"]

    def test_levels_are_reordered_by_dependencies(self, tmp_path):
        _sub(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
            {"name": "layout", "dependencies": ["review"]},
        ], [["writer"], ["review"], ["layout"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        tiers = [[n.agent_name for n in lvl] for lvl in plan.levels]
        assert tiers == [["writer"], ["checker__review"], ["fact_checker__review"],
                         ["layout"]], tiers

    def test_same_agent_in_parent_and_sub_do_not_collide(self, tmp_path):
        """父子都用 checker：身份必须不同，否则 dag_nodes 互相覆盖、结果张冠李戴。"""
        _sub(tmp_path, agents=[{"name": "checker", "dependencies": []}])
        _write(tmp_path, "parent", [
            {"name": "checker", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["checker"]},
        ], [["checker"], ["review"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        ids = sorted(n.agent_name for lvl in plan.levels for n in lvl)
        assert ids == ["checker", "checker__review"], ids
        assert all(agent_of(i) == "checker" for i in ids)

    def test_pool_inside_sub_keeps_pool_index(self, tmp_path):
        _sub(tmp_path, agents=[{"name": "researcher", "pool_size": 2, "dependencies": []}])
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_review", "dependencies": []},
        ], [["review"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        ids = sorted(n.agent_name for lvl in plan.levels for n in lvl)
        assert ids == ["researcher_pool_0__review", "researcher_pool_1__review"], ids
        assert all(agent_of(i) == "researcher" for i in ids)

    def test_plan_without_call_is_untouched(self, tmp_path):
        """退出门：不写 call 时，手写的层级一字不改地保留。"""
        _write(tmp_path, "plain", [
            {"name": "researcher", "dependencies": []},
            {"name": "fetcher", "dependencies": ["researcher"]},
        ], [["researcher"], ["fetcher"]])
        plan = _sched(tmp_path).parse("plain", verify_lock=False)
        assert [[n.agent_name for n in lvl] for lvl in plan.levels] == [
            ["researcher"], ["fetcher"]]


class TestCallIsRejectedEarly:
    def test_missing_target_lists_the_name(self, tmp_path):
        _write(tmp_path, "parent", [
            {"name": "review", "call": "no_such_sub", "dependencies": []},
        ], [["review"]])
        with pytest.raises(ValueError, match="no_such_sub"):
            _sched(tmp_path).parse("parent", verify_lock=False)

    def test_cycle_is_rejected_with_the_chain(self, tmp_path):
        _write(tmp_path, "a", [{"name": "b", "call": "b", "dependencies": []}], [["b"]])
        _write(tmp_path, "b", [{"name": "a", "call": "a", "dependencies": []}], [["a"]])
        with pytest.raises(ValueError, match="循环引用"):
            _sched(tmp_path).parse("a", verify_lock=False)

    def test_same_sub_reused_by_two_call_nodes(self, tmp_path):
        """两条节点引用同一个子流水线是合法复用，不是环；身份也不能互相覆盖。

        这条同时是给上一轮的判据兜底：环判据我改过一次（从"call 节点名在栈里"
        改成"被引流水线名在栈里"），只测 a→b→a 的话，改坏了也发现不了。
        """
        _sub(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
            {"name": "audit", "call": "sub_review", "dependencies": ["review"]},
        ], [["writer"], ["review"], ["audit"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        ids = sorted(n.agent_name for lvl in plan.levels for n in lvl)
        assert ids == ["checker__audit", "checker__review", "fact_checker__audit",
                       "fact_checker__review", "writer"], ids
        audit_entry = [n for lvl in plan.levels for n in lvl
                       if n.agent_name == "checker__audit"][0]
        assert audit_entry.dependencies == ["fact_checker__review"]

    def test_three_level_chain_is_within_the_cap(self, tmp_path):
        """上限 3 是"含叶子共 3 条流水线"，别把合法深度判成违规。"""
        _write(tmp_path, "leaf", [{"name": "checker", "dependencies": []}], [["checker"]])
        _write(tmp_path, "mid", [{"name": "n", "call": "leaf", "dependencies": []}], [["n"]])
        _write(tmp_path, "top", [{"name": "m", "call": "mid", "dependencies": []}], [["m"]])
        plan = _sched(tmp_path).parse("top", verify_lock=False)
        assert [n.agent_name for lvl in plan.levels for n in lvl] == ["checker__n__m"]

    def test_nested_alias_cannot_collide_with_a_flat_name(self, tmp_path):
        """`a` 里再调 `b` 与一个名叫 `a_b` 的 call 节点，身份不能撞在一起。

        这是我把嵌套别名从 `writer__a_b` 改成 `writer__a__b` 的直接原因：
        撞名不会报错，只会让 dag_nodes 后写的覆盖先写的。
        """
        _write(tmp_path, "inner", [{"name": "checker", "dependencies": []}], [["checker"]])
        _write(tmp_path, "a", [{"name": "b", "call": "inner", "dependencies": []}], [["b"]])
        _write(tmp_path, "a_b", [{"name": "checker", "dependencies": []}], [["checker"]])
        _write(tmp_path, "parent", [
            {"name": "via_nest", "call": "a", "dependencies": []},
            {"name": "a_b", "call": "a_b", "dependencies": []},
        ], [["via_nest", "a_b"]])

        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        ids = sorted(n.agent_name for lvl in plan.levels for n in lvl)
        assert len(ids) == len(set(ids)) == 2, ids
        assert "checker__b__via_nest" in ids and "checker__a_b" in ids, ids

    def test_depth_cap(self, tmp_path):
        # s1 → s2 → s3 → s4：第四层超上限
        for i in (4, 3, 2):
            _write(tmp_path, f"s{i}",
                   [{"name": f"n{i}", "call": f"s{i + 1}", "dependencies": []}],
                   [[f"n{i}"]])
        _write(tmp_path, "s4", [{"name": "checker", "dependencies": []}], [["checker"]])
        _write(tmp_path, "s1", [{"name": "n1", "call": "s2", "dependencies": []}], [["n1"]])
        with pytest.raises(ValueError, match="嵌套超过上限"):
            _sched(tmp_path).parse("s1", verify_lock=False)

    def test_call_node_rejects_its_own_config(self, tmp_path):
        """父子各有一份配置会难推断：配置属于子流水线。"""
        _sub(tmp_path)
        _write(tmp_path, "parent", [{
            "name": "review", "call": "sub_review", "dependencies": [],
        }], [["review"]])
        p = tmp_path / "parent.yaml"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "  - name: review\n    version: \"1.0\"\n    call: sub_review\n",
            "  - name: review\n    version: \"1.0\"\n    call: sub_review\n"
            "    config:\n      max_retries: 9\n"), encoding="utf-8")
        with pytest.raises(ValueError, match="不接受"):
            _sched(tmp_path).parse("parent", verify_lock=False)


class TestCallerLockFollowsCallee:
    """被引子流水线改了，调用方的 lockfile 必须报漂移——否则换掉整段子流程而
    护栏一声不响，等于把"版本锁定"绕过去。"""

    def _parent(self, tmp_path):
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
        ], [["writer"], ["review"]])

    def test_lock_generated_then_drifts_when_sub_changes(self, tmp_path):
        _sub(tmp_path)
        self._parent(tmp_path)
        sched = _sched(tmp_path)
        sched.generate_lockfile(sched.parse("parent", verify_lock=False),
                                output_dir=str(tmp_path))
        assert sched.parse("parent", verify_lock=True).node_count == 3

        # 子流水线加一个节点（不动调用方一个字节）
        _sub(tmp_path, agents=[
            {"name": "checker", "dependencies": []},
            {"name": "fact_checker", "dependencies": ["checker"]},
            {"name": "layout", "dependencies": ["fact_checker"]},
        ])
        with pytest.raises(LockfileMismatchError) as exc:
            sched.parse("parent", verify_lock=True)
        assert "拓扑漂移" in str(exc.value)

    def test_caller_hash_changes_even_with_same_node_count(self, tmp_path):
        """子图内部换 Agent 但节点数不变时，只靠 node_count 抓不住。"""
        _sub(tmp_path, agents=[{"name": "checker", "dependencies": []}])
        self._parent(tmp_path)
        sched = _sched(tmp_path)
        plan_a = sched.parse("parent", verify_lock=False)
        hash_a = Scheduler._topology_hash(plan_a)

        _sub(tmp_path, agents=[{"name": "layout", "dependencies": []}])
        plan_b = sched.parse("parent", verify_lock=False)
        assert plan_a.node_count == plan_b.node_count
        assert Scheduler._topology_hash(plan_b) != hash_a


class TestRuntimeIdentity:
    """展开后的图要真能跑：两个内联实例不能共用身份。

    解析层测不到的是运行期：RPC topic 是 `f"{agent}.input"`，两条
    `checker__review` / `checker__audit` 都会走 `checker.input`；幂等键、
    dag_nodes 主键、上下游产物可见性都靠"节点身份带别名"来区分。
    """

    def _run(self, tmp_path):
        import sys
        from pathlib import Path
        from unittest.mock import patch

        sys.path.insert(0, str(PROJECT))
        from pipeline_core import PipelineOrchestrator

        _write(tmp_path, "sub_probe", [
            {"name": "layout", "dependencies": []},
        ], [["layout"]])
        _write(tmp_path, "rt_parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_probe", "dependencies": ["writer"]},
            {"name": "audit", "call": "sub_probe", "dependencies": ["review"]},
        ], [["writer"], ["review"], ["audit"]])

        def fake_layout(self, msg):
            # upstream_content 是引擎按产物契约铺到顶层的上游正文，用来验边界接线
            return {"status": "ok", "content": f"[{msg.payload.get('node')}]",
                    "upstream_content": msg.payload.get("content")}

        def fake_safe_writer(self, msg):
            # 真落盘：交付契约要求 output_path 指向的文件确实存在，否则不该算 done
            target = msg.payload.get("target_file") or str(tmp_path / "out.md")
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(str(msg.payload.get("content") or ""), encoding="utf-8")
            return {"status": "ok", "written": target,
                    "seen": msg.payload.get("content")}

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        # 类必须从注册**之后**的 sys.modules 里取：agent_loader 会重新加载
        # agents/*.py 并覆写这些键，注册前拿到的是另一个类对象，patch 会空转
        # （见 tests/test_e2e_mock.py 的同源教训）。
        layout_cls = sys.modules["agents.layout"].LayoutAgent
        writer_cls = sys.modules["agents.writer"].WriterAgent
        safe_writer_cls = sys.modules["agents.safe_writer_agent"].SafeWriterAgent
        with patch.object(layout_cls, "handle", fake_layout), \
             patch.object(writer_cls, "handle",
                          lambda self, msg: {"status": "ok",
                                             "content": f"[{msg.payload.get('node')}]"
                                             + "\n\n## 段\n\n正文足够长以便通过保真底线检查。" * 3}), \
             patch.object(safe_writer_cls, "handle", fake_safe_writer):
            plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                             pipeline_dir=str(tmp_path)).parse("rt_parent", verify_lock=False)
            try:
                task = orch.run_plan(plan, input_file=str(PROJECT / "test_input.md"), wait=True)
            finally:
                orch.shutdown()
        return task

    def test_inlined_instances_have_separate_identities(self, tmp_path):
        task = self._run(tmp_path)
        assert task.status.value == "done", f"{task.status.value}: {task.error}"
        ids = sorted(task.dag_nodes)
        assert ids == ["layout__audit", "layout__review", "writer"], ids
        # 两个内联实例各自看到自己的节点身份，没有互相覆盖
        assert task.dag_nodes["layout__review"].result["content"] == "[layout__review]"
        assert task.dag_nodes["layout__audit"].result["content"] == "[layout__audit]"

    def test_chained_call_sees_previous_subflow_output(self, tmp_path):
        """链式 call：audit 的入口拿到的上游是 review 出口的内容，不是 writer 的。"""
        payloads = self._run(tmp_path).result
        audit = payloads["layout__audit"]
        assert audit.get("upstream_content") == "[layout__review]", (
            f"链式子流水线的边界产物没接上：{audit!r}")


class TestPoolMergeIsAliasAware:
    """池化结果归并必须按家族分组，否则内联节点会被混进父图同名节点。"""

    def _task(self):
        from pipeline_core.pipeline import PipelineTask

        task = PipelineTask(id="t-merge", pipeline_name="p", input_file="", config={})
        for key, n in (("researcher_pool_0", 1), ("researcher_pool_1", 2),
                       ("researcher_pool_0__review", 3), ("researcher_pool_1__review", 4)):
            task.result[key] = {"status": "ok", "results": [{"i": n}], "total": 1}
        return task

    def test_parent_and_inlined_pools_merge_separately(self, tmp_path):
        from pipeline_core import PipelineOrchestrator

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()       # 不 mock executor：本例只测归并，不执行任何节点
        try:
            task = self._task()
            orch._merge_pooled_results(task)
            parent = task.result.get("researcher") or {}
            inlined = task.result.get("researcher__review") or {}
            assert [r["i"] for r in parent.get("results", [])] == [1, 2], parent
            assert [r["i"] for r in inlined.get("results", [])] == [3, 4], inlined
        finally:
            orch.shutdown()


class TestExpandedPlanShape:
    def test_expanded_plan_is_a_valid_executionplan(self, tmp_path):
        _sub(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
        ], [["writer"], ["review"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        assert isinstance(plan, ExecutionPlan)
        assert plan.node_count == 3
        # 展开后每个节点都能被 agent_of 还原成真实 Agent 名
        agents = {f.stem for f in (PROJECT / "agents").glob("*.py")}
        for lvl in plan.levels:
            for n in lvl:
                assert agent_of(n.agent_name) in agents
