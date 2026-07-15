"""
流式输出支持 —— Writer 逐章节 yield + Pipeline 进度回调 + SSE 端点。

设计：
  - StreamCallback: 回调对象，Writer 生成每个章节时调用
  - handle_streaming(): Writer 生成器方法，yield 每个章节内容
  - /stream SSE 端点: 实时推送生成进度到客户端
"""
from __future__ import annotations

import json
import time
import threading
import queue
from typing import Optional, Callable, Generator, Any


class StreamEvent:
    """流式事件"""
    __slots__ = ("event_type", "data", "timestamp", "section_index", "total_sections")

    def __init__(self, event_type: str, data: Any,
                 section_index: int = -1, total_sections: int = 0):
        self.event_type = event_type      # "start" | "section" | "progress" | "complete" | "error"
        self.data = data
        self.timestamp = time.time()
        self.section_index = section_index
        self.total_sections = total_sections

    def to_sse(self) -> str:
        """转为 SSE 格式字符串"""
        payload = {
            "type": self.event_type,
            "data": self.data,
            "ts": self.timestamp,
            "section": self.section_index,
            "total": self.total_sections,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def to_dict(self) -> dict:
        return {
            "type": self.event_type,
            "data": self.data,
            "ts": self.timestamp,
            "section": self.section_index,
            "total": self.total_sections,
        }


class StreamCallback:
    """
    流式回调 —— Writer 生成每个章节时调用。

    用法:
        callback = StreamCallback()
        writer.handle_streaming(msg, callback)
        for event in callback:
            print(event)
    """

    def __init__(self, max_queue_size: int = 100):
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_size)
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._section_count = 0
        self._total_sections = 0
        self._start_time = time.time()

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
        event = StreamEvent(event_type, data, section_index, total_sections)
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # 队列满时丢弃最旧的事件
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except queue.Empty:
                pass

    def close(self):
        self._closed.set()
        try:
            self._queue.put_nowait(None)  # sentinel
        except queue.Full:
            pass

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
