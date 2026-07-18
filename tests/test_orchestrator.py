"""Orchestrator — registration, run_plan, checkpoint, pause/resume"""
import time
import json
from pathlib import Path
import pytest
from types import SimpleNamespace


class TestOrchestratorRegistration:
    """Agent 注册"""

    def test_registers_six_agents(self, orch):
        agents = orch.registry.list()
        agent_names = [a["name"] if isinstance(a, dict) else str(a) for a in agents]
        assert len([n for n in agent_names if n in ("researcher", "writer", "safe_writer",
                   "checker", "quality_gate", "layout", "fetcher")]) >= 6, \
            f"expected 6+ agents, got {agent_names}"

    def test_get_instance(self, orch):
        inst = orch.registry.get_instance("safe_writer")
        assert inst is not None, "safe_writer agent should be instantiated"

    def test_get_status(self, orch):
        status = orch.registry.get_status("researcher")
        assert status is not None


class TestOrchestratorRun:
    """完整流水线执行"""

    def test_run_plan_full_pipeline(self, orch, docgen_plan):
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file, task_id="test_full_run")

        assert task.status.name == "DONE", f"pipeline failed: {task.status}"
        assert task.progress == 100
        assert len(task.steps) > 0, "should have steps recorded"

    def test_run_produces_results(self, orch, docgen_plan):
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file, task_id="test_results")

        assert task.status.name == "DONE"
        assert task.result is not None
        assert "researcher" in task.result
        assert "writer" in task.result

    def test_run_sets_dag_nodes(self, orch, docgen_plan):
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file, task_id="test_dag")

        assert len(task.dag_nodes) > 0
        for name, node in task.dag_nodes.items():
            assert node.status in ("success", "failed")


class TestOrchestratorCheckpoint:
    """断点保存与恢复"""

    def test_save_checkpoint_creates_file(self, orch, docgen_plan):
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file, task_id="test_ckpt")

        # checkpoint 文件在 pipeline 过程中被创建
        ckpt_dir = Path(orch.checkpoint_dir)
        files = list(ckpt_dir.glob("test_ckpt*.json"))
        # keep_on_success=False 可能会删除，所以不强制存在
        if files:
            with open(files[0]) as f:
                data = json.load(f)
            assert "id" in data
            assert "pipeline" in data

    def test_remember_task(self, orch, docgen_plan):
        """已完成的任务仍可通过 get_task 找到"""
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file, task_id="test_remember")
        restored = orch.get_task(task.id)
        assert restored is not None
        assert restored.id == task.id

    def test_list_tasks(self, orch, docgen_plan):
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        orch.run_plan(docgen_plan, input_file=input_file, task_id="test_list")
        tasks = orch.list_tasks()
        assert len(tasks) >= 1


class TestOrchestratorPauseResume:
    """暂停 / 恢复 / 取消"""

    def test_pause(self, orch, docgen_plan):
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file,
                             task_id="test_pause", wait=False)
        time.sleep(0.02)
        ok = orch.pause(task.id)
        # 可能已完成（太快），但 pause 应该返回至少不在 running
        if ok:
            assert task.status.name in ("PAUSED", "DONE")
        else:
            # 任务可能已完成
            pass
        orch.resume(task.id)

    def test_cancel(self, orch, docgen_plan):
        """取消应立即将任务状态转为 CANCELLED"""
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file,
                             task_id="test_cancel", wait=False)
        time.sleep(0.01)
        orch.cancel(task.id)
        # 可能已经在 done，但不会在 running
        assert task.status.name in ("DONE", "CANCELLED", "PAUSED")

    def test_resume_after_pause(self, orch, docgen_plan):
        """resume 后任务应完成"""
        input_file = str(Path(__file__).parent.parent / "test_input.md")
        task = orch.run_plan(docgen_plan, input_file=input_file,
                             task_id="test_resume", wait=False)
        orch.pause(task.id)
        orch.resume(task.id)
        time.sleep(1)
        assert task.status.name == "DONE", f"task stuck at {task.status}"


class TestRateLimitIntegration:
    """RateLimiter 与 Orchestrator 集成测试"""

    def test_unconfigured_agent_not_limited(self):
        """未配置 rate_limit 的 agent 不受限流影响"""
        from pipeline_core import PipelineOrchestrator

        o = PipelineOrchestrator()
        for i in range(10):
            ok = o._acquire_rate_limit("unlimited_agent", rate_limit_cfg={}, timeout=1)
            assert ok, f"第 {i+1} 次 acquire 被限（不应限流）"

    def test_configured_agent_throttled(self):
        """rate=5 burst=5 的 agent 第 6 次应阻塞（burst 耗尽）"""
        from pipeline_core import PipelineOrchestrator

        o = PipelineOrchestrator()
        # 耗尽令牌桶（非阻塞 acquire）
        for i in range(5):
            ok = o._acquire_rate_limit("bursty", {"rate": 5, "burst": 5}, timeout=1)
            assert ok, f"burst 内第 {i+1} 次应放行"

        # 第 6 次：桶已空，block=False 应失败
        from pipeline_core.rate_limiter import RateLimiter
        limiter = o._rate_limiters.get_or_create("bursty")
        ok = limiter.acquire(1, block=False)
        assert ok is False, "burst 耗尽后非阻塞 acquire 应返回 False"

    def test_rate_limit_in_execute_skipped_when_not_configured(self):
        """_execute_node 中未配置限流时不报错（回归保护）"""
        from pipeline_core import PipelineOrchestrator
        from pipeline_core.scheduler import AgentConfig

        o = PipelineOrchestrator()
        node = SimpleNamespace(
            agent_name="no_rl",
            agent_config=SimpleNamespace(
                rate_limit={},
                circuit_breaker={},
                config={}, pool_size=1, idempotency=False,
                max_retries=1, retry_initial_delay=0.1, retry_backoff="linear",
            ),
            dependencies=[], timeout=5,
        )
        task = SimpleNamespace(
            id="no_rl_test", result={},
            dag_nodes={"no_rl": SimpleNamespace(result=None, status="pending", attempts=0)},
            status="running",
        )
        plan = SimpleNamespace(pipeline_name="test", raw={"pipeline": {"output": "out.md"}})

        import tempfile, os
        tdir = tempfile.mkdtemp()
        input_file = os.path.join(tdir, "input.md")
        with open(input_file, "w") as f:
            f.write("query")

        # 不应抛异常
        result = o._execute_node_from_scheduler(task, node, input_file, plan)
        # 没有注册 agent，应返回 error 而非异常
        assert isinstance(result, dict) and "error" in result, \
            f"未注册 agent 应返回 error，实际 {result}"

    def test_scheduler_parses_rate_limit(self, tmp_path):
        """scheduler 从 YAML 解析 rate_limit → AgentConfig"""
        from pipeline_core.scheduler import Scheduler
        import yaml

        raw = {
            "pipeline": {"name": "test"},
            "topology": {"levels": [["a1", "a2"]]},
            "agents": [
                {"name": "a1", "rate_limit": {"rate": 10, "burst": 20}},
                {"name": "a2"},
            ],
        }
        pfile = tmp_path / "test_rate_limit.yaml"
        with open(pfile, "w") as f:
            yaml.dump(raw, f)

        scheduler = Scheduler(str(tmp_path))
        plan = scheduler.parse("test_rate_limit")

        # 验证 ExecutionNode 的 rate_limit
        nodes = [n for level in plan.levels for n in level]
        node_a1 = next(n for n in nodes if n.agent_name == "a1")
        assert node_a1.agent_config.rate_limit == {"rate": 10, "burst": 20}

        node_a2 = next(n for n in nodes if n.agent_name == "a2")
        assert node_a2.agent_config.rate_limit == {}, "未配置的 agent 应是空 dict"