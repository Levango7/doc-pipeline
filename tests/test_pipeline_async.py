"""测试 PipelineOrchestrator.run_plan_async —— async 编排验证"""
import inspect
import pytest
from pathlib import Path

from pipeline_core.pipeline import PipelineOrchestrator, TaskStatus


@pytest.fixture
def orchestrator(tmp_path):
    """创建编排器"""
    orch = PipelineOrchestrator(
        agents_dir=str(Path(__file__).parent.parent / "agents"),
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )
    return orch


class TestRunPlanAsync:
    """run_plan_async 基本功能测试"""

    def test_run_plan_async_exists(self, orchestrator):
        """run_plan_async 方法存在且是 coroutine function"""
        assert hasattr(orchestrator, "run_plan_async")
        assert inspect.iscoroutinefunction(orchestrator.run_plan_async)

    def test_execute_level_async_exists(self, orchestrator):
        """DAGExecutor.execute_level_async 方法存在且是 coroutine function"""
        assert hasattr(orchestrator._executor, "execute_level_async")
        assert inspect.iscoroutinefunction(orchestrator._executor.execute_level_async)

    @pytest.mark.asyncio
    async def test_run_plan_async_with_empty_plan(self, orchestrator, tmp_path):
        """空 plan 的 run_plan_async 应正常完成"""
        from pipeline_core.scheduler import ExecutionPlan

        plan = ExecutionPlan(
            pipeline_name="test_async",
            plan_id="test_001",
            levels=[],
            raw={"pipeline": {}},
            checkpoint={},
        )
        plan.node_count = 0

        input_file = str(tmp_path / "input.md")
        Path(input_file).write_text("test content", encoding="utf-8")

        task = await orchestrator.run_plan_async(plan, input_file=input_file)
        assert task.status == TaskStatus.DONE
        assert task.progress == 100
