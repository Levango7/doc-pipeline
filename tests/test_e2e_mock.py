"""Mock 端到端测试 — 完整 docgen 流水线（无需真实 LLM/搜索 Key）。

验证核心编排路径：DAG 构建 → 节点调度 → Agent 执行 → 质量门控 → 输出。
所有外部依赖（LLM/搜索/网络）均 mock，CI 默认运行（不加 -m e2e）。
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        """验证 DAG 构建 → 节点执行 → 质量门控 → 输出的完整路径。

        mock 必须在 `register_agents()` **之后**打：agent_loader 用
        spec_from_file_location 重新加载 agents/*.py 并覆写
        `sys.modules["agents.<name>"]`，注册前拿到的类不是实例化用的那个类，
        补丁会全程空转（本文件此前正是如此，靠弱断言"writer in task.result"蒙过）。
        因此这里断言 mock 的哨兵字符串真的出现在结果里——判据本身必须能被命中。
        """
        input_file = tmp_path / "input.md"
        input_file.write_text("Python 异步编程的基本概念和用法\n", encoding="utf-8")

        mock_results = [_mock_search_result(f"Result {i}") for i in range(3)]

        from pipeline_core import PipelineOrchestrator
        from pipeline_core.scheduler import Scheduler

        orch = PipelineOrchestrator(
            agents_dir=str(PROJECT / "agents"),
            checkpoint_dir=str(tmp_path / "checkpoints"),
        )
        orch.register_agents()

        # 取加载器真正使用的类对象（sys.modules 已被 loader 覆写）
        writer_cls = sys.modules["agents.writer"].WriterAgent
        gate_cls = sys.modules["agents.quality_gate"].QualityGateAgent

        with patch("pipeline_core.search_engines.SearchEngineManager.from_env") as mock_mgr, \
             patch.object(writer_cls, "handle", _mock_writer_handle()), \
             patch.object(gate_cls, "handle", _mock_quality_gate_handle()):

            mock_mgr.return_value.is_available.return_value = True
            mock_mgr.return_value.search_with_sites.return_value = mock_results
            mock_mgr.return_value.search.return_value = mock_results

            sched = Scheduler()
            plan = sched.parse_file(str(PROJECT / "pipelines" / "test_pipeline.yaml"))

            try:
                task = orch.run_plan(plan, input_file=str(input_file), wait=True)

                assert task.status.value == "done", f"{task.status.value}: {task.error}"
                assert task.result is not None
                assert "writer" in task.result
                content = str(task.result["writer"].get("content", ""))
                assert "mock 生成的文档内容" in content, (
                    f"writer mock 没被命中，说明补丁打在了错误的类对象上: {content[:120]!r}")
            finally:
                orch.shutdown()

    def test_delivery_contract_sink_failure_cannot_report_done(self, tmp_path, monkeypatch):
        """交付契约：声明了落盘节点却没交付物，就不能报 done。

        实测缺陷：kb-docgen 摄入被跳过、knowledge_base 失败，writer 之后从未执行，
        而 `fail_fast: false` 让末端节点保持 RUNNING，收尾却无条件盖 DONE，
        于是 exit 0 且没有任何产物文件。

        这里直接封 `_delivered` 返回 False 来走同一条收尾判定——不能用
        `patch("agents.safe_writer_agent.SafeWriterAgent.handle")`：agent_loader
        会以 `spec_from_file_location` 重新加载模块并覆写 `sys.modules["agents.*"]`，
        注册期之前拿到的类根本不是实例化用的那个类。
        """
        input_file = tmp_path / "input.md"
        input_file.write_text("Python 异步编程的基本概念和用法\n", encoding="utf-8")

        from pipeline_core import PipelineOrchestrator
        from pipeline_core.scheduler import Scheduler

        monkeypatch.setattr(PipelineOrchestrator, "_delivered", staticmethod(lambda task: False))
        orch = PipelineOrchestrator(
            agents_dir=str(PROJECT / "agents"),
            checkpoint_dir=str(tmp_path / "checkpoints"),
        )
        orch.register_agents()
        plan = Scheduler().parse_file(str(PROJECT / "pipelines" / "test_pipeline.yaml"))
        plan.fail_fast = False  # 复现 kb-docgen 的配置：软失败不中断
        try:
            assert orch._sink_declared(plan), "test_pipeline 应以 safe_writer 为落盘节点"
            task = orch.run_plan(plan, input_file=str(input_file), wait=True)
            assert task.status.value == "failed", (
                f"没有交付物却报 {task.status.value}: {task.error}")
            assert "交付" in (task.error or ""), task.error
        finally:
            orch.shutdown()

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
