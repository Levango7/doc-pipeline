"""P0-3 recover_tasks 内存状态同步测试。

队列侧（SQLite）running → pending 的同时，_running_tasks 中同 id 的
RUNNING 任务必须同步置为 PENDING，避免 get_task/list_tasks 返回旧状态。
PAUSED/DONE 等其他状态不动。
"""
import threading

from pipeline_core.pipeline import PipelineOrchestrator, PipelineTask, TaskStatus
from pipeline_core.task_queue import TaskQueue


def _make_orch(tmp_path) -> PipelineOrchestrator:
    """最小 orchestrator 骨架：recover_tasks 只依赖 task_queue/_running_tasks/_lock。"""
    orch = PipelineOrchestrator.__new__(PipelineOrchestrator)
    orch._running_tasks = {}
    orch._lock = threading.RLock()
    orch.task_queue = TaskQueue(str(tmp_path / "tasks.db"))
    return orch


class TestRecoverSyncsMemory:
    def test_running_task_synced_to_pending(self, tmp_path):
        orch = _make_orch(tmp_path)
        q = orch.task_queue
        q.submit("t1", "docgen", "in.md", {"k": 1})
        assert q.acquire("w1")["task_id"] == "t1"
        assert q.get("t1")["status"] == "running"

        mem = PipelineTask(id="t1", pipeline_name="docgen", input_file="in.md", config={})
        mem.status = TaskStatus.RUNNING
        orch._running_tasks["t1"] = mem

        recovered = orch.recover_tasks()

        assert [r["task_id"] for r in recovered] == ["t1"]
        assert q.get("t1")["status"] == "pending"
        assert mem.status == TaskStatus.PENDING
        q.close()

    def test_paused_memory_state_untouched(self, tmp_path):
        orch = _make_orch(tmp_path)
        q = orch.task_queue
        q.submit("t2", "docgen", "in.md")
        q.acquire("w1")

        mem = PipelineTask(id="t2", pipeline_name="docgen", input_file="in.md", config={})
        mem.status = TaskStatus.PAUSED
        orch._running_tasks["t2"] = mem

        orch.recover_tasks()

        assert q.get("t2")["status"] == "pending"  # 队列侧照常恢复
        assert mem.status == TaskStatus.PAUSED  # 内存侧 PAUSED 不动
        q.close()

    def test_empty_recover_leaves_memory_alone(self, tmp_path):
        orch = _make_orch(tmp_path)
        mem = PipelineTask(id="t3", pipeline_name="docgen", input_file="in.md", config={})
        mem.status = TaskStatus.DONE
        orch._running_tasks["t3"] = mem

        assert orch.recover_tasks() == []
        assert mem.status == TaskStatus.DONE
        orch.task_queue.close()
