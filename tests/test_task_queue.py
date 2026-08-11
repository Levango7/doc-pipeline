"""TaskQueue — 持久化任务队列测试"""
import threading

import pytest

from pipeline_core.task_queue import TaskQueue


@pytest.fixture
def queue(tmp_path):
    db = str(tmp_path / "test_tasks.db")
    return TaskQueue(db_path=db)


class TestTaskQueue:
    def test_submit_and_acquire(self, queue):
        """基本入队出队"""
        assert queue.submit("t1", "docgen", "input.md", {"key": "val"})
        task = queue.acquire(worker_id="w1")
        assert task is not None
        assert task["task_id"] == "t1"
        assert task["pipeline_name"] == "docgen"
        assert task["input_file"] == "input.md"
        assert task["config"] == {"key": "val"}

    def test_acquire_empty_queue(self, queue):
        """空队列出队返回 None"""
        assert queue.acquire() is None

    def test_acquire_order(self, queue):
        """FIFO 顺序"""
        queue.submit("t1", "p", "a.md")
        queue.submit("t2", "p", "b.md")
        queue.submit("t3", "p", "c.md")
        assert queue.acquire()["task_id"] == "t1"
        assert queue.acquire()["task_id"] == "t2"
        assert queue.acquire()["task_id"] == "t3"

    def test_complete_success(self, queue):
        """完成任务"""
        queue.submit("t1", "p", "a.md")
        queue.acquire()
        queue.complete("t1", result={"output": "done"})
        task = queue.get("t1")
        assert task["status"] == "done"
        assert task["result"] == {"output": "done"}
        assert task["finished_at"] > 0

    def test_complete_with_error(self, queue):
        """失败任务"""
        queue.submit("t1", "p", "a.md")
        queue.acquire()
        queue.complete("t1", error="something broke")
        task = queue.get("t1")
        assert task["status"] == "failed"
        assert task["error"] == "something broke"

    def test_cancel(self, queue):
        """取消任务"""
        queue.submit("t1", "p", "a.md")
        queue.cancel("t1")
        task = queue.get("t1")
        assert task["status"] == "cancelled"

    def test_recover(self, queue):
        """重启恢复：running → pending"""
        queue.submit("t1", "docgen", "a.md")
        queue.submit("t2", "docgen", "b.md")
        queue.acquire()  # t1 → running
        queue.acquire()  # t2 → running

        recovered = queue.recover()
        assert len(recovered) == 2
        assert all(t["status"] == "pending" for t in queue.list_pending())

    def test_recover_empty(self, queue):
        """无 running 任务时恢复返回空"""
        queue.submit("t1", "p", "a.md")
        assert queue.recover() == []

    def test_idempotent_submit(self, queue):
        """重复 submit 同一 task_id 被忽略"""
        assert queue.submit("t1", "p", "a.md") is True
        assert queue.submit("t1", "p", "a.md") is False  # 已存在

    def test_list_all(self, queue):
        """列出所有任务"""
        queue.submit("t1", "p", "a.md")
        queue.submit("t2", "p", "b.md")
        queue.acquire()
        tasks = queue.list_all()
        assert len(tasks) == 2

    def test_list_by_status(self, queue):
        """按状态过滤"""
        queue.submit("t1", "p", "a.md")
        queue.submit("t2", "p", "b.md")
        queue.acquire()
        pending = queue.list_all(status="pending")
        running = queue.list_all(status="running")
        assert len(pending) == 1
        assert len(running) == 1

    def test_stats(self, queue):
        """队列统计"""
        queue.submit("t1", "p", "a.md")
        queue.submit("t2", "p", "b.md")
        queue.acquire()
        stats = queue.stats()
        assert stats.get("pending") == 1
        assert stats.get("running") == 1

    def test_cleanup(self, queue):
        """清理过期任务"""
        queue.submit("t1", "p", "a.md")
        queue.complete("t1", result={})
        queue.cleanup(max_age_days=0)
        assert queue.get("t1") is None

    def test_concurrent_acquire(self, queue):
        """多线程并发 acquire 不会拿到同一个任务"""
        queue.submit("t1", "p", "a.md")
        queue.submit("t2", "p", "b.md")
        results = []
        lock = threading.Lock()

        def worker(wid):
            task = queue.acquire(worker_id=wid)
            if task:
                with lock:
                    results.append(task["task_id"])

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 2
        assert set(results) == {"t1", "t2"}

    def test_get_nonexistent(self, queue):
        """查询不存在的任务"""
        assert queue.get("nonexistent") is None
