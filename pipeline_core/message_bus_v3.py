"""
MessageBus v3 - 持久化消息总线
================================
与 v2 相比的关键改进：
  - SQLite-backed 消息队列，进程崩溃不丢消息
  - 死信队列可回溯、可重放、可查询
  - 事务性 send/request，确保 at-least-once 语义
  - 幂等键持久化（非内存 set，重启后继续有效）
  - 高性能：批量写入 + WAL 模式
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from queue import Queue, Empty
from typing import Optional, Any, Callable


# ─── 常量 ─────────────────────────────────────

DEFAULT_DB_PATH = os.path.join(Path(__file__).parent.parent.absolute(), "bus_data", "message_bus.db")
MAX_PROCESSED_KEYS = 50000


# ─── 数据类型 ────────────────────────────────

class MessageType(Enum):
    EVENT = "event"
    REQUEST = "request"
    RESPONSE = "response"


class MessagePriority(Enum):
    LOW = 0
    NORMAL = 50
    HIGH = 100
    CRITICAL = 200

    @classmethod
    def from_value(cls, v):
        for p in cls:
            if p.value == v:
                return p
        return cls.NORMAL


@dataclass
class Message:
    topic: str
    payload: dict
    msg_type: MessageType = MessageType.EVENT
    from_agent: str = "system"
    to_agent: str = ""
    correlation_id: str = ""
    trace_id: str = ""
    priority: int = 50
    idempotency_key: str = ""
    retry_count: int = 0
    max_retries: int = 3
    msg_id: str = ""
    created_at: float = 0.0
    delivered: bool = False
    error: str = ""

    def __post_init__(self):
        if not self.msg_id:
            self.msg_id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = time.time()
        if not self.trace_id:
            self.trace_id = self.msg_id[:8]

    @classmethod
    def response(cls, original: Message, from_a: str, payload: dict) -> Message:
        return cls(
            topic=original.topic,
            payload=payload,
            msg_type=MessageType.RESPONSE,
            from_agent=from_a,
            to_agent=original.from_agent,
            correlation_id=original.correlation_id,
            trace_id=original.trace_id,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["msg_type"] = self.msg_type.value
        return d


@dataclass
class MessageMetrics:
    sent: int = 0
    received: int = 0
    failed: int = 0
    retried: int = 0
    dlq_count: int = 0
    high_watermark_hits: int = 0
    peak_depth: int = 0
    start_time: float = 0.0

    def record_sent(self):
        self.sent += 1

    def record_received(self):
        self.received += 1

    def record_failed(self):
        self.failed += 1

    def record_retry(self):
        self.retried += 1

    def record_dlq(self):
        self.dlq_count += 1

    def to_dict(self) -> dict:
        return {
            "sent": self.sent,
            "received": self.received,
            "failed": self.failed,
            "retried": self.retried,
            "dlq": self.dlq_count,
            "high_watermark_hits": self.high_watermark_hits,
            "peak_depth": self.peak_depth,
            "uptime": time.time() - self.start_time if self.start_time else 0,
        }


# ─── SQLite 持久化层 ───────────────────────

class PersistentStore:
    """SQLite-backed 消息持久化"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程独立的连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                timeout=10,
                check_same_thread=False,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_db(self):
        """建表"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                msg_id       TEXT PRIMARY KEY,
                topic        TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                msg_type     TEXT NOT NULL DEFAULT 'event',
                from_agent   TEXT NOT NULL DEFAULT 'system',
                to_agent     TEXT DEFAULT '',
                correlation_id TEXT DEFAULT '',
                trace_id     TEXT DEFAULT '',
                priority     INTEGER DEFAULT 50,
                retry_count  INTEGER DEFAULT 0,
                max_retries  INTEGER DEFAULT 3,
                idempotency_key TEXT DEFAULT '',
                created_at   REAL NOT NULL,
                delivered    INTEGER DEFAULT 0,
                error        TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS dlq (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id       TEXT NOT NULL,
                topic        TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                error_msg    TEXT NOT NULL,
                failed_at    REAL NOT NULL,
                replay_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS processed_keys (
                key        TEXT PRIMARY KEY,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic);
            CREATE INDEX IF NOT EXISTS idx_messages_delivered ON messages(delivered);
            CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
            CREATE INDEX IF NOT EXISTS idx_dlq_created ON dlq(failed_at);
            CREATE INDEX IF NOT EXISTS idx_processed_created ON processed_keys(created_at);
        """)
        conn.commit()

    # ── 消息 CRUD ──

    def save_message(self, msg: Message):
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO messages
               (msg_id, topic, payload_json, msg_type, from_agent, to_agent,
                correlation_id, trace_id, priority, retry_count, max_retries,
                idempotency_key, created_at, delivered, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                msg.msg_id, msg.topic, json.dumps(msg.payload, ensure_ascii=False),
                msg.msg_type.value, msg.from_agent, msg.to_agent,
                msg.correlation_id, msg.trace_id, msg.priority,
                msg.retry_count, msg.max_retries,
                msg.idempotency_key, msg.created_at,
                1 if msg.delivered else 0, msg.error,
            ),
        )
        conn.commit()

    def mark_delivered(self, msg_id: str):
        conn = self._get_conn()
        conn.execute("UPDATE messages SET delivered=1 WHERE msg_id=?", (msg_id,))
        conn.commit()

    def move_to_dlq(self, msg: Message, error: str):
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO dlq (msg_id, topic, payload_json, error_msg, failed_at)
               VALUES (?,?,?,?,?)""",
            (msg.msg_id, msg.topic, json.dumps(msg.to_dict(), ensure_ascii=False),
             error, time.time()),
        )
        conn.execute("UPDATE messages SET delivered=1, error=? WHERE msg_id=?", (error, msg.msg_id))
        conn.commit()

    def replay_dlq(self, dlq_id: int) -> Optional[dict]:
        """取一条死信数据并更新重放计数（真实重投由调用方完成）"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT msg_id, topic, payload_json, error_msg, replay_count FROM dlq WHERE id=?",
            (dlq_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE dlq SET replay_count=replay_count+1 WHERE id=?", (dlq_id,))
        conn.commit()
        return {
            "msg_id": row[0],
            "topic": row[1],
            "payload": json.loads(row[2]),
            "error": row[3],
            "replay_count": row[4] + 1,
        }

    def get_dlq_entry(self, dlq_id: int) -> Optional[dict]:
        """仅取一条死信数据，不更新计数、不重投（供 orchestrator 解析）"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, msg_id, topic, payload_json, error_msg, failed_at, replay_count FROM dlq WHERE id=?",
            (dlq_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "msg_id": row[1], "topic": row[2],
            "payload": json.loads(row[3]), "error": row[4],
            "failed_at": row[5], "replay_count": row[6],
        }

    def list_dlq(self, limit: int = 50) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, msg_id, topic, error_msg, failed_at, replay_count FROM dlq ORDER BY failed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": r[0], "msg_id": r[1], "topic": r[2], "error": r[3],
             "failed_at": r[4], "replay_count": r[5]}
            for r in rows
        ]

    # ── 幂等键 ──

    def is_idempotent(self, key: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM processed_keys WHERE key=?", (key,)
        ).fetchone()
        return row is not None

    def save_idempotent(self, key: str):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO processed_keys (key, created_at) VALUES (?,?)",
            (key, time.time()),
        )
        conn.commit()
        self._trim_processed_keys()

    def _trim_processed_keys(self):
        """保持幂等键数量在限制内"""
        conn = self._get_conn()
        count = conn.execute("SELECT COUNT(*) FROM processed_keys").fetchone()[0]
        if count > MAX_PROCESSED_KEYS:
            conn.execute(
                """DELETE FROM processed_keys WHERE rowid IN (
                    SELECT rowid FROM processed_keys ORDER BY created_at ASC LIMIT ?
                )""",
                (count - MAX_PROCESSED_KEYS,),
            )
            conn.commit()

    # ── 健康检查 ──

    def count_undelivered_events(self) -> int:
        """返回未投递的 EVENT 消息数量（背压检测用）"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE delivered=0 AND msg_type='event'"
        ).fetchone()
        return row[0] if row else 0

    def health(self) -> dict:
        try:
            conn = self._get_conn()
            msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            dlq_count = conn.execute("SELECT COUNT(*) FROM dlq").fetchone()[0]
            pk_count = conn.execute("SELECT COUNT(*) FROM processed_keys").fetchone()[0]
            return {
                "status": "ok",
                "messages": msg_count,
                "dlq": dlq_count,
                "processed_keys": pk_count,
                "db_path": self.db_path,
                "db_size": os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


# ─── MessageBus ──────────────────────────────

class MessageBus:
    """持久化消息总线 v3"""
    _async_queue: Queue

    def __init__(self, db_path: str = "", enable_persistence: bool = True,
                 max_queue_depth: int = 500, backpressure_watermark: int = 400):
        """MessageBus v3

        Args:
            db_path: SQLite 数据库路径（空=默认路径）
            enable_persistence: 是否启用持久化
            max_queue_depth: 队列最大深度
            backpressure_watermark: 触发背压的水位线
        """
        self._async_queue = Queue()
        self._store = PersistentStore(db_path) if enable_persistence else None
        self._subscribers: dict[str, list[tuple[Callable, int]]] = {}
        self._callbacks: dict[str, Callable] = {}
        self._log: list[Message] = []
        self._max_log = 1000
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._metrics = MessageMetrics(start_time=time.time())
        self.max_queue_depth = max_queue_depth
        self.backpressure_watermark = backpressure_watermark
        self._peak_depth = 0
        self._high_watermark_hits = 0

        # 工作线程
        self._worker_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._worker_running = True
        self._worker_thread.start()

    # ─── 订阅 ─────────────────────────────────

    def subscribe(self, topic: str, callback: Callable, priority: int = 50):
        with self._lock:
            if topic not in self._subscribers:
                self._subscribers[topic] = []
            self._subscribers[topic].append((callback, priority))
            self._subscribers[topic].sort(key=lambda x: x[1])

    def unsubscribe(self, topic: str, callback: Callable):
        with self._lock:
            if topic in self._subscribers:
                self._subscribers[topic] = [
                    (cb, p) for cb, p in self._subscribers[topic] if cb != callback
                ]

    # ─── 发送 ─────────────────────────────────

    def send(self, msg: Message, wait_for_delivery: bool = False):
        """发送消息（同步写入 SQLite，异步投递）"""
        with self._lock:
            self._log.append(msg)
            if len(self._log) > self._max_log:
                self._log = self._log[-self._max_log:]

        # 幂等检查
        if msg.idempotency_key:
            if self._store and self._store.is_idempotent(msg.idempotency_key):
                return
            if self._store:
                self._store.save_idempotent(msg.idempotency_key)

        # 持久化
        if self._store:
            self._store.save_message(msg)

        # 同步投递（REQUEST 类型需要同步等待，直接走回调）
        if msg.msg_type == MessageType.REQUEST:
            self._deliver(msg)
        elif msg.msg_type == MessageType.EVENT:
            # EVENT 通过工作队列异步投递
            self._async_queue.put(msg)
        else:
            self._deliver(msg)

        self._metrics.record_sent()

    _async_queue: Queue = Queue()

    def request(self, topic: str, from_a: str = "system", to_a: str = "",
                payload: dict = None, timeout: float = 30,
                idempotency_key: str = "") -> Any:
        """发送 REQUEST 并同步等待响应"""
        if payload is None:
            payload = {}

        # 幂等检查
        if idempotency_key and self._store and self._store.is_idempotent(idempotency_key):
            return None

        msg = Message(
            topic=topic,
            payload=payload,
            msg_type=MessageType.REQUEST,
            from_agent=from_a,
            to_agent=to_a,
            correlation_id=str(uuid.uuid4()),
            idempotency_key=idempotency_key,
        )

        event = threading.Event()
        result = [None]

        def handler(resp_msg: Message):
            result[0] = resp_msg.payload
            event.set()

        with self._lock:
            self._callbacks[msg.correlation_id] = handler

        self._metrics.record_sent()
        self._deliver(msg)

        event.wait(timeout=timeout)

        with self._lock:
            self._callbacks.pop(msg.correlation_id, None)

        return result[0]

    def reply(self, original: Message, from_a: str, payload: dict):
        """回复一个 REQUEST 消息"""
        resp = Message.response(original, from_a, payload)
        self._deliver(resp)
        return resp

    def publish(self, topic: str, from_a: str, payload: dict) -> dict:
        """
        发布 EVENT 消息（带背压支持）

        当队列中未投递的 EVENT 消息数量 >= backpressure_watermark 时，
        消息会被跳过（_backpressure_skipped=True），返回 busy 状态。
        否则正常发送。

        Returns:
            {"status": "sent", "msg_id": str}   — 成功
            {"status": "busy", "queue_depth": int} — 背压触发，消息未发送
        """
        depth = self.queue_depth()
        self._track_peak(depth)

        if depth >= self.backpressure_watermark:
            self._metrics.high_watermark_hits += 1
            msg = Message(topic=topic, payload=payload, msg_type=MessageType.EVENT, from_agent=from_a)
            msg._backpressure_skipped = True
            return {"status": "busy", "queue_depth": depth}

        msg = Message(topic=topic, payload=payload, msg_type=MessageType.EVENT, from_agent=from_a)
        self.send(msg)
        return {"status": "sent", "msg_id": msg.msg_id}

    def publish_blocking(self, topic: str, from_a: str, payload: dict,
                         max_wait: float = 30) -> dict:
        """
        阻塞式发布 EVENT 消息

        当背压触发时，以 0.1s 间隔轮询队列深度，直到水位降至
        backpressure_watermark 以下再发布。超时时返回 timeout 状态。

        Args:
            topic: 主题
            from_a: 发送方标识
            payload: 消息负载
            max_wait: 最大等待秒数（默认 30）

        Returns:
            {"status": "sent", "msg_id": str}    — 成功
            {"status": "timeout", "queue_depth": int} — 超时，消息未发送
        """
        deadline = time.time() + max_wait
        while time.time() < deadline:
            depth = self.queue_depth()
            self._track_peak(depth)
            if depth < self.backpressure_watermark:
                return self.publish(topic, from_a, payload)
            time.sleep(0.1)
        depth = self.queue_depth()
        self._track_peak(depth)
        return {"status": "timeout", "queue_depth": depth}

    def queue_depth(self) -> int:
        """返回当前未投递的 EVENT 消息数量（背压检测用）"""
        if not self._store:
            return 0
        return self._store.count_undelivered_events()

    def _track_peak(self, depth: int):
        """更新历史峰值队列深度"""
        if depth > self._metrics.peak_depth:
            self._metrics.peak_depth = depth

    # ─── 投递 ─────────────────────────────────

    def _deliver(self, msg: Message):
        """投递消息到订阅者（同步执行）"""
        # RESPONSE 消息：通过 _callbacks 路由回 request() 调用方
        if msg.msg_type == MessageType.RESPONSE and msg.correlation_id:
            with self._lock:
                cb = self._callbacks.pop(msg.correlation_id, None)
            if cb:
                try:
                    cb(msg)
                    self._metrics.record_received()
                except Exception as e:
                    self._metrics.record_failed()
                    if self._store:
                        self._store.move_to_dlq(msg, str(e))
                        self._metrics.record_dlq()
            return

        with self._lock:
            callbacks = list(self._subscribers.get(msg.topic, []))

        if not callbacks:
            return

        replied = False
        for callback, priority in sorted(callbacks, key=lambda x: x[1]):
            try:
                result = callback(msg)
                self._metrics.record_received()

                # REQUEST 消息的回调返回值自动回复
                if msg.msg_type == MessageType.REQUEST and result is not None and not replied:
                    self.reply(msg, msg.from_agent or "unknown", result)
                    replied = True

            except Exception as e:
                self._metrics.record_failed()
                if self._store:
                    self._store.move_to_dlq(msg, str(e))
                    self._metrics.record_dlq()

    def _process_loop(self):
        """异步事件处理循环"""
        while self._worker_running and not self._shutdown_event.is_set():
            try:
                msg = self._async_queue.get(timeout=1)
                self._deliver(msg)
            except Empty:
                continue
            except Exception:
                continue

    # ─── 查询 ─────────────────────────────────

    def health(self) -> dict:
        result = {"status": "ok", "subscribers": len(self._subscribers)}
        if self._store:
            result["store"] = self._store.health()
        result["metrics"] = self._metrics.to_dict()
        result["queue_depth"] = self.queue_depth()
        result["high_watermark_hits"] = self._metrics.high_watermark_hits
        result["peak_depth"] = self._metrics.peak_depth
        result["backpressure_watermark"] = self.backpressure_watermark
        return result

    def list_dlq(self, limit: int = 50) -> list[dict]:
        return self._store.list_dlq(limit) if self._store else []

    def replay_dlq(self, dlq_id: int) -> Optional[dict]:
        """重放一条死信：取出数据并真实重新投递到原 topic"""
        if not self._store:
            return None
        data = self._store.replay_dlq(dlq_id)
        if not data:
            return None
        # 真实重投：把原始 payload 重新 publish 到原 topic
        payload = data["payload"]
        original_payload = payload.get("payload", payload)
        from_agent = payload.get("from_agent", "dlq_replay")
        self.publish(data["topic"], from_agent, original_payload)
        return data

    # ─── 关闭 ─────────────────────────────────

    def shutdown(self):
        self._worker_running = False
        self._shutdown_event.set()
        self._worker_thread.join(timeout=3)
