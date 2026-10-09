"""两实例共享一库、并发跑真流水线 —— product-spec §2.1 的验收判据原文。

「两实例共享一库并发跑通（P1 判据）」。与 test_multi_process_workers.py 的分工：
那边验队列契约（互斥 / 租约 / 活 owner），用轻量 stub 子进程；这边跑真件——
两个真 TaskWorker 进程共享同一 tasks.db，用 mock 搜索的 test_pipeline
真把任务跑完并落盘产物。

断言的是并发正确性，不是调度公平性（哪台领哪条属于不确定调度，不做断言）：
- 无丢失：所有任务最终落 done 且产物在盘；
- 无重复领取：两个 worker 的领取集合互不相交、并集覆盖全部任务；
- 归属留痕：每条任务的 worker_id 与其领取者一致。

离线可跑：test_pipeline 走 mock 搜索 + 无 LLM Key 的降级写作，写入显式 output 路径。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

#: 子进程：真跑 worker（反复 run_once 直到队列空），领取明细写 JSON
_WORKER_SCRIPT = """
import json, os, sys
sys.path.insert(0, {project!r})
from pipeline_core.task_queue import TaskQueue
from pipeline_core.worker import TaskWorker

worker_id = sys.argv[1]
db = sys.argv[2]
pipelines = sys.argv[3]
out_path = sys.argv[4]
loops = int(sys.argv[5])
worker = TaskWorker(worker_id=worker_id,
                    agents_dir=os.path.join({project!r}, "agents"),
                    pipeline_dir=pipelines, queue=TaskQueue(db))
claimed = []
for _ in range(loops):
    item = worker.run_once()
    if item is None:
        break
    claimed.append(item["task_id"])
worker.shutdown()
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(claimed, fh)
"""


class TestTwoInstancesShareOneQueueEndToEnd:
    """规格原文的验收判据：两实例共享一库并发跑通。"""

    #: 每个实例最多消费这么多轮（多于任务数即可，避免进程停不下来）
    LOOP_COUNT = 8

    def _run_two_workers(self, tmp_path, n_tasks):
        from pipeline_core.task_queue import TaskQueue

        state = tmp_path / "state"
        state.mkdir()
        db = str(state / "tasks.db")
        pipelines = str(PROJECT / "pipelines")
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        queue = TaskQueue(db)
        ids = []
        for i in range(n_tasks):
            task_id = f"e2e-{i}"
            input_file = tmp_path / f"in-{i}.md"
            input_file.write_text(f"# 主题\nPython 异步编程基础 第 {i} 篇\n",
                                  encoding="utf-8")
            queue.submit(task_id, "test_pipeline", str(input_file),
                         {"output": str(out_dir / (task_id + ".md"))})
            ids.append(task_id)
        queue.close_all()

        outs = [tmp_path / "claimed-a.json", tmp_path / "claimed-b.json"]
        logs = [tmp_path / "worker-a.log", tmp_path / "worker-b.log"]
        procs = []
        for idx, worker_id in enumerate(("inst-a", "inst-b")):
            env = dict(os.environ, DOC_PIPELINE_STATE_DIR=str(state))
            # 输出重定向到文件而不是 PIPE：worker 真跑流水线会打大量日志，
            # PIPE 不读会写满管道缓冲区把子进程卡死（经典的 Popen 死锁），
            # 那是测试写法的问题，不是 worker 的问题——实测踩过（等了 900s）。
            with logs[idx].open("w", encoding="utf-8") as lf:
                procs.append(subprocess.Popen(
                    [sys.executable, "-c", _WORKER_SCRIPT.format(project=str(PROJECT)),
                     worker_id, db, pipelines, str(outs[idx]), str(self.LOOP_COUNT)],
                    stdout=lf, stderr=subprocess.STDOUT, text=True, env=env))
        for idx, p in enumerate(procs):
            p.wait(timeout=900)
            assert p.returncode == 0, logs[idx].read_text(encoding="utf-8")[-1500:]

        claimed = {}
        for idx, worker_id in enumerate(("inst-a", "inst-b")):
            claimed[worker_id] = json.loads(outs[idx].read_text(encoding="utf-8"))
        return db, ids, claimed, out_dir

    def test_no_task_lost_or_double_claimed(self, tmp_path):
        db, ids, claimed, out_dir = self._run_two_workers(tmp_path, n_tasks=4)
        a, b = claimed["inst-a"], claimed["inst-b"]

        # 无重复领取（互斥）
        assert not (set(a) & set(b)), (
            f"两实例领到了同一任务: {sorted(set(a) & set(b))}")
        # 无遗漏（并发跑通）
        assert set(a) | set(b) == set(ids), (
            f"任务丢失：领取 {sorted(set(a) | set(b))} vs 提交 {sorted(ids)}")

# 全部真跑完并落盘
        from pipeline_core.task_queue import TaskQueue

        queue = TaskQueue(db)
        try:
            for tid in ids:
                row = queue.get(tid)
                assert row["status"] == "done", (
                    "{} 终态是 {}: {}".format(tid, row["status"], row.get("error")))
                assert Path(out_dir / (tid + ".md")).exists(), f"{tid} 产物没落盘"
            # 归属留痕与实际领取者一致
            for worker_id, tids in claimed.items():
                for tid in tids:
                    assert queue.get(tid)["worker_id"] == worker_id
        finally:
            queue.close_all()
