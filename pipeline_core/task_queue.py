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

import contextlib
import logging
import os
import sqlite3
import threading
import time
import weakref
from pathlib import Path

from .fast_json import dumps as _fast_dumps
from .fast_json import loads as _fast_loads

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(Path(__file__).parent.parent.absolute(), "bus_data", "tasks.db")


class _TrackableConnection(sqlite3.Connection):
    """支持弱引用的连接（sqlite3.Connection 原生不可弱引用，经 factory 子类补槽）"""

    __slots__ = ("__weakref__",)


def _pid_alive(pid: int) -> bool:
    """跨平台进程存活探测（Windows 上禁用 os.kill(pid, 0)：它会 TerminateProcess）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid & 0xFFFFFFFF)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class TaskQueue:
    """SQLite 持久化任务队列（线程安全，WAL 模式）"""

    def __init__(self, db_path: str = _DEFAULT_DB):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # 修复 P0：原 _get_conn 每次调用都新建 SQLite 连接且永不关闭，
        # 导致 fd 泄漏（每次 submit/acquire/complete 都泄漏一个 fd）。
        # 改用 threading.local 缓存 per-thread 连接，复用同一 fd。
        self._local = threading.local()
        # 修复 P2：thread-local 连接随线程死亡靠 GC 回收，close() 只能关当前线程。
        # 创建连接时登记弱引用，close_all() 遍历关闭全部已登记连接。
        self._conn_refs: set = set()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程缓存的 SQLite 连接（复用，避免 fd 泄漏）。

        连接被 close()/close_all() 关闭后，下次调用自动重建（自愈）。
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
            except sqlite3.Error:
                conn = None
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=5, check_same_thread=False,
                                   factory=_TrackableConnection)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            self._local.conn = conn
            self._conn_refs.add(weakref.ref(conn))
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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(task_queue)").fetchall()}
            if "owner_pid" not in columns:
                conn.execute("ALTER TABLE task_queue ADD COLUMN owner_pid INTEGER DEFAULT 0")

    def submit(self, task_id: str, pipeline_name: str, input_file: str,
               config: dict = None) -> bool:
        """入队新任务。已存在则忽略（幂等）。"""
        with self._lock, self._get_conn() as conn:
            try:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO task_queue "
                    "(task_id, pipeline_name, input_file, status, config_json, created_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (task_id, pipeline_name, input_file,
                     _fast_dumps(config or {}), time.time()),
                )
                # 修复 P0 回归：原用 conn.total_changes > 0 判断是否插入，
                # 但连接复用（threading.local 缓存）后 total_changes 累积历史变更，
                # 导致幂等重复 submit 仍返回 True。
                # 改用 cursor.rowcount：仅反映本次 INSERT 的行数（0=被 IGNORE，1=成功插入）。
                return cursor.rowcount > 0
            except sqlite3.IntegrityError:
                return False

    def acquire(self, worker_id: str = "") -> dict | None:
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
            cursor = conn.execute(
                "UPDATE task_queue SET status = 'running', started_at = ?, worker_id = ?, "
                "owner_pid = ? WHERE task_id = ? AND status = 'pending'",
                (now, worker_id, os.getpid(), task_id),
            )
            # 修复 P0 回归：原用 conn.total_changes == 0 判断 UPDATE 是否生效，
            # 但连接复用后 total_changes 累积历史变更，永远 > 0，
            # 导致并发场景下两个 worker 可能 acquire 同一任务（数据竞争）。
            # 改用 cursor.rowcount：仅反映本次 UPDATE 的行数。
            if cursor.rowcount == 0:
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

    def recover(self, stale_seconds: float | None = None) -> list[dict]:
        """重启恢复：把 running 状态的任务改回 pending。

        Args:
            stale_seconds: 陈旧阈值（秒）。

        - None（默认）：旧行为，回收全部 running 任务（单进程场景兼容）。
        - 传入数值：多进程共享 tasks.db 安全模式，仅回收同时满足以下条件的任务：
            1. started_at 早于 now - stale_seconds（新鲜的 running 视为仍在执行）；
            2. owner_pid 为空 / 本进程 pid / 已死亡的外部进程
              （存活外部进程正在执行的任务跳过，避免翻回 pending 导致双执行）。

        返回被恢复的任务列表，供调用方决定是否重新执行。
        """
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT task_id, pipeline_name, input_file, config_json, "
                "started_at, owner_pid FROM task_queue WHERE status = 'running'"
            ).fetchall()
            if not rows:
                return []
            my_pid = os.getpid()
            now = time.time()
            recovered = []
            skipped = 0
            for task_id, pipeline_name, input_file, config_json, started_at, owner_pid in rows:
                if stale_seconds is not None:
                    owner_pid = int(owner_pid) if owner_pid else 0
                    if owner_pid and owner_pid != my_pid and _pid_alive(owner_pid):
                        skipped += 1
                        continue
                    if started_at and now - started_at <= stale_seconds:
                        skipped += 1
                        continue
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
            if skipped:
                logger.info(f"recover 跳过 {skipped} 个活跃/新鲜 running 任务")
            logger.info(f"恢复 {len(recovered)} 个中断任务")
            return recovered

    def get(self, task_id: str) -> dict | None:
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
        """显式关闭当前线程缓存的连接（修复 P0：原为空实现，连接永不关闭）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
            self._local.conn = None

    def close_all(self):
        """关闭本实例登记的全部 thread-local 连接（长驻进程优雅关停用）。

        其他线程残留的已关连接在其下次 _get_conn() 时探测失效并自动重建（自愈）。
        """
        refs = list(self._conn_refs)
        self._conn_refs.clear()
        for ref in refs:
            conn = ref()
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
        self._local.conn = None
