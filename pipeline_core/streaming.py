"""
流式输出支持 —— Writer 逐章节 yield + Pipeline 进度回调 + SSE 端点。

设计：
  - StreamCallback: 回调对象，Writer 生成每个章节时调用
  - handle_streaming(): Writer 生成器方法，yield 每个章节内容
  - /stream SSE 端点: 实时推送生成进度到客户端

增强特性：
  - SSE 重连: Last-Event-ID 支持，客户端断线重连后从断点继续
  - 流式指标: events_emitted / events_dropped / avg_latency
  - 背压控制: pause()/resume() 让消费者控制生产速率
"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
from typing import Any

from .fast_json import dumps as _fast_dumps


class StreamEvent:
    """流式事件"""
    __slots__ = ("event_type", "data", "timestamp", "section_index", "total_sections", "event_id")

    _id_counter = 0
    _id_lock = threading.Lock()

    def __init__(self, event_type: str, data: Any,
                 section_index: int = -1, total_sections: int = 0,
                 event_id: int = 0):
        self.event_type = event_type      # "start" | "section" | "progress" | "complete" | "error" | "chunk"
        self.data = data
        self.timestamp = time.time()
        self.section_index = section_index
        self.total_sections = total_sections
        self.event_id = event_id or self._next_id()

    @classmethod
    def _next_id(cls) -> int:
        with cls._id_lock:
            cls._id_counter += 1
            return cls._id_counter

    def to_sse(self) -> str:
        """转为 SSE 格式字符串（含 id 字段支持重连）"""
        payload = {
            "type": self.event_type,
            "data": self.data,
            "ts": self.timestamp,
            "section": self.section_index,
            "total": self.total_sections,
        }
        return f"id: {self.event_id}\ndata: {_fast_dumps(payload)}\n\n"

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            "data": self.data,
            "ts": self.timestamp,
            "section": self.section_index,
            "total": self.total_sections,
            "id": self.event_id,
        }


class StreamMetrics:
    """流式指标采集（线程安全）"""

    def __init__(self):
        self._lock = threading.Lock()
        self.events_emitted = 0
        self.events_dropped = 0
        self.chunks_emitted = 0
        self.sections_emitted = 0
        self._start_time = time.time()
        self._first_event_time: float | None = None
        self._last_event_time: float | None = None

    def record_event(self, event_type: str):
        with self._lock:
            now = time.time()
            if self._first_event_time is None:
                self._first_event_time = now
            self._last_event_time = now
            self.events_emitted += 1
            if event_type == "chunk":
                self.chunks_emitted += 1
            elif event_type == "section":
                self.sections_emitted += 1

    def record_drop(self):
        with self._lock:
            self.events_dropped += 1

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = time.time() - self._start_time
            avg_latency = 0.0
            if self._first_event_time and self._last_event_time:
                avg_latency = (self._last_event_time - self._first_event_time) / max(self.events_emitted, 1)
            return {
                "events_emitted": self.events_emitted,
                "events_dropped": self.events_dropped,
                "chunks_emitted": self.chunks_emitted,
                "sections_emitted": self.sections_emitted,
                "elapsed": elapsed,
                "events_per_sec": self.events_emitted / elapsed if elapsed > 0 else 0.0,
                "avg_latency": avg_latency,
            }


class StreamCallback:
    """
    流式回调 —— Writer 生成每个章节时调用。

    用法:
        callback = StreamCallback()
        writer.handle_streaming(msg, callback)
        for event in callback:
            print(event)

    增强特性:
        - 背压控制: pause()/resume() 让消费者控制生产速率
        - 流式指标: metrics 属性实时反映 events/sec、延迟等
        - SSE 重连: get_events_since(event_id) 返回指定 ID 之后的事件
    """

    def __init__(self, max_queue_size: int = 100):
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._section_count = 0
        self._total_sections = 0
        self._start_time = time.time()
        self.metrics = StreamMetrics()
        # 背压控制（Condition 使 pause 真正阻塞生产者，避免忙等自旋）
        self._pause_condition = threading.Condition()
        self._paused = False
        # 事件历史（用于 SSE 重连，保留最近 N 个事件）
        self._history: list[StreamEvent] = []
        self._history_max = 500

    def on_start(self, total_sections: int, title: str = ""):
        """文档生成开始"""
        self._total_sections = total_sections
        self._emit("start", {"title": title, "total_sections": total_sections})

    def on_section(self, section_index: int, section_name: str, content: str):
        """一个章节生成完成"""
        with self._lock:
            self._section_count += 1
        self._emit("section", {
            "section_name": section_name,
            "content": content,
            "char_count": len(content),
        }, section_index=section_index, total_sections=self._total_sections)

    def on_chunk(self, chunk: str, section_index: int = -1):
        """LLM token-level chunk —— 实时传播生成中的文本片段"""
        if chunk:
            self._emit("chunk", {
                "text": chunk,
                "char_count": len(chunk),
            }, section_index=section_index, total_sections=self._total_sections)

    def on_progress(self, current: int, total: int, message: str = ""):
        """进度更新"""
        self._emit("progress", {
            "current": current,
            "total": total,
            "message": message,
            "elapsed": time.time() - self._start_time,
        }, section_index=current, total_sections=total)

    def on_complete(self, full_content: str, stats: dict = None):
        """文档生成完成"""
        self._emit("complete", {
            "content": full_content,
            "char_count": len(full_content),
            "stats": stats or {},
            "elapsed": time.time() - self._start_time,
        })
        self.close()

    def on_error(self, error: str):
        """生成出错"""
        self._emit("error", {"error": error})
        self.close()

    def _emit(self, event_type: str, data: Any,
              section_index: int = -1, total_sections: int = 0):
        if self._closed.is_set():
            return
        # 背压：如果消费者暂停，阻塞等待恢复（close 时 notify 唤醒退出）
        with self._pause_condition:
            while self._paused and not self._closed.is_set():
                self._pause_condition.wait()
        if self._closed.is_set():
            return

        event = StreamEvent(event_type, data, section_index, total_sections)
        self.metrics.record_event(event_type)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 队列满时丢弃最旧的事件
            self.metrics.record_drop()
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except queue.Empty:
                pass
        # 记录历史（用于 SSE 重连）
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_max:
                self._history = self._history[-self._history_max:]

    # ── 背压控制 ──────────────────────────────

    def pause(self):
        """暂停事件生产（消费者处理不过来时调用）"""
        with self._pause_condition:
            self._paused = True

    def resume(self):
        """恢复事件生产"""
        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()

    def is_paused(self) -> bool:
        with self._pause_condition:
            return self._paused

    # ── SSE 重连支持 ──────────────────────────

    def get_events_since(self, last_event_id: int) -> list[StreamEvent]:
        """获取指定 event_id 之后的所有事件（用于 SSE 重连）"""
        with self._lock:
            return [e for e in self._history if e.event_id > last_event_id]

    def close(self):
        self._closed.set()
        with self._pause_condition:
            self._pause_condition.notify_all()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)  # sentinel

    def __iter__(self):
        """迭代事件流"""
        while not self._closed.is_set() or not self._queue.empty():
            try:
                event = self._queue.get(timeout=1.0)
                if event is None:
                    break
                yield event
            except queue.Empty:
                continue

    def is_closed(self) -> bool:
        return self._closed.is_set()

    def get_events(self) -> list[StreamEvent]:
        """非阻塞获取所有已缓冲事件"""
        events = []
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                if event is not None:
                    events.append(event)
            except queue.Empty:
                break
        return events

    def get_metrics(self) -> dict:
        """获取流式指标快照"""
        return self.metrics.snapshot()


# ── 全局 StreamCallback 注册表（支持 SSE 重连） ──────────

_callback_registry: dict[str, StreamCallback] = {}
_registry_lock = threading.Lock()


def register_callback(task_id: str, callback: StreamCallback):
    """注册 StreamCallback 到全局注册表（供 SSE 重连查找）"""
    with _registry_lock:
        _callback_registry[task_id] = callback


def get_callback(task_id: str) -> StreamCallback | None:
    """获取已注册的 StreamCallback（重连时使用）"""
    with _registry_lock:
        return _callback_registry.get(task_id)


def unregister_callback(task_id: str):
    """注销已完成的 StreamCallback"""
    with _registry_lock:
        _callback_registry.pop(task_id, None)
