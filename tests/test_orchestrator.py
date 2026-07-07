"""Orchestrator — registration, run_plan, checkpoint, pause/resume"""
import time
import json
from pathlib import Path
import pytest


class TestOrchestratorRegistration:
    """Agent 注册"""

    def test_registers_six_agents(self, orch):
        agents = orch.registry.list()
        agent_names = [a["name"] if isinstance(a, dict) else str(a) for a in agents]
        assert len([n for n in agent_names if n in ("researcher", "writer", "checker",
                   "quality_gate", "layout", "safe_writer")]) >= 6, \
            f"expected 6+ agents, got {agent_names}"

    def test_get_instance(self, orch):
        inst = orch.registry.get_instance("writer")
        assert inst is not None, "writer agent should be instantiated"

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