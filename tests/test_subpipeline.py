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
        if a.get("inputs"):
            lines.append(f"    inputs: {a['inputs']}")
        if a.get("when"):
            lines.append(f"    when: {a['when']}")
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
        """call 的前置只接到子图**入口**，依赖 call 的接到子图出口。

        入口判据一度写成"d 不在 renamed 里"，而 deps 那时已经改名，于是每个节点
        都被当成入口：writer 成了 fact_checker__review 的依赖，整条子流水线的
        层级和并发窗口跟着错。所以这里把"非入口不得带父图前置"也断言掉。
        """
        _sub(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_review", "dependencies": ["writer"]},
            {"name": "layout", "dependencies": ["review"]},
        ], [["writer"], ["review"], ["layout"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        by_id = {n.agent_name: n for lvl in plan.levels for n in lvl}

        assert by_id["checker__review"].dependencies == ["writer"]
        assert by_id["fact_checker__review"].dependencies == ["checker__review"], (
            "父图的前置漏进了子图内部节点")
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

    def test_missing_fragment_names_the_co_location_rule(self, tmp_path):
        """片段找不到时，报错要说出"跟调用方同目录"这条部署要求。

        单文件复制流水线的用户（以及把一条 yaml 拷进沙箱的测试）撞的就是这个，
        只报"流水线不存在"会让人去查名字拼写，而真正的问题是目录里没有片段。
        """
        _write(tmp_path, "needs_frag", [
            {"name": "tail", "call": "_not_installed", "dependencies": []},
        ], [["tail"]])
        with pytest.raises(ValueError, match="同一个 pipelines/ 目录"):
            _sched(tmp_path).parse("needs_frag", verify_lock=False)

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


class TestCallInputs:
    """call 传参：子流水线读 `inputs.*`，同一段流程在不同调用方走不同分支。

    没有参数化，抽取就只是"把四份 YAML 复制成一份再抄回去"——阈值写死在共享
    tail 里，四条流水线各自的要求无法同时满足，最后又要拆回去。
    """

    def _tail(self, tmp_path, name="sub_tail", defaults=None):
        """一条按参数决定走不走的小流水线。defaults=None 表示不声明默认值。"""
        agent = {"name": "checker", "dependencies": [],
                 "when": {"path": "inputs.mode", "op": "==", "value_from": "inputs.want"}}
        if defaults is not None:
            agent["inputs"] = defaults
        _write(tmp_path, name, [agent], [["checker"]])
        return name

    def test_inputs_reach_every_inlined_node(self, tmp_path):
        self._tail(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "writer", "dependencies": []},
            {"name": "review", "call": "sub_tail", "dependencies": ["writer"],
             "inputs": {"mode": "fast", "want": "fast"}},
        ], [["writer"], ["review"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        node = [n for lvl in plan.levels for n in lvl if n.agent_name == "checker__review"][0]
        assert node.inputs == {"mode": "fast", "want": "fast"}

    def test_caller_overrides_subflow_defaults(self, tmp_path):
        """子流水线自带默认值，调用方给的值覆盖它——抽取出来的 tail 才有可用性。"""
        self._tail(tmp_path, defaults={"mode": "slow", "want": "slow"})
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_tail", "dependencies": [],
             "inputs": {"mode": "fast"}},
        ], [["review"]])
        plan = _sched(tmp_path).parse("parent", verify_lock=False)
        node = plan.levels[0][0]
        assert node.inputs == {"mode": "fast", "want": "slow"}, node.inputs

    def test_subflow_defaults_make_it_parse_standalone(self, tmp_path):
        """带默认值的片段自己能独立解析（也因此能被列出、被 lint）。"""
        name = self._tail(tmp_path, defaults={"mode": "slow", "want": "slow"})
        assert _sched(tmp_path).parse(name, verify_lock=False).node_count == 1

    def test_fragment_without_defaults_cannot_run_alone(self, tmp_path):
        """没默认值又没人传参：参数根本无从解析，解析期就该拒绝。"""
        name = self._tail(tmp_path)
        with pytest.raises(ValueError, match="inputs.mode"):
            _sched(tmp_path).parse(name, verify_lock=False)

    def test_plain_agent_inputs_must_be_read_by_its_own_when(self, tmp_path):
        """普通节点上的 inputs 只有当自己的 when 读到它才有意义，否则是配置幻觉。"""
        _write(tmp_path, "plain", [
            {"name": "writer", "dependencies": [], "inputs": {"mode": "fast"}},
        ], [["writer"]])
        with pytest.raises(ValueError, match="没有任何 when|只能作为 when"):
            _sched(tmp_path).parse("plain", verify_lock=False)

    def test_inputs_must_be_a_mapping(self, tmp_path):
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_tail", "dependencies": [],
             "inputs": ["mode"]},
        ], [["review"]])
        with pytest.raises(ValueError, match="必须是映射"):
            _sched(tmp_path).parse("parent", verify_lock=False)

    def test_input_key_with_dot_is_rejected(self, tmp_path):
        """when 用点号做路径，键里带点就永远取不到——写的时候拦住。"""
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_tail", "dependencies": [],
             "inputs": {"a.b": 1, "mode": "x", "want": "x"}},
        ], [["review"]])
        with pytest.raises(ValueError, match="不能含点号"):
            _sched(tmp_path).parse("parent", verify_lock=False)

    def test_missing_input_fails_at_parse_time(self, tmp_path):
        """引用了却没传：现在报错，而不是任务跑挂或者被 all 短路静默绕过。"""
        self._tail(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_tail", "dependencies": [],
             "inputs": {"mode": "fast"}},
        ], [["review"]])
        with pytest.raises(ValueError, match="inputs.want"):
            _sched(tmp_path).parse("parent", verify_lock=False)

    def test_unread_input_is_rejected(self, tmp_path):
        """传了没人读的参数＝键名写错。放着不管，节点就会拿默认值继续跑。"""
        self._tail(tmp_path)
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_tail", "dependencies": [],
             "inputs": {"mode": "fast", "want": "fast", "spare": 1}},
        ], [["review"]])
        with pytest.raises(ValueError, match="没有人读到.*spare"):
            _sched(tmp_path).parse("parent", verify_lock=False)

    def test_typo_in_input_name_is_rejected(self, tmp_path):
        """min_scor 这类拼写错误必须被拦住，而不是静默走默认值。"""
        self._tail(tmp_path, defaults={"mode": "slow", "want": "slow"})
        _write(tmp_path, "parent", [
            {"name": "review", "call": "sub_tail", "dependencies": [],
             "inputs": {"wdan": "fast"}},
        ], [["review"]])
        with pytest.raises(ValueError, match="没有人读到.*wdan"):
            _sched(tmp_path).parse("parent", verify_lock=False)

    def test_topo_hash_moves_with_input_values(self, tmp_path):
        """改实参=改分支走向，调用方 lockfile 必须跟着漂移。"""
        self._tail(tmp_path)

        def build(want):
            _write(tmp_path, "parent", [
                {"name": "review", "call": "sub_tail", "dependencies": [],
                 "inputs": {"mode": "fast", "want": want}},
            ], [["review"]])
            return Scheduler._topology_hash(
                _sched(tmp_path).parse("parent", verify_lock=False))

        assert build("fast") != build("slow")

    def test_outer_args_do_not_leak_into_inner_scope(self, tmp_path):
        """嵌套调用的作用域要隔开：外层的参数不该出现在内层节点的 inputs 里。

        否则同名键会跨层生效，内层片段的行为取决于外层写了什么——那就不叫
        可复用片段了。判据对着旧实现写：把 inputs=dict(n.inputs) 改回
        {**call_node.inputs, **n.inputs} 这条就会红。
        """
        _write(tmp_path, "leaf", [
            {"name": "checker", "dependencies": [],
             "when": {"path": "inputs.leaf_only", "op": "==", "value_from": "inputs.leaf_want"},
             "inputs": {"leaf_only": "a", "leaf_want": "a"}},
        ], [["checker"]])
        _write(tmp_path, "mid", [
            {"name": "audit", "dependencies": [],
             "when": {"path": "inputs.outer_key", "op": "==", "value": 5},
             "inputs": {"outer_key": 0}},
            {"name": "n", "call": "leaf", "dependencies": ["audit"]},
        ], [["audit"], ["n"]])
        _write(tmp_path, "top", [
            {"name": "m", "call": "mid", "dependencies": [], "inputs": {"outer_key": 5}},
        ], [["m"]])
        plan = _sched(tmp_path).parse("top", verify_lock=False)
        nodes = {n.agent_name: n for lvl in plan.levels for n in lvl}
        assert nodes["audit__m"].inputs == {"outer_key": 5}, nodes["audit__m"].inputs
        assert nodes["checker__n__m"].inputs == {"leaf_only": "a", "leaf_want": "a"}, (
            f"外层参数漏进内层作用域: {nodes['checker__n__m'].inputs}")


class TestInlinedNodesKeepAgentCapabilities:
    """内联节点必须保留 Agent 自己声明的能力。

    这两条是真机跑出来的：docgen 换成 call 片段之后 safe_writer 的产物没被记进
    `task.output_path`（交付契约反过来误报"没有交付物"），quality_gate 的重做循环
    则整段不再进入。两次同源——`get_meta(node.agent_name)` 用别名名查注册表得到
    None。None 不报错，只是"这个能力悄悄没了"，正是本项目一路在关的那类洞。
    """

    def _run(self, tmp_path, tail_agents, patches):
        """跑 writer → call(片段)；patches 是 (模块名, 类名, handle 替身)。"""
        import sys
        from contextlib import ExitStack
        from unittest.mock import patch

        sys.path.insert(0, str(PROJECT))
        from pipeline_core import PipelineOrchestrator

        _write(tmp_path, "sub_cap", tail_agents, [[a["name"]] for a in tail_agents])
        _write(tmp_path, "cap_parent", [
            {"name": "writer", "dependencies": []},
            {"name": "tail", "call": "sub_cap", "dependencies": ["writer"]},
        ], [["writer"], ["tail"]])

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        # 注册之后 sys.modules 里的类才是真正被实例化的那一份（见 test_e2e_mock.py）
        stack = []
        for mod, cls_name, handler in patches:
            stack.append(patch.object(getattr(sys.modules[mod], cls_name), "handle", handler))
        plan = _sched(tmp_path).parse("cap_parent", verify_lock=False)
        try:
            with ExitStack() as box:
                for cm in stack:
                    box.enter_context(cm)
                task = orch.run_plan(plan, input_file=str(PROJECT / "test_input.md"),
                                     wait=True)
        finally:
            orch.shutdown()
        return task

    @staticmethod
    def _writer_handle(self, msg):
        return {"status": "ok", "content": "正文足够长以便通过保真底线检查。" * 3}

    def test_inlined_sink_still_records_the_deliverable(self, tmp_path):
        from pathlib import Path
        from unittest.mock import patch

        from pipeline_core.dag_executor import DAGExecutor

        seen = {}
        real = DAGExecutor._record_task_output

        def spy(self, task, meta, payload, result, plan):
            seen["meta"] = meta
            return real(self, task, meta, payload, result, plan)

        def fake_layout(self, msg):
            return {"status": "ok",
                    "content": "\n\n".join([str(msg.payload.get("content") or ""),
                                            "## 排过版", "正文。"])}

        def fake_safe_writer(self, msg):
            path = msg.payload.get("target_file") or msg.payload.get("target")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(str(msg.payload.get("content") or ""), encoding="utf-8")
            return {"status": "ok", "written": str(path)}

        with patch.object(DAGExecutor, "_record_task_output", spy):
            task = self._run(tmp_path, [
                {"name": "layout", "dependencies": []},
                {"name": "safe_writer", "dependencies": ["layout"]},
            ], [("agents.writer", "WriterAgent", self._writer_handle),
                ("agents.layout", "LayoutAgent", fake_layout),
                ("agents.safe_writer_agent", "SafeWriterAgent", fake_safe_writer)])

        assert seen.get("meta") is not None, (
            "落盘节点的 meta 查成了 None——WRITES_OUTPUT 随之失效，交付契约会被误判")
        assert task.output_path, "落盘节点的产物没记进 task.output_path"
        assert Path(task.output_path).exists(), task.output_path
        assert task.status.value == "done", f"{task.status.value}: {task.error}"

    def test_inlined_quality_gate_still_enters_regeneration(self, tmp_path):
        from unittest.mock import patch

        from pipeline_core.dag_executor import DAGExecutor

        calls = []

        def fake_gate(self, msg):
            return {"status": "fail", "needs_regenerate": True, "can_regenerate": True,
                    "overall_score": 55, "generation_count": 0}

        def spy(self, task, node, result, msg_payload, **kw):
            calls.append((node.agent_name, kw.get("regenerate_agent"),
                          kw.get("recheck_agent")))
            return {"status": "pass", "overall_score": 88, "needs_regenerate": False}

        with patch.object(DAGExecutor, "_handle_regeneration", spy):
            task = self._run(tmp_path, [{"name": "quality_gate", "dependencies": []}],
                             [("agents.writer", "WriterAgent", self._writer_handle),
                              ("agents.quality_gate", "QualityGateAgent", fake_gate)])

        assert calls == [("quality_gate__tail", "writer", "quality_gate")], (
            f"内联 gate 没进入重做循环（supports_regeneration 查不到就等于质量门失效）:"
            f" {calls}")
        assert task.status.value == "done", f"{task.status.value}: {task.error}"


class TestInputsDriveRuntimeBranch:
    """参数在运行期真的要改变走向：同一段子流程，一处执行、一处按条件跳过。

    只在解析层比 dict 是不够的——抽取的价值全在于"一份 tail，两种行为"，
    而行为由 executor 读 node.inputs 求 when 决定。
    """

    def test_same_tail_two_verdicts(self, tmp_path):
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(PROJECT))
        from pipeline_core import PipelineOrchestrator

        _write(tmp_path, "sub_gate", [
            {"name": "checker", "dependencies": [],
             "when": {"path": "inputs.enabled", "op": "==", "value": True}},
        ], [["checker"]])
        _write(tmp_path, "branchy", [
            {"name": "writer", "dependencies": []},
            {"name": "gate_on", "call": "sub_gate", "dependencies": ["writer"],
             "inputs": {"enabled": True}},
            {"name": "gate_off", "call": "sub_gate", "dependencies": ["gate_on"],
             "inputs": {"enabled": False}},
        ], [["writer"], ["gate_on"], ["gate_off"]])

        seen = []

        def fake_checker(self, msg):
            seen.append(msg.payload.get("node"))
            return {"status": "ok", "content": "checker 跑过了"}

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        checker_cls = sys.modules["agents.checker"].CheckerAgent
        writer_cls = sys.modules["agents.writer"].WriterAgent
        with patch.object(checker_cls, "handle", fake_checker), \
             patch.object(writer_cls, "handle",
                          lambda self, msg: {"status": "ok", "content": "正文"}):
            plan = _sched(tmp_path).parse("branchy", verify_lock=False)
            try:
                task = orch.run_plan(plan, input_file=str(PROJECT / "test_input.md"),
                                     wait=True)
            finally:
                orch.shutdown()

        assert seen == ["checker__gate_on"], f"内联实例的执行走向不对: {seen}"
        off = task.dag_nodes["checker__gate_off"]
        assert off.status == "skipped" and off.skip_reason == "condition", (
            f"跳过要留痕，不能算成功也不能算失败: {off.status}/{off.skip_reason}")
        # 被条件跳过的分支不阻断下游（这里 off 之后没有节点，图仍应正常收尾）
        assert task.status.value == "done", f"{task.status.value}: {task.error}"


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


class TestAgentNameLookupsStaySafe:
    """按 Agent 名取上游结果的写法，在内联之后必须仍然有效。

    Agent 代码里普遍是 `dependencies_results.get("ingest")`，报表里是
    `task.result.get("quality_gate")`。内联把节点名改成 `ingest__tail` 之后，
    这类查询不会报错——它只会拿到空，于是下游静默少一份输入。
    """

    def _payload_for(self, tmp_path, deps, results):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from pipeline_core.dag_executor import DAGExecutor
        from pipeline_core.registry import AgentMeta

        src = tmp_path / "in.md"
        src.write_text("# 主题\n\n为什么要内联子流水线？\n", encoding="utf-8")

        ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        metas = {"downstream": AgentMeta(name="downstream", produces={"content": "last"})}
        ex.registry.get_meta.side_effect = lambda n: metas.get(n.split("_pool_")[0])
        nodes = {name: SimpleNamespace(result=res, dependencies=[], status="success", attempts=1)
                 for name, res in results.items()}
        task = SimpleNamespace(id="t", dag_nodes=nodes)
        node = SimpleNamespace(agent_name="downstream", dependencies=list(deps),
                               agent_config=SimpleNamespace(config={}))
        plan = SimpleNamespace(pipeline_name="p", raw={}, levels=[[node]])
        return ex._build_node_payload(task, node, str(src), plan, "downstream", 0, 1)

    def test_inlined_dependency_is_also_reachable_by_agent_name(self, tmp_path):
        payload = self._payload_for(tmp_path, ["ingest__tail"],
                                     {"ingest__tail": {"files": ["a.md"]}})
        got = payload["dependencies_results"]
        assert got["ingest__tail"] == {"files": ["a.md"]}
        assert got.get("ingest") == {"files": ["a.md"]}, "别名键在，按 Agent 名的取法就断了"

    def test_ambiguous_agent_gets_no_alias_key(self, tmp_path):
        """同名 Agent 出现两次时不猜：只留节点键，不给 Agent 键。"""
        payload = self._payload_for(
            tmp_path, ["ingest__a", "ingest__b"],
            {"ingest__a": {"n": 1}, "ingest__b": {"n": 2}})
        got = payload["dependencies_results"]
        assert "ingest" not in got, f"两处同名不该被并成一个: {sorted(got)}"
        assert got["ingest__a"]["n"] == 1 and got["ingest__b"]["n"] == 2

    def test_report_lookup_resolves_aliased_quality_gate(self):
        from types import SimpleNamespace

        import run as run_mod

        task = SimpleNamespace(result={"quality_gate__tail": {"status": "accepted_with_warnings",
                                                              "overall_score": 55}})
        assert run_mod._result_for(task, "quality_gate")["overall_score"] == 55

    def test_report_lookup_refuses_to_guess_between_two(self):
        from types import SimpleNamespace

        import run as run_mod

        task = SimpleNamespace(result={"quality_gate__a": {"overall_score": 1},
                                       "quality_gate__b": {"overall_score": 2}})
        assert run_mod._result_for(task, "quality_gate") == {}


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
