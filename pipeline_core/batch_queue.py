"""
BatchQueue - 批量文档生成队列
================================
支持批量提交多个文档生成任务，按优先级/顺序调度执行。

核心特性：
  - 内存任务队列 + SQLite 持久化（可选）
  - 优先级调度（HIGH > NORMAL > LOW）
  - 并发控制（max_concurrent 限制同时执行的任务数）
  - 任务状态追踪（pending/running/done/failed/cancelled）
  - 进度回调 + 完成通知
  - 线程安全

用法：
    from pipeline_core.batch_queue import BatchQueue

    bq = BatchQueue(max_concurrent=2)
    bq.submit("topic1.md", output="out1.md", priority=10)
    bq.submit("topic2.md", output="out2.md", priority=50)
    bq.start()
    bq.wait_all(timeout=600)
    print(bq.summary())
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor, as_completed


class BatchTaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BatchTask:
    """批量队列中的单个任务"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    input_file: str = ""
    output: str = ""
    pipeline: str = "docgen"
    config: dict = field(default_factory=dict)
    priority: int = 50          # 数字越小优先级越高
    status: BatchTaskStatus = BatchTaskStatus.PENDING
    progress: int = 0
    error: str = ""
    result: dict = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "input_file": self.input_file,
            "output": self.output,
            "pipeline": self.pipeline,
            "priority": self.priority,
            "status": self.status.value,
            "progress": self.progress,
            "error": self.error,
            "duration": round(self.duration, 2),
        }


class BatchQueue:
    """批量文档生成队列

    Args:
        max_concurrent: 最大并发执行任务数
        on_task_done: 单任务完成回调 (task) -> None
        on_all_done: 全部完成回调 (results: list[BatchTask]) -> None
    """

    def __init__(self, max_concurrent: int = 2,
                 on_task_done: Optional[Callable[[BatchTask], None]] = None,
                 on_all_done: Optional[Callable[[list], None]] = None):
        self.max_concurrent = max_concurrent
        self._on_task_done = on_task_done
        self._on_all_done = on_all_done

        self._queue: list[BatchTask] = []
        self._lock = threading.RLock()
        self._running = False
        self._executor: Optional[ThreadPoolExecutor] = None
        self._stop_event = threading.Event()

    def submit(self, input_file: str, output: str = "",
               pipeline: str = "docgen", config: dict = None,
               priority: int = 50, task_id: str = "") -> BatchTask:
        """提交一个文档生成任务到队列"""
        task = BatchTask(
            id=task_id or str(uuid.uuid4())[:8],
            input_file=input_file,
            output=output or f"output/{task_id or 'batch'}_result.md",
            pipeline=pipeline,
            config=config or {},
            priority=priority,
        )
        with self._lock:
            self._queue.append(task)
            # 按优先级排序
            self._queue.sort(key=lambda t: t.priority)
        return task

    def submit_many(self, items: list[dict]) -> list[BatchTask]:
        """批量提交多个任务

        Args:
            items: [{"input_file": "...", "output": "...", "priority": 10}, ...]
        """
        tasks = []
        for item in items:
            t = self.submit(
                input_file=item.get("input_file", ""),
                output=item.get("output", ""),
                pipeline=item.get("pipeline", "docgen"),
                config=item.get("config", {}),
                priority=item.get("priority", 50),
                task_id=item.get("task_id", ""),
            )
            tasks.append(t)
        return tasks

    def start(self):
        """启动队列执行"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()

        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent)
        worker = threading.Thread(target=self._dispatch_loop, daemon=True, name="batch-dispatch")
        worker.start()

    def _dispatch_loop(self):
        """调度循环：按优先级从队列取任务提交执行"""
        futures = {}
        while not self._stop_event.is_set():
            # 提交新任务
            with self._lock:
                pending = [t for t in self._queue if t.status == BatchTaskStatus.PENDING]
                running_count = sum(1 for t in self._queue if t.status == BatchTaskStatus.RUNNING)

                for task in pending:
                    if running_count >= self.max_concurrent:
                        break
                    task.status = BatchTaskStatus.RUNNING
                    task.started_at = time.time()
                    running_count += 1
                    fut = self._executor.submit(self._execute_task, task)
                    futures[fut] = task

            # 检查完成的 futures
            done_futs = [f for f in futures if f.done()]
            for fut in done_futs:
                task = futures.pop(fut)
                try:
                    fut.result()  # 触发异常（如果有）
                except Exception:
                    pass
                if self._on_task_done:
                    try:
                        self._on_task_done(task)
                    except Exception:
                        pass

            # 检查是否全部完成
            with self._lock:
                all_done = all(
                    t.status in (BatchTaskStatus.DONE, BatchTaskStatus.FAILED, BatchTaskStatus.CANCELLED)
                    for t in self._queue
                )
                if all_done and self._queue:
                    self._running = False
                    if self._on_all_done:
                        try:
                            self._on_all_done(list(self._queue))
                        except Exception:
                            pass
                    break

            if not self._queue:
                break

            time.sleep(0.5)

    def _execute_task(self, task: BatchTask):
        """执行单个文档生成任务"""
        try:
            from .pipeline import PipelineOrchestrator
            from .scheduler import Scheduler
            from pathlib import Path

            orch = PipelineOrchestrator()
            loaded = orch.register_agents()
            if not loaded:
                task.status = BatchTaskStatus.FAILED
                task.error = "No agents loaded"
                task.finished_at = time.time()
                return

            # 尝试加载 pipeline YAML
            plan = None
            pipelines_dir = Path(__file__).parent.parent / "pipelines"
            pf = pipelines_dir / f"{task.pipeline}.yaml"
            if pf.exists():
                try:
                    sched = Scheduler()
                    plan = sched.parse_file(str(pf))
                except Exception:
                    pass

            if plan:
                if task.output:
                    plan.raw.setdefault("pipeline", {})["output"] = task.output
                result = orch.run_plan(plan=plan, input_file=task.input_file, task_id=task.id, wait=True)
            else:
                result = orch.run(
                    task_id=task.id,
                    pipeline_name=task.pipeline,
                    input_file=task.input_file,
                    config=task.config,
                    wait=True,
                )

            task.status = BatchTaskStatus.DONE if result.status.value == "done" else BatchTaskStatus.FAILED
            task.progress = 100
            task.error = result.error or ""
            task.result = result.result if hasattr(result, "result") else {}
            orch.shutdown()

        except Exception as e:
            task.status = BatchTaskStatus.FAILED
            task.error = str(e)
        finally:
            task.finished_at = time.time()

    def cancel(self, task_id: str) -> bool:
        """取消队列中的任务"""
        with self._lock:
            for t in self._queue:
                if t.id == task_id and t.status == BatchTaskStatus.PENDING:
                    t.status = BatchTaskStatus.CANCELLED
                    return True
        return False

    def stop(self):
        """停止队列"""
        self._stop_event.set()
        if self._executor:
            self._executor.shutdown(wait=False)

    def wait_all(self, timeout: float = 600) -> bool:
        """等待所有任务完成"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                all_done = all(
                    t.status in (BatchTaskStatus.DONE, BatchTaskStatus.FAILED, BatchTaskStatus.CANCELLED)
                    for t in self._queue
                )
                if all_done:
                    return True
            time.sleep(1)
        return False

    def status(self) -> list[dict]:
        """返回所有任务状态"""
        with self._lock:
            return [t.to_dict() for t in self._queue]

    def summary(self) -> dict:
        """队列摘要"""
        with self._lock:
            total = len(self._queue)
            done = sum(1 for t in self._queue if t.status == BatchTaskStatus.DONE)
            failed = sum(1 for t in self._queue if t.status == BatchTaskStatus.FAILED)
            pending = sum(1 for t in self._queue if t.status == BatchTaskStatus.PENDING)
            running = sum(1 for t in self._queue if t.status == BatchTaskStatus.RUNNING)
            return {
                "total": total,
                "done": done,
                "failed": failed,
                "pending": pending,
                "running": running,
                "success_rate": done / total if total > 0 else 0.0,
            }
