"""Mock 端到端测试 — 完整 docgen 流水线（无需真实 LLM/搜索 Key）。

验证核心编排路径：DAG 构建 → 节点调度 → Agent 执行 → 质量门控 → 输出。
所有外部依赖（LLM/搜索/网络）均 mock，CI 默认运行（不加 -m e2e）。
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

PROJECT = Path(__file__).parent.parent


def _mock_search_result(title="T", url="https://example.com/x", snippet="snippet"):
    from pipeline_core.search_engines import SearchItem
    return SearchItem(title=title, url=url, snippet=snippet, source="mock", query="q")


def _mock_writer_handle():
    """mock writer.handle 返回结构化内容。"""
    def _handle(self, msg):
        return {
            "status": "ok",
            "content": "# 测试文档\n\n## 简介\n\n这是 mock 生成的文档内容，长度足够。\n\n"
                       "## 核心概念\n\n正文内容。\n\n## 实践应用\n\n正文内容。\n\n"
                       "## 总结\n\n总结内容。\n\n## 参考资料\n\n- [ref](https://example.com)\n",
            "stats": {"empty_sections": [], "total_words": 100},
        }
    return _handle


def _mock_quality_gate_handle():
    """mock quality_gate.handle 返回通过。"""
    def _handle(self, msg):
        return {"status": "pass", "overall_score": 85, "scores": {"completeness": 80}}
    return _handle


class TestMockE2E:
    """Mock E2E: 完整 docgen 流水线。"""

    def test_full_docgen_pipeline_with_mocks(self, tmp_path):
        """验证 DAG 构建 → 节点执行 → 质量门控 → 输出的完整路径。"""
        input_file = tmp_path / "input.md"
        input_file.write_text("Python 异步编程的基本概念和用法\n", encoding="utf-8")

        mock_results = [_mock_search_result(f"Result {i}") for i in range(3)]

        with patch("pipeline_core.search_engines.SearchEngineManager.from_env") as mock_mgr, \
             patch("agents.writer.WriterAgent.handle", _mock_writer_handle()), \
             patch("agents.quality_gate.QualityGateAgent.handle", _mock_quality_gate_handle()):

            mock_mgr.return_value.is_available.return_value = True
            mock_mgr.return_value.search_with_sites.return_value = mock_results
            mock_mgr.return_value.search.return_value = mock_results

            from pipeline_core import PipelineOrchestrator
            orch = PipelineOrchestrator(
                agents_dir=str(PROJECT / "agents"),
                checkpoint_dir=str(tmp_path / "checkpoints"),
            )
            orch.register_agents()

            from pipeline_core.scheduler import Scheduler
            sched = Scheduler()
            plan = sched.parse_file(str(PROJECT / "pipelines" / "test_pipeline.yaml"))

            task = orch.run_plan(plan, input_file=str(input_file), wait=True)

            assert task.status.value == "done"
            assert task.result is not None
            assert "writer" in task.result

    def test_dag_builds_correct_levels(self):
        """验证 test_pipeline.yaml 的 DAG 层级正确。"""
        from pipeline_core.scheduler import Scheduler
        sched = Scheduler()
        plan = sched.parse_file(str(PROJECT / "pipelines" / "test_pipeline.yaml"))

        assert plan.node_count > 0
        assert len(plan.levels) >= 3

        first_level_agents = {n.agent_name for n in plan.levels[0]}
        assert "researcher" in first_level_agents

    def test_checkpoint_save_and_load(self, tmp_path):
        """验证断点保存 → 加载 → 恢复。"""
        from pipeline_core import PipelineOrchestrator
        from pipeline_core.pipeline import PipelineTask, TaskStatus

        orch = PipelineOrchestrator(
            agents_dir=str(PROJECT / "agents"),
            checkpoint_dir=str(tmp_path / "checkpoints"),
        )

        task = PipelineTask(id="ckpt-test", pipeline_name="docgen",
                            input_file="in.md", config={})
        task.status = TaskStatus.PAUSED
        task.result = {"writer": {"content": "partial"}}
        task.checkpoint_file = str(tmp_path / "checkpoints" / "ckpt-test.json")

        orch._save_checkpoint(task, full_state=True)

        loaded = orch._load_checkpoint("ckpt-test")
        assert loaded is not None
        assert loaded.id == "ckpt-test"
        assert loaded.result["writer"]["content"] == "partial"

    def test_task_cancellation(self):
        """验证任务取消信号传播。"""
        from pipeline_core import PipelineOrchestrator
        from pipeline_core.pipeline import PipelineTask, TaskStatus

        orch = PipelineOrchestrator(
            agents_dir=str(PROJECT / "agents"),
            checkpoint_dir=str(Path(tempfile.mkdtemp()) / "checkpoints"),
        )

        task = PipelineTask(id="cancel-test", pipeline_name="docgen",
                            input_file="in.md", config={})
        task.status = TaskStatus.RUNNING
        orch._running_tasks["cancel-test"] = task

        ok = orch.cancel("cancel-test")
        assert ok
        assert task.status == TaskStatus.CANCELLED
        assert task.stop_event.is_set()

    def test_rate_limiter_allows_when_unconfigured(self):
        """验证无限流配置时直接放行。"""
        from pipeline_core.dag_executor import DAGExecutor
        ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        assert ex._acquire_rate_limit("any", {}) is True
        assert ex._acquire_rate_limit("any", {"rate": 0}) is True
