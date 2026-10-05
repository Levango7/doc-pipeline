"""tests/test_condition_nodes.py — 条件节点（when）的接线行为。

`pipeline_core/conditions.py` 已经表驱动测过语言本身；这里测的是"语言接进引擎之后
到底改变了什么"，四个必须钉住的点：

1. 条件不成立 → 节点标 skipped + skip_reason="condition"，**不投递消息**；
2. 这种跳过是作者声明的可选分支 → 下游照常执行（不能被依赖级联一起停摆）；
3. 依赖真失败的级联跳过仍然生效（我为了 2 开了例外，不能把 3 也一起放行）；
4. 条件求值出错（路径拼错）→ 整个 run 失败。把它当成"条件不成立"就是静默跳过，
   流水线会带着一个从未执行的分支报 done。
"""
from unittest.mock import MagicMock

import pytest

from pipeline_core.conditions import ConditionError
from pipeline_core.dag_executor import DAGExecutor
from pipeline_core.naming import agent_of
from pipeline_core.pipeline import PipelineTask, StepResult, TaskNode, TaskStatus
from pipeline_core.registry import AgentMeta
from pipeline_core.scheduler import (
    AgentConfig,
    ExecutionNode,
    ExecutionPlan,
    Scheduler,
)
from pipeline_core.scheduler import (
    ConditionError as _CE,  # noqa: F401  确认异常从 scheduler 可达（解析期用它报错）
)

PROJECT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _node(name, deps=None, when=None, config=None):
    return ExecutionNode(
        agent_name=name,
        agent_config=AgentConfig(name=name.split("_pool_")[0], config=config or {}),
        dependencies=deps or [],
        when=when,
    )


def _task(names_results: dict, node_objs: dict) -> PipelineTask:
    """names_results: 节点名 -> 结果 dict（可为 None 表示未执行）。"""
    task = PipelineTask(id="t-cond", pipeline_name="p", input_file="in.md", config={})
    for name, res in names_results.items():
        dn = TaskNode(name=name, agent_name=name, dependencies=list(node_objs[name].dependencies))
        dn.status = "success" if res is not None else "pending"
        dn.result = res or {}
        task.dag_nodes[name] = dn
    return task


def _executor(metas: dict[str, AgentMeta]) -> DAGExecutor:
    ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    ex.registry.get_meta.side_effect = lambda name: metas.get(name.split("_pool_")[0])
    return ex


META_GATE = AgentMeta(name="quality_gate", produces={"score": "last"})
META_RENDER = AgentMeta(name="renderer", produces={"docx": "last"})
META_SINK = AgentMeta(name="safe_writer", produces={}, writes_output=True)


class TestDispatchDecision:
    def test_false_condition_skips_without_dispatch(self):
        gate = _node("quality_gate")
        render = _node("renderer", deps=["quality_gate"],
                       when={"path": "upstream.quality_gate.overall_score",
                             "op": ">=", "value": 95})
        task = _task({"quality_gate": {"overall_score": 82}, "renderer": None},
                     {"quality_gate": gate, "renderer": render})
        ex = _executor({"quality_gate": META_GATE, "renderer": META_RENDER})
        plan = ExecutionPlan(pipeline_name="p", levels=[[gate], [render]], raw={})

        executor_pool = MagicMock()
        futures = ex._submit_level_futures(task, [render], "in.md", plan, executor_pool)

        assert futures == {}, "条件不成立的节点仍被提交执行"
        assert task.dag_nodes["renderer"].status == "skipped"
        assert task.dag_nodes["renderer"].skip_reason == "condition"
        assert "when 不成立" in task.dag_nodes["renderer"].error
        steps = [s for s in task.steps if s.agent_name == "renderer"]
        assert steps and steps[-1].status == "skipped", "跳过没进步骤报表，事后看不出这格是空的"

    def test_true_condition_dispatches(self):
        gate = _node("quality_gate")
        render = _node("renderer", deps=["quality_gate"],
                       when={"path": "upstream.quality_gate.overall_score",
                             "op": ">=", "value": 70})
        task = _task({"quality_gate": {"overall_score": 82}, "renderer": None},
                     {"quality_gate": gate, "renderer": render})
        ex = _executor({"quality_gate": META_GATE, "renderer": META_RENDER})
        plan = ExecutionPlan(pipeline_name="p", levels=[[gate], [render]], raw={})

        futures = ex._submit_level_futures(task, [render], "in.md", plan, MagicMock())
        assert len(futures) == 1
        assert task.dag_nodes["renderer"].status == "running"

    def test_no_condition_always_dispatches(self):
        """退出门：不写 when 的老流水线行为完全不变。"""
        a = _node("quality_gate")
        b = _node("renderer", deps=["quality_gate"])
        task = _task({"quality_gate": {"overall_score": 1}, "renderer": None},
                     {"quality_gate": a, "renderer": b})
        ex = _executor({"quality_gate": META_GATE, "renderer": META_RENDER})
        plan = ExecutionPlan(pipeline_name="p", levels=[[a], [b]], raw={})
        assert len(ex._submit_level_futures(task, [b], "in.md", plan, MagicMock())) == 1

    def test_condition_skipped_dependency_does_not_block_downstream(self):
        """可选分支被跳过后，下游继续执行——这正是 `when` 存在的意义。"""
        render = _node("renderer")
        render.status_marker = None
        sink = _node("safe_writer", deps=["renderer"])
        task = _task({"renderer": None, "safe_writer": None},
                     {"renderer": render, "safe_writer": sink})
        task.dag_nodes["renderer"].status = "skipped"
        task.dag_nodes["renderer"].skip_reason = "condition"
        ex = _executor({"renderer": META_RENDER, "safe_writer": META_SINK})
        plan = ExecutionPlan(pipeline_name="p", levels=[[render], [sink]], raw={})

        assert len(ex._submit_level_futures(task, [sink], "in.md", plan, MagicMock())) == 1

    def test_failed_dependency_still_cascades(self):
        """上一例开了例外，不能顺手把"依赖真失败"也放行。"""
        render = _node("renderer")
        sink = _node("safe_writer", deps=["renderer"])
        task = _task({"renderer": None, "safe_writer": None},
                     {"renderer": render, "safe_writer": sink})
        task.dag_nodes["renderer"].status = "failed"
        ex = _executor({"renderer": META_RENDER, "safe_writer": META_SINK})
        plan = ExecutionPlan(pipeline_name="p", levels=[[render], [sink]], raw={})

        futures = ex._submit_level_futures(task, [sink], "in.md", plan, MagicMock())
        assert futures == {}
        assert task.dag_nodes["safe_writer"].status == "skipped"
        assert task.dag_nodes["safe_writer"].skip_reason == "", \
            "依赖失败的级联跳过不得冒充条件跳过，否则下游也会被放行"


class TestEvaluationErrorsFailLoud:
    def test_typo_path_raises_instead_of_skipping(self):
        gate = _node("quality_gate")
        render = _node("renderer", deps=["quality_gate"],
                       when={"path": "upstream.quality_gate.oversall_score",
                             "op": ">=", "value": 70})
        task = _task({"quality_gate": {"overall_score": 82}, "renderer": None},
                     {"quality_gate": gate, "renderer": render})
        ex = _executor({"quality_gate": META_GATE, "renderer": META_RENDER})
        plan = ExecutionPlan(pipeline_name="p", levels=[[gate], [render]], raw={})

        with pytest.raises(ConditionError, match="取不到值"):
            ex._submit_level_futures(task, [render], "in.md", plan, MagicMock())

    def test_context_exposes_inputs(self):
        """`inputs.*` 是 when 的一等上下文：子流水线的阈值从这里来。"""
        a = _node("quality_gate")
        b = _node("fact_checker", deps=["quality_gate"])
        b.inputs = {"min_score": 70}
        task = _task({"quality_gate": {"overall_score": 82}, "fact_checker": None},
                     {"quality_gate": a, "fact_checker": b})
        metas = {"quality_gate": META_GATE,
                 "fact_checker": AgentMeta(name="fact_checker", produces={"verdict": "last"})}
        ex = _executor(metas)
        plan = ExecutionPlan(pipeline_name="p", levels=[[a], [b]], raw={})
        assert ex._condition_context(task, b, plan)["inputs"] == {"min_score": 70}

    def test_context_exposes_artifacts_config_and_pipeline(self):
        a = _node("quality_gate")
        b = _node("renderer", deps=["quality_gate"], config={"format": "docx"})
        task = _task({"quality_gate": {"content": "正文"}, "renderer": None},
                     {"quality_gate": a, "renderer": b})
        metas = {"quality_gate": AgentMeta(name="quality_gate", produces={"content": "last"}),
                 "renderer": META_RENDER}
        ex = _executor(metas)
        plan = ExecutionPlan(pipeline_name="p", plan_id="p1", levels=[[a], [b]], raw={})

        ctx = ex._condition_context(task, b, plan)
        assert ctx["artifacts"]["content"] == "正文"
        assert ctx["config"]["format"] == "docx"
        assert ctx["pipeline"] == "p"
        assert ctx["task"]["id"] == "t-cond"

    def _aliased_ctx(self, upstream_names):
        """上游串成一条链（都带别名），取回下游节点 when 的求值上下文。"""
        from pipeline_core.naming import agent_of

        chain: list[ExecutionNode] = []
        prev = None
        for name in upstream_names:
            node = _node(name, deps=[prev] if prev else [])
            chain.append(node)
            prev = name
        target = _node("layout__tail", deps=[prev])
        results = {n.agent_name: {"overall_score": 82, "content": "正文"} for n in chain}
        results[target.agent_name] = None
        node_objs = {n.agent_name: n for n in chain + [target]}
        task = _task(results, node_objs)
        metas = {"quality_gate": META_GATE, "writer": META_GATE,
                 "layout": AgentMeta(name="layout", produces={"content": "last"})}
        ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        # 注册表按 Agent 名寻址（与真实 executor 一致），别名名要还原后再查
        ex.registry.get_meta.side_effect = lambda name: metas.get(agent_of(name))
        plan = ExecutionPlan(pipeline_name="p", levels=[chain, [target]], raw={})
        return ex._condition_context(task, target, plan)

    def test_unique_aliased_upstream_is_addressable_by_agent_name(self):
        """片段里写 `upstream.quality_gate.*` 的人不知道自己被改名叫什么。

        docgen-verified 实跑能过就是靠这条：闭包键是 quality_gate__quality_tail，
        条件路径却是 upstream.quality_gate.overall_score。
        """
        ctx = self._aliased_ctx(["writer", "quality_gate__quality_tail"])
        assert ctx["upstream"]["quality_gate"]["overall_score"] == 82
        assert ctx["upstream"]["quality_gate__quality_tail"]["overall_score"] == 82

    def test_ambiguous_aliased_upstream_gets_no_agent_key(self):
        """同一个 Agent 出现两次时不补名：宁可让条件抛"取不到值"，也不猜一份结果。"""
        ctx = self._aliased_ctx(["quality_gate__a", "quality_gate__b"])
        assert "quality_gate" not in ctx["upstream"], sorted(ctx["upstream"])
        assert ctx["upstream"]["quality_gate__a"]["overall_score"] == 82


class TestSchedulerPlanning:
    def _plan_with_when(self, tmp_path, when_yaml):
        src = (PROJECT / "pipelines" / "test_pipeline.yaml").read_text(encoding="utf-8")
        # 给 renderer 位置不合适（test_pipeline 没有），改挂到 layout 节点上
        patched = src.replace(
            "  - name: layout\n    version: \"2.0\"",
            f"  - name: layout\n    when:\n{when_yaml}\n    version: \"2.0\"",
            1,
        )
        assert patched != src, "锚点没替换成功，测试会变成空转"
        f = tmp_path / "cond_pipeline.yaml"
        f.write_text(patched, encoding="utf-8")
        return f

    def test_valid_when_parses_into_node(self, tmp_path):
        path = self._plan_with_when(tmp_path, '      path: doc.format\n      op: "=="\n      value: docx\n')
        plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(tmp_path)).parse_file(str(path), verify_lock=False)
        layout = [n for lvl in plan.levels for n in lvl if n.agent_name == "layout"][0]
        assert layout.when == {"path": "doc.format", "op": "==", "value": "docx"}

    def test_invalid_when_raises_at_parse_time(self, tmp_path):
        path = self._plan_with_when(tmp_path, '      path: doc.format\n      op: "gte"\n      value: docx\n')
        with pytest.raises(ValueError, match="when 条件非法"):
            Scheduler(agents_dir=str(PROJECT / "agents"),
                      pipeline_dir=str(tmp_path)).parse_file(str(path), verify_lock=False)

    def test_condition_changes_topology_fingerprint(self, tmp_path):
        """加了条件却不动锁文件，等于绕开漂移护栏。"""
        plain = Scheduler(agents_dir=str(PROJECT / "agents"),
                          pipeline_dir=str(PROJECT / "pipelines")).parse(
            "test_pipeline", verify_lock=False)
        cond = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(tmp_path)).parse_file(
            str(self._plan_with_when(tmp_path, '      path: doc.format\n      op: "=="\n      value: docx\n')),
            verify_lock=False)
        assert Scheduler._topology_hash(plain) != Scheduler._topology_hash(cond)

    def test_lockfile_rejects_added_condition(self, tmp_path):
        """改了条件 → 与既有 lock 不一致 → 拒绝执行。"""
        sched = Scheduler(agents_dir=str(PROJECT / "agents"),
                          pipeline_dir=str(PROJECT / "pipelines"))
        plan = sched.parse("test_pipeline", verify_lock=False)
        lock_dir = tmp_path / "pipelines"
        lock_dir.mkdir()
        sched.generate_lockfile(plan, output_dir=str(lock_dir))
        for lvl in plan.levels:
            for n in lvl:
                if n.agent_name == "layout":
                    n.when = {"path": "doc.format", "op": "==", "value": "docx"}
        issues = sched.verify_lockfile(plan, lockfile=str(lock_dir / "test_pipeline.lock"))
        assert any("拓扑漂移" in i for i in issues), issues

    def test_shipped_pipelines_keep_their_hashes(self):
        """退出门：现有流水线的指纹不能因为这次改动而变。

        逐条 `verify_lock=True` 就是证明——lockfile 里存的是改动前生成的指纹，
        哈希一变就报拓扑漂移。用 glob 取清单，避免新增流水线时这条测试静默漏掉它。
        """
        sched = Scheduler(agents_dir=str(PROJECT / "agents"),
                          pipeline_dir=str(PROJECT / "pipelines"))
        names = sorted(p.stem for p in (PROJECT / "pipelines").glob("*.yaml"))
        assert "docgen-lean" in names          # 带 when 的那条也在这批里
        for name in names:
            assert sched.parse(name, verify_lock=True).node_count > 0


class TestDeliveryContractWithConditions:
    def test_sink_skipped_by_condition_is_not_a_failure(self, tmp_path, monkeypatch):
        from pipeline_core import PipelineOrchestrator

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(PROJECT / "pipelines")).parse(
            "test_pipeline", verify_lock=False)
        for lvl in plan.levels:
            for n in lvl:
                if n.agent_name == "safe_writer":
                    n.when = {"path": "config.export", "op": "==", "value": True}

        task = PipelineTask(id="t-sink-cond", pipeline_name="test_pipeline",
                            input_file="in.md", config={})
        for name in ("safe_writer",):
            dn = TaskNode(name=name, agent_name=name, dependencies=[])
            dn.status = "skipped"
            dn.skip_reason = "condition"
            task.dag_nodes[name] = dn

        assert orch._sink_declared(plan)
        assert orch._delivered(task, plan) is True, "落盘节点被条件主动跳过应视为按声明无交付"
        orch.shutdown()

    def test_sink_skipped_by_failure_is_still_a_failure(self, tmp_path):
        from pipeline_core import PipelineOrchestrator

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck2"))
        orch.register_agents()
        plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(PROJECT / "pipelines")).parse(
            "test_pipeline", verify_lock=False)
        task = PipelineTask(id="t-sink-fail", pipeline_name="test_pipeline",
                            input_file="in.md", config={})
        dn = TaskNode(name="safe_writer", agent_name="safe_writer", dependencies=[])
        dn.status = "skipped"          # 没有 skip_reason：依赖失败的级联跳过
        task.dag_nodes["safe_writer"] = dn

        assert orch._delivered(task, plan) is False, (
            "依赖失败导致落盘节点没跑，仍然没有交付物——这正是 #13 关掉的洞")
        orch.shutdown()


class TestShippedConsumer:
    """`when` 在出厂流水线里有真实消费者：质量尾片段的 fact_checker（docgen-lean 引用）。

    写这一组的理由是本项目反复踩过的同一类错——"实现正确但没被接线"。
    语言与接线都测过之后，如果没有任何出厂配置用它，能力就等于零。

    抽取之后节点身份带别名（fact_checker__quality_tail），所以这里一律按
    Agent 名查，不断言字面节点名——否则每次改调用方名字都要来动行为测试。
    """

    def _plan(self, name="docgen-lean"):
        return Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(PROJECT / "pipelines")).parse(
            name, verify_lock=True)

    def _find(self, plan, agent: str):
        hits = [n for level in plan.levels for n in level
                if agent_of(n.agent_name) == agent]
        assert len(hits) == 1, f"{agent} 在计划里出现 {len(hits)} 次: {[n.agent_name for n in hits]}"
        return hits[0]

    def _task_for(self, plan, score: float) -> PipelineTask:
        task = PipelineTask(id="t-lean", pipeline_name=plan.pipeline_name,
                            input_file="in.md", config={})
        for level in plan.levels:
            for node in level:
                dn = TaskNode(name=node.agent_name, agent_name=node.agent_name,
                              dependencies=list(node.dependencies))
                dn.status = "pending"
                task.dag_nodes[node.agent_name] = dn
        gate = self._find(plan, "quality_gate")
        gate_node = task.dag_nodes[gate.agent_name]
        gate_node.status = "success"
        gate_node.result = {"overall_score": score, "status": "pass"}
        checker = task.dag_nodes[self._find(plan, "checker").agent_name]
        checker.status = "success"
        checker.result = {"issues": []}
        return task

    def test_shipped_plan_carries_the_condition(self):
        plan = self._plan()
        fc = self._find(plan, "fact_checker")
        assert fc.when == {"all": [
            {"path": "inputs.fact_check", "op": "==", "value": True},
            {"path": "upstream.quality_gate.overall_score", "op": ">=",
             "value_from": "inputs.min_score"},
        ]}, "出厂消费者的条件形态变了，判据要跟着改（这条断言就是为了逼我改）"
        # 阈值来自调用方实参，不再抄死在条件里
        assert fc.inputs["min_score"] == 70, fc.inputs

    def test_low_score_skips_fact_checker_but_layout_still_runs(self):
        plan = self._plan()
        fc, layout = self._find(plan, "fact_checker"), self._find(plan, "layout")
        task = self._task_for(plan, score=61.0)
        metas = {"fact_checker": AgentMeta(name="fact_checker", produces={"verdict": "last"}),
                 "layout": AgentMeta(name="layout", produces={"content": "last"})}
        ex = _executor(metas)

        assert ex._submit_level_futures(task, [fc], "in.md", plan, MagicMock()) == {}
        assert task.dag_nodes[fc.agent_name].skip_reason == "condition"

        futures = ex._submit_level_futures(task, [layout], "in.md", plan, MagicMock())
        assert len(futures) == 1, "被条件跳过的可选分支不该让下游停摆"

    def test_high_score_runs_the_escalated_check(self):
        plan = self._plan()
        fc = self._find(plan, "fact_checker")
        task = self._task_for(plan, score=85.0)
        ex = _executor({"fact_checker": AgentMeta(name="fact_checker", produces={"verdict": "last"})})
        assert len(ex._submit_level_futures(task, [fc], "in.md", plan, MagicMock())) == 1

    def test_threshold_actually_comes_from_the_caller(self):
        """同一份片段，调用方把 min_score 改成 90，85 分就不该核查。

        这条是"参数化生效"的直接判据：把 docgen-verified（min_score: 0）拿来跑
        同一个 85 分，结论必须相反。
        """
        plan = self._plan("docgen-verified")
        fc = self._find(plan, "fact_checker")
        assert fc.inputs["min_score"] == 0, fc.inputs
        task = self._task_for(plan, score=85.0)
        ex = _executor({"fact_checker": AgentMeta(name="fact_checker", produces={"verdict": "last"})})
        assert len(ex._submit_level_futures(task, [fc], "in.md", plan, MagicMock())) == 1

    def test_fact_check_false_short_circuits_the_branch(self):
        """docgen 只要质量尾、不要核查：fact_check=false 时分数再高也不跑。"""
        plan = self._plan("docgen")
        fc = self._find(plan, "fact_checker")
        assert fc.inputs["fact_check"] is False, fc.inputs
        task = self._task_for(plan, score=99.0)
        ex = _executor({"fact_checker": AgentMeta(name="fact_checker", produces={"verdict": "last"})})
        assert ex._submit_level_futures(task, [fc], "in.md", plan, MagicMock()) == {}
        assert task.dag_nodes[fc.agent_name].skip_reason == "condition"


class TestStepResultCompatibility:
    def test_skipped_step_status_is_documented(self):
        """StepResult 的 status 取值集合里 skipped 是合法值（报表/审计依赖它）。"""
        s = StepResult(step_name="renderer", agent_name="renderer", status="skipped",
                       started_at=0.0)
        assert s.status == "skipped"

    def test_task_status_enum_untouched(self):
        assert TaskStatus.DONE.value == "done"
