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

    def test_pipelines_without_when_keep_their_hashes(self):
        """退出门：7 条现有流水线的指纹不能因为这次改动而变。"""
        sched = Scheduler(agents_dir=str(PROJECT / "agents"),
                          pipeline_dir=str(PROJECT / "pipelines"))
        for name in ("docgen", "docgen-render", "docgen-verified", "docreq",
                     "kb-docgen", "test_pipeline", "three_pass"):
            plan = sched.parse(name, verify_lock=True)   # 校验锁文件即含指纹比对
            assert plan.node_count > 0


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


class TestStepResultCompatibility:
    def test_skipped_step_status_is_documented(self):
        """StepResult 的 status 取值集合里 skipped 是合法值（报表/审计依赖它）。"""
        s = StepResult(step_name="renderer", agent_name="renderer", status="skipped",
                       started_at=0.0)
        assert s.status == "skipped"

    def test_task_status_enum_untouched(self):
        assert TaskStatus.DONE.value == "done"
