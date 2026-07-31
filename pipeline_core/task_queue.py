"""TaskQueue — SQLite 持久化任务队列

解决核心问题：进程重启后内存任务丢失。

特性：
  - 任务持久化（SQLite WAL，与 message_store 同模式）
  - 原子 acquire/release（多 worker 安全）
  - 自动恢复：重启时 running → pending
  - 任务历史查询

用法：
    q = TaskQueue("bus_data/tasks.db")
    q.submit("task-001", "docgen", "input.md", {"key": "val"})
    task = q.acquire(worker_id="w1")      # 原子出队
    q.complete("task-001", result={...})  # 完成
    q.recover()                            # 重启恢复
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import logging
from pathlib import Path
from typing import Optional, Any

from .fast_json import dumps as _fast_dumps, loads as _fast_loads

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(Path(__file__).parent.parent.absolute(), "bus_data", "tasks.db")


class TaskQueue:
    """SQLite 持久化任务队列（线程安全，WAL 模式）"""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_queue (
                    task_id       TEXT PRIMARY KEY,
                    pipeline_name TEXT NOT NULL,
                    input_file    TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'pending',
                    config_json   TEXT DEFAULT '{}',
                    result_json   TEXT DEFAULT '{}',
                    error         TEXT DEFAULT '',
                    created_at    REAL NOT NULL,
                    started_at    REAL DEFAULT 0,
                    finished_at   REAL DEFAULT 0,
                    worker_id     TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON task_queue(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON task_queue(created_at)")

    def submit(self, task_id: str, pipeline_name: str, input_file: str,
               config: dict = None) -> bool:
        """入队新任务。已存在则忽略（幂等）。"""
        with self._lock, self._get_conn() as conn:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO task_queue "
                    "(task_id, pipeline_name, input_file, status, config_json, created_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (task_id, pipeline_name, input_file,
                     _fast_dumps(config or {}), time.time()),
                )
                return conn.total_changes > 0
            except sqlite3.IntegrityError:
                return False

    def acquire(self, worker_id: str = "") -> Optional[dict]:
        """原子出队：取一条 pending 任务，标记为 running。

        多 worker 安全：SQLite 事务保证同一任务不会被两个 worker acquire。
        """
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT task_id, pipeline_name, input_file, config_json "
                "FROM task_queue WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            task_id, pipeline_name, input_file, config_json = row
            now = time.time()
            conn.execute(
                "UPDATE task_queue SET status = 'running', started_at = ?, worker_id = ? "
                "WHERE task_id = ? AND status = 'pending'",
                (now, worker_id, task_id),
            )
            if conn.total_changes == 0:
                return None
            return {
                "task_id": task_id,
                "pipeline_name": pipeline_name,
                "input_file": input_file,
                "config": _fast_loads(config_json) if config_json else {},
            }

    def complete(self, task_id: str, result: dict = None, error: str = ""):
        """标记任务完成或失败"""
        with self._lock, self._get_conn() as conn:
            status = "failed" if error else "done"
            conn.execute(
                "UPDATE task_queue SET status = ?, result_json = ?, error = ?, finished_at = ? "
                "WHERE task_id = ?",
                (status, _fast_dumps(result or {}), error, time.time(), task_id),
            )

    def cancel(self, task_id: str):
        """取消任务"""
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "UPDATE task_queue SET status = 'cancelled', finished_at = ? "
                "WHERE task_id = ? AND status IN ('pending', 'running')",
                (time.time(), task_id),
            )

    def update_status(self, task_id: str, status: str, result: dict = None, error: str = ""):
        """通用状态更新"""
        with self._lock, self._get_conn() as conn:
            if result is not None or error:
                conn.execute(
                    "UPDATE task_queue SET status = ?, result_json = ?, error = ?, finished_at = ? "
                    "WHERE task_id = ?",
                    (status, _fast_dumps(result or {}), error,
                     time.time() if status in ("done", "failed", "cancelled") else 0,
                     task_id),
                )
            else:
                conn.execute(
                    "UPDATE task_queue SET status = ? WHERE task_id = ?",
                    (status, task_id),
                )

    def recover(self) -> list[dict]:
        """重启恢复：把 running 状态的任务改回 pending。

        返回被恢复的任务列表，供调用方决定是否重新执行。
        """
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT task_id, pipeline_name, input_file, config_json "
                "FROM task_queue WHERE status = 'running'"
            ).fetchall()
            if not rows:
                return []
            recovered = []
            for task_id, pipeline_name, input_file, config_json in rows:
                conn.execute(
                    "UPDATE task_queue SET status = 'pending', worker_id = '' WHERE task_id = ?",
                    (task_id,),
                )
                recovered.append({
                    "task_id": task_id,
                    "pipeline_name": pipeline_name,
                    "input_file": input_file,
                    "config": _fast_loads(config_json) if config_json else {},
                })
            logger.info(f"恢复 {len(recovered)} 个中断任务")
            return recovered

    def get(self, task_id: str) -> Optional[dict]:
        """查询单个任务"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT task_id, pipeline_name, input_file, status, config_json, "
                "result_json, error, created_at, started_at, finished_at, worker_id "
                "FROM task_queue WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def list_all(self, status: str = None, limit: int = 100) -> list[dict]:
        """列出任务（可按状态过滤）"""
        with self._get_conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT task_id, pipeline_name, input_file, status, config_json, "
                    "result_json, error, created_at, started_at, finished_at, worker_id "
                    "FROM task_queue WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT task_id, pipeline_name, input_file, status, config_json, "
                    "result_json, error, created_at, started_at, finished_at, worker_id "
                    "FROM task_queue ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def list_pending(self) -> list[dict]:
        """列出所有 pending 任务"""
        return self.list_all(status="pending", limit=1000)

    def cleanup(self, max_age_days: int = 30):
        """清理过期已完成任务"""
        cutoff = time.time() - max_age_days * 86400
        with self._lock, self._get_conn() as conn:
            conn.execute(
                "DELETE FROM task_queue "
                "WHERE status IN ('done', 'failed', 'cancelled') AND finished_at < ?",
                (cutoff,),
            )

    def stats(self) -> dict:
        """队列统计"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM task_queue GROUP BY status"
            ).fetchall()
            return {status: cnt for status, cnt in rows}

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "task_id": row[0],
            "pipeline_name": row[1],
            "input_file": row[2],
            "status": row[3],
            "config": _fast_loads(row[4]) if row[4] else {},
            "result": _fast_loads(row[5]) if row[5] else {},
            "error": row[6] or "",
            "created_at": row[7],
            "started_at": row[8],
            "finished_at": row[9],
            "worker_id": row[10] or "",
        }

    def close(self):
        pass