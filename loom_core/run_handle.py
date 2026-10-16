"""`RunHandle` —— 一次运行的对外句柄（spec §6「API 原语」）。

为什么需要它：此前所有接口层（MCP `get_task`、Admin `GET /tasks/<id>`、MAOP 适配器）
都各写一份"从 task 对象拼载荷"的代码，而**产物**这一维根本没人拼得出——MAOP 只能
自己按节点名造伪 ID（`pipeline:<名>:step<n>:<agent>`），读的人拿不到"这次运行到底
产出了什么"。本模块把五件事收成一个原语：`status / wait / cancel / resume / artifacts`。

> spec 写的 `await` 是 Python 关键字，落地名为 `wait()`，语义不变。

句柄不是持久收据：它按 `task_id` 现取现拼。内存里没有就回落到持久队列读状态
（多机 worker / 重启后的场景），回落也只报状态、不假装拿得到产物与结果。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import nullcontext
from typing import Any

from .pipeline import TaskStatus

#: 这些状态不会再自己变化，`wait()` 到此为止。PAUSED 算终态是因为它可能停在
#: 审批闸门前等人——把它当"还在跑"会让 wait() 挂到超时。
TERMINAL_STATUSES = frozenset({
    TaskStatus.DONE.value, TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value, TaskStatus.PAUSED.value,
})


class RunHandle:
    """按 task_id 取得的一次运行句柄（不持有 task 对象，每次读最新态）。"""

    def __init__(self, orch: Any, task_id: str):
        self._orch = orch
        self._task_id = str(task_id)

    # ─── 身份 ──────────────────────────────────────────

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def task(self):
        """内存里的工作对象；不在内存（重启/别的机器）时为 None。"""
        getter = getattr(self._orch, "get_task", None)
        return getter(self._task_id) if callable(getter) else None

    # ─── status ────────────────────────────────────────

    def status(self) -> str:
        """状态字符串；内存缺失时读持久队列，两边都没有则 "unknown"。

        `task.status` 只读一次：并行腿随时可能推进它，读两遍就可能拿 A 的 `.value`
        判空、拿 B 的值返回。
        """
        task = self.task
        if task is not None:
            state = task.status
            return state.value if hasattr(state, "value") else str(state)
        queue = getattr(self._orch, "task_queue", None)
        row = queue.get(self._task_id) if queue is not None else None
        if isinstance(row, dict):
            return str(row.get("status") or "unknown")
        return "unknown"

    def is_terminal(self) -> bool:
        return self.status() in TERMINAL_STATUSES

    def wait(self, timeout: float | None = None, poll: float = 0.2) -> str:
        """阻塞到终态或超时，返回最后一次读到的状态。

        超时**不抛**：调用方要能区分"跑完了"与"我等多了"，所以判据是返回值 +
        `is_terminal()`，而不是异常。`timeout=None` 表示不设限。

        任务在内存里时 `poll` 只是字典读；不在内存时每轮是一次 SQLite 取行，
        默认 0.2s 就是把这条路径的开销钉住（多机 worker 下状态仍能从队列读到）。
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            current = self.status()
            if current in TERMINAL_STATUSES:
                return current
            if deadline is not None and time.monotonic() >= deadline:
                return current
            time.sleep(max(poll, 0.001))

    # ─── cancel / resume ───────────────────────────────

    def cancel(self) -> bool:
        """取消；引擎侧不接受（已完成/不存在）时如实返回 False。"""
        cancel = getattr(self._orch, "cancel", None)
        return bool(cancel(self._task_id)) if callable(cancel) else False

    def resume(self) -> bool:
        """从断点续跑。停在审批闸门上的任务由引擎拒绝，这里同样返回 False。"""
        resume = getattr(self._orch, "resume", None)
        return bool(resume(self._task_id)) if callable(resume) else False

    # ─── artifacts ─────────────────────────────────────

    def artifacts(self) -> list[dict]:
        """本次运行的产出清单：逐节点产物 + 落盘交付物。

        只报**引擎确实记下来的东西**：节点成功写回的结构化产物（`TaskNode.result`
        的键），以及声明 `WRITES_OUTPUT` 的节点挂到任务上的交付物路径。
        不猜节点名、不造伪 ID——那正是本原语要替掉的旧做法。

        内存里没有这一运行（被 `_trim_task_history` 淘汰的老任务）时，
        从交付账本 `deliveries.db` 重建**同一形状**的清单，见 `_ledger_record`。
        """
        task = self.task
        if task is None:
            return self._artifacts_from(self._ledger_record())
        items: list[dict] = []
        for name, node in (getattr(task, "dag_nodes", None) or {}).items():
            result = getattr(node, "result", None)
            if not isinstance(result, dict) or not result:
                continue
            items.append({
                "kind": "artifact",
                "node": str(name),
                "keys": sorted(str(k) for k in result),
                "status": str(getattr(node, "status", "") or ""),
            })
        path = str(getattr(task, "output_path", "") or "")
        if path:
            items.append({"kind": "deliverable", "node": "", "path": path,
                          "bytes": _size_of(path)})
        return items

    def _ledger_record(self) -> dict | None:
        """交付账本里这一 run 的记录；库或行不存在给 None。

        刻意**不**因为读一次就建库：`DeliveryLedger()` 构造时会 mkdir + 建表，
        那会让"查一个从没跑过的 id"在磁盘上留下副作用。
        """
        from pathlib import Path

        from . import state_paths
        from .deliveries import DeliveryLedger

        db = state_paths.store_path("deliveries.db")
        if not Path(db).exists():
            return None
        try:
            return DeliveryLedger(db_path=str(db)).get(self._task_id)
        except (OSError, sqlite3.Error):
            # 读不动账本 ≠ 没有产物；但也不该把整条接口请求打飞。给 None 让
            # 调用方退回"只有状态"的降级形态（status() 仍走持久队列）。
            return None

    @staticmethod
    def _artifacts_from(rec: dict | None) -> list[dict]:
        """账本记录 → 与内存态逐字段同形的清单（两边不一致就是两个来源在漂移）。"""
        if not isinstance(rec, dict):
            return []
        items: list[dict] = []
        for node in rec.get("nodes") or []:
            keys = node.get("artifact_keys") or []
            if not keys:
                continue
            items.append({
                "kind": "artifact",
                "node": str(node.get("node", "")),
                "keys": sorted(str(k) for k in keys),
                "status": str(node.get("status", "") or ""),
            })
        path = str(rec.get("output_path") or "")
        if path:
            items.append({"kind": "deliverable", "node": "", "path": path,
                          "bytes": _size_of(path)})
        return items

    # ─── 载荷 ──────────────────────────────────────────

    def to_dict(self) -> dict:
        """接口层共享的状态载荷（字段名沿用各接口已有键，只增不改）。"""
        task = self.task
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "id": self.task_id,
            "status": self.status(),
            "terminal": self.is_terminal(),
        }
        if task is None:
            # 被 _trim_task_history 淘汰的老运行：账本里有的（pipeline / error /
            # 产物清单）照原样给，账本没有的（result 明细）不编。
            rec = self._ledger_record()
            out["pipeline"] = str((rec or {}).get("pipeline") or "")
            out["error"] = str((rec or {}).get("error") or "") or None
            out["artifacts"] = self._artifacts_from(rec)
            return out
        out["pipeline"] = getattr(task, "pipeline_name", "")
        out["progress"] = int(getattr(task, "progress", 0) or 0)
        out["error"] = getattr(task, "error", "") or None
        out["result"] = self._copy_result(task)
        out["artifacts"] = self.artifacts()
        out["awaiting_approval"] = (dict(task.awaiting_approval)
                                    if getattr(task, "awaiting_approval", None) else None)
        out["approvals"] = [dict(a) for a in (getattr(task, "approvals", None) or [])]
        return out

    @staticmethod
    def _copy_result(task) -> dict:
        # 必须持 result_lock：并行腿在 as_completed 里逐节点写 task.result，
        # 无锁拷贝会撞上 dict 并发变更（RuntimeError: dictionary changed size）
        lock = getattr(task, "result_lock", None)
        with (lock if lock is not None else nullcontext()):
            return dict(getattr(task, "result", {}) or {})


def _size_of(path: str) -> int:
    """交付物字节数；读不到（已删/无权限）报 -1 而不是省略这条产物。"""
    try:
        return os.path.getsize(path)
    except OSError:
        return -1
