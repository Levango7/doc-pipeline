"""队列多机（真跨进程）判据 —— product-spec §2.1 验收线第 3 条的"队列多机"。

`tests/test_worker.py` 的互斥/租约判据全在**同进程多线程**上跑，其注释自己写明
"等价于两个进程共用同一份 tasks.db"——那是类比，不是实证。本文件把这条补成真件：
**subprocess 起独立解释器进程**，都指向同一份 tasks.db。

1. 互斥：多个进程同时抢 N 条任务，断言每个 task_id 恰好被抢到一次（无遗漏无重复）；
2. 租约回收：进程 A 抢到任务后**被强杀**（未落终态），后续 worker 必须回收重跑；
3. 活着的 owner 不被抢：owner 进程存活时，别的 worker 的回收必须放过它。

离线可跑：进程之间只共享 SQLite 文件，不跑真流水线——判据打的正是"多机共享一库"
的并发契约本身。
"""
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

#: 子进程脚本：抢一条任务并把结果写到 out（拿不到就写 {}）
_CLAIM_SCRIPT = """
import json, sys
sys.path.insert(0, {project!r})
from pipeline_core.task_queue import TaskQueue
db, worker_id, out = sys.argv[1], sys.argv[2], sys.argv[3]
item = TaskQueue(db).acquire(worker_id=worker_id)
json.dump(item or {{}}, open(out, "w", encoding="utf-8"))
"""

#: 子进程脚本：抢任务后停住（外部会强杀它），ready 文件写回抢到的 task_id
_HOLD_SCRIPT = """
import sys, time
sys.path.insert(0, {project!r})
from pipeline_core.task_queue import TaskQueue
db, worker_id, ready = sys.argv[1], sys.argv[2], sys.argv[3]
item = TaskQueue(db).acquire(worker_id=worker_id)
with open(ready, "w", encoding="utf-8") as f:
    f.write(item["task_id"] if item else "")
time.sleep(300)
"""


def _spawn(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script.format(project=str(PROJECT)), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for_file(path: Path, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    raise AssertionError(f"子进程没在 {timeout}s 内写出 {path}")


class TestCrossProcessMutualExclusion:
    """真跨进程抢同一份库：每条任务只被抢到一次。"""

    def test_each_task_claimed_exactly_once_across_processes(self, tmp_path):
        from pipeline_core.task_queue import TaskQueue

        db = str(tmp_path / "tasks.db")
        n = 6
        queue = TaskQueue(db)
        for i in range(n):
            queue.submit(f"t-{i}", "test_pipeline", "input.md", {})
        queue.close_all()

        # 8 个独立进程同时开抢（多于任务数，必有进程空手而归）
        outs = [tmp_path / f"out-{i}.json" for i in range(8)]
        procs = [_spawn(_CLAIM_SCRIPT, db, f"w{i}", str(outs[i])) for i in range(8)]
        for p in procs:
            p.wait(timeout=60)
        for i, p in enumerate(procs):
            assert p.returncode == 0, f"进程 {i} 退出码 {p.returncode}: {p.stderr.read()[-400:]}"

        claimed = []
        for out in outs:
            item = json.loads(out.read_text(encoding="utf-8"))
            if item:
                claimed.append(item["task_id"])
        assert len(claimed) == len(set(claimed)) == n, (
            f"抢到的任务应无重复无遗漏：{sorted(claimed)}")
        assert set(claimed) == {f"t-{i}" for i in range(n)}


class TestCrossProcessLeaseReclaim:
    """跨进程租约：owner 被强杀 → 另一台必须能回收；owner 活着 → 不许抢。"""

    def test_killed_owner_task_is_reclaimed_by_another_process(self, tmp_path):
        from pipeline_core.task_queue import TaskQueue, _pid_alive

        db = str(tmp_path / "tasks.db")
        queue = TaskQueue(db)
        queue.submit("t-crash", "test_pipeline", "input.md", {})
        queue.close_all()

        ready = tmp_path / "ready.txt"
        victim = _spawn(_HOLD_SCRIPT, db, "w-victim", str(ready))
        task_id = _wait_for_file(ready)
        assert task_id == "t-crash"
        owner_pid = victim.pid
        assert _pid_alive(owner_pid), "前提：持任务的进程当时应存活"

        victim.kill()                      # 强杀：终态没落，行会停在 running
        victim.wait(timeout=30)

        # 另一进程（新解释器）用租约阈值 0 回收：owner 已死 + 租约过期 ⇒ 应回收
        reclaim_script = """
import json, sys
sys.path.insert(0, {project!r})
from pipeline_core.task_queue import TaskQueue
db, out = sys.argv[1], sys.argv[2]
q = TaskQueue(db)
n = len(q.recover(stale_seconds=0.01))
json.dump({{"recovered": n, "status": q.get("t-crash")["status"]}},
          open(out, "w", encoding="utf-8"))
"""
        out = tmp_path / "reclaim.json"
        p = _spawn(reclaim_script, db, str(out))
        p.wait(timeout=60)
        assert p.returncode == 0, p.stderr.read()[-400:]
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["recovered"] == 1, "被杀 worker 留下的 running 行没有被回收"
        assert data["status"] == "pending", "回收后应回到 pending 可被重跑"

        # 再抢一次：回收的任务能被新 worker 正常取走
        item = TaskQueue(db).acquire(worker_id="w-takeover")
        assert item and item["task_id"] == "t-crash"

    def test_live_owner_task_is_not_stolen_across_processes(self, tmp_path):
        from pipeline_core.task_queue import TaskQueue

        db = str(tmp_path / "tasks.db")
        queue = TaskQueue(db)
        queue.submit("t-live", "test_pipeline", "input.md", {})
        queue.close_all()

        ready = tmp_path / "ready.txt"
        holder = _spawn(_HOLD_SCRIPT, db, "w-live", str(ready))
        try:
            assert _wait_for_file(ready) == "t-live"

            # 另一进程回收：owner 存活 ⇒ 必须放过（否则任务被抢走会双跑）
            reclaim_script = """
import json, sys
sys.path.insert(0, {project!r})
from pipeline_core.task_queue import TaskQueue
q = TaskQueue(sys.argv[1])
json.dump({{"recovered": len(q.recover(stale_seconds=0.01)),
            "status": q.get("t-live")["status"]}},
          open(sys.argv[2], "w", encoding="utf-8"))
"""
            out = tmp_path / "reclaim.json"
            p = _spawn(reclaim_script, db, str(out))
            p.wait(timeout=60)
            data = json.loads(out.read_text(encoding="utf-8"))
            assert data["recovered"] == 0, \
                "活着的 owner 正在执行的任务被回收了——这会导致双跑"
            assert data["status"] == "running"
        finally:
            holder.kill()
            holder.wait(timeout=30)


class TestWorkerCliIsUsableAcrossProcesses:
    """worker CLI 的真进程巡检：`--once` 空库退出码 0、不卡死。"""

    def test_once_on_empty_queue_exits_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOC_PIPELINE_STATE_DIR", str(tmp_path / "state"))
        r = subprocess.run(
            [sys.executable, "run.py", "--worker", "--once", "--worker-id=probe"],
            capture_output=True, text=True, timeout=180, cwd=str(PROJECT))
        assert r.returncode == 0, r.stderr[-800:]
        assert "退出：处理 0 条" in r.stdout
