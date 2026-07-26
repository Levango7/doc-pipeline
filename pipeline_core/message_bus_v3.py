"""
MessageBus v3 - 持久化消息总线
================================
传输层：发布/订阅、请求/响应、异步广播
存储层：委托给 message_store.PersistentStore
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from queue import Queue, Empty
from typing import Optional, Any, Callable

from .message_store import (
    DEFAULT_DB_PATH, MAX_PROCESSED_KEYS,
    MessageType, MessagePriority, Message, MessageMetrics,
    PersistentStore,
)

_logger = logging.getLogger(__name__)


# ─── MessageBus ──────────────────────────────

class MessageBus:
    """持久化消息总线 v3"""

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

        # 背压通知条件变量（替代 sleep 轮询）
        self._backpressure_cv = threading.Condition(self._lock)

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

    def send(self, msg: Message, wait_for_delivery: bool = False, persist: bool = True):
        """发送消息

        Args:
            msg: 消息对象
            wait_for_delivery: 是否等待投递完成
            persist: 是否持久化到 SQLite（REQUEST/RESPONSE 热路径可跳过）
        """
        with self._lock:
            self._log.append(msg)
            if len(self._log) > self._max_log:
                self._log = self._log[-self._max_log:]

            # 幂等检查（在锁内，避免 check-then-act 竞态）
            if msg.idempotency_key and self._store:
                if self._store.check_and_mark_idempotent(msg.idempotency_key):
                    return  # 已存在，重复消息，跳过

            # 持久化（仅 EVENT 消息写入 SQLite，REQUEST/RESPONSE 是瞬态同步消息跳过）
            # 优化：减少热路径写锁竞争，EVENT 需要持久化用于 DLQ/审计
            if persist and self._store and msg.msg_type == MessageType.EVENT:
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

    def request(self, topic: str, from_a: str = "system", to_a: str = "",
                payload: dict = None, timeout: float = 30,
                idempotency_key: str = "") -> Any:
        """发送 REQUEST 并同步等待响应"""
        if payload is None:
            payload = {}

        # 幂等检查（原子操作）
        if idempotency_key and self._store:
            if self._store.check_and_mark_idempotent(idempotency_key):
                return None  # 已存在，重复请求

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

        当背压触发时，通过 Condition 通知机制等待水位降至
        backpressure_watermark 以下再发布（替代 sleep 轮询）。
        超时时返回 timeout 状态。

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
        with self._backpressure_cv:
            while True:
                depth = self.queue_depth()
                self._track_peak(depth)
                if depth < self.backpressure_watermark:
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    depth = self.queue_depth()
                    self._track_peak(depth)
                    return {"status": "timeout", "queue_depth": depth}
                # 等待通知或超时，不再 sleep 轮询
                self._backpressure_cv.wait(timeout=min(remaining, 1.0))
        return self.publish(topic, from_a, payload)

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
        if msg.msg_type == MessageType.RESPONSE and msg.correlation_id:
            with self._lock:
                cb = self._callbacks.pop(msg.correlation_id, None)
            if cb:
                try:
                    cb(msg)
                    self._metrics.record_received()
                    if self._store and msg.msg_id:
                        self._store.mark_delivered(msg.msg_id)
                except Exception as e:
                    self._metrics.record_failed()
                    if self._store:
                        self._store.move_to_dlq(msg, str(e))
                        self._metrics.record_dlq()
            return

        with self._lock:
            callbacks = list(self._subscribers.get(msg.topic, []))

        if not callbacks:
            if self._store and msg.msg_id:
                self._store.mark_delivered(msg.msg_id)
            return

        delivered_ok = True
        replied = False
        for callback, priority in sorted(callbacks, key=lambda x: x[1]):
            try:
                result = callback(msg)
                self._metrics.record_received()

                if msg.msg_type == MessageType.REQUEST and result is not None and not replied:
                    self.reply(msg, msg.from_agent or "unknown", result)
                    replied = True

            except Exception as e:
                delivered_ok = False
                self._metrics.record_failed()
                if self._store:
                    self._store.move_to_dlq(msg, str(e))
                    self._metrics.record_dlq()

        if delivered_ok and self._store and msg.msg_id:
            # EVENT 消息由 _process_loop 批量标记，此处仅处理非 EVENT（如 RESPONSE）
            if msg.msg_type != MessageType.EVENT:
                self._store.mark_delivered(msg.msg_id)
                with self._backpressure_cv:
                    self._backpressure_cv.notify_all()

    def _process_loop(self):
        """异步事件处理循环（批量 drain 优化：一次取多条，减少锁竞争和 SQLite 写入）"""
        BATCH_SIZE = 50  # 单批最大处理条数
        while self._worker_running and not self._shutdown_event.is_set():
            try:
                # 阻塞等待第一条消息
                msg = self._async_queue.get(timeout=1)
                batch = [msg]
                # 非阻塞 drain：尽可能多取（最多 BATCH_SIZE 条）
                while len(batch) < BATCH_SIZE:
                    try:
                        batch.append(self._async_queue.get_nowait())
                    except Empty:
                        break
                # 逐条投递，收集已投递的 msg_id
                delivered_ids = []
                for m in batch:
                    self._deliver(m)
                    if m.msg_id and m.msg_type == MessageType.EVENT:
                        delivered_ids.append(m.msg_id)
                # 批量标记已投递（一次 UPDATE 替代 N 次）
                if delivered_ids and self._store:
                    try:
                        self._store.mark_delivered_batch(delivered_ids)
                    except Exception:
                        pass  # 标记失败不影响投递
                # 通知背压等待者
                if delivered_ids:
                    with self._backpressure_cv:
                        self._backpressure_cv.notify_all()
            except Empty:
                continue
            except Exception as e:
                _logger.error(f"[MessageBus] _process_loop error: {e}", exc_info=True)

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