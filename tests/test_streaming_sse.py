"""SSE 流式输出与重连测试。"""
import threading
import time

from pipeline_core.streaming import (
    StreamCallback,
    StreamEvent,
    StreamMetrics,
    get_callback,
    register_callback,
    unregister_callback,
)


class TestStreamEvent:
    """StreamEvent 单元测试"""

    def test_event_id_auto_increment(self):
        e1 = StreamEvent("start", {"x": 1})
        e2 = StreamEvent("section", {"x": 2})
        assert e2.event_id > e1.event_id

    def test_to_sse_contains_id(self):
        e = StreamEvent("section", {"content": "hello"}, section_index=2, total_sections=5)
        sse = e.to_sse()
        assert f"id: {e.event_id}" in sse
        assert "data: " in sse
        assert sse.endswith("\n\n")

    def test_to_sse_json_payload(self):
        e = StreamEvent("chunk", {"text": "abc"})
        sse = e.to_sse()
        # orjson 产出紧凑 JSON（无空格），标准 json 带空格，两种均合法
        import json as _json
        data_line = [ln for ln in sse.strip().split("\n") if ln.startswith("data: ")][0]
        payload = _json.loads(data_line[6:])
        assert payload["type"] == "chunk"
        assert payload["data"]["text"] == "abc"

    def test_to_dict_roundtrip(self):
        e = StreamEvent("progress", {"current": 3, "total": 10}, section_index=3, total_sections=10)
        d = e.to_dict()
        assert d["type"] == "progress"
        assert d["data"]["current"] == 3
        assert d["section"] == 3
        assert d["id"] == e.event_id


class TestStreamMetrics:
    """StreamMetrics 指标采集测试"""

    def test_record_event_counts(self):
        m = StreamMetrics()
        m.record_event("section")
        m.record_event("section")
        m.record_event("chunk")
        m.record_event("chunk")
        m.record_event("chunk")
        snap = m.snapshot()
        assert snap["events_emitted"] == 5
        assert snap["sections_emitted"] == 2
        assert snap["chunks_emitted"] == 3

    def test_events_per_sec(self):
        m = StreamMetrics()
        m.record_event("section")
        time.sleep(0.05)
        m.record_event("section")
        snap = m.snapshot()
        assert snap["events_per_sec"] > 0
        assert snap["elapsed"] > 0.04

    def test_record_drop(self):
        m = StreamMetrics()
        m.record_drop()
        m.record_drop()
        assert m.snapshot()["events_dropped"] == 2


class TestStreamCallback:
    """StreamCallback 回调测试"""

    def test_on_start_emits_event(self):
        cb = StreamCallback()
        cb.on_start(total_sections=5, title="Test Doc")
        events = cb.get_events()
        assert len(events) == 1
        assert events[0].event_type == "start"
        assert events[0].data["title"] == "Test Doc"
        assert events[0].data["total_sections"] == 5
        cb.close()

    def test_on_section_emits_event(self):
        cb = StreamCallback()
        cb.on_start(3, "Doc")
        cb.on_section(0, "Intro", "content here")
        events = cb.get_events()
        assert len(events) == 2
        assert events[1].event_type == "section"
        assert events[1].data["section_name"] == "Intro"
        assert events[1].section_index == 0
        cb.close()

    def test_on_chunk_emits_event(self):
        cb = StreamCallback()
        cb.on_chunk("hello ", section_index=0)
        cb.on_chunk("world", section_index=0)
        events = cb.get_events()
        assert len(events) == 2
        assert events[0].event_type == "chunk"
        assert events[0].data["text"] == "hello "
        cb.close()

    def test_on_complete_closes_callback(self):
        cb = StreamCallback()
        cb.on_start(1, "Doc")
        cb.on_complete("full content", {"chars": 12})
        assert cb.is_closed()
        # Events should include start + complete
        # complete closes the callback, so get_events may only get what's buffered
        cb2 = StreamCallback()
        cb2.on_start(1, "Doc")
        cb2.on_complete("full content", {"chars": 12})

    def test_on_error_closes_callback(self):
        cb = StreamCallback()
        cb.on_error("something went wrong")
        assert cb.is_closed()

    def test_backpressure_pause_resume(self):
        cb = StreamCallback()
        cb.pause()
        assert cb.is_paused()
        # Emit should block while paused, so use a thread
        def _emit():
            cb.on_start(1, "Doc")
        t = threading.Thread(target=_emit, daemon=True)
        t.start()
        # Should still be paused, no events yet
        time.sleep(0.1)
        assert len(cb.get_events()) == 0
        cb.resume()
        t.join(timeout=2)
        events = cb.get_events()
        assert len(events) == 1
        cb.close()

    def test_history_for_reconnect(self):
        cb = StreamCallback()
        cb.on_start(3, "Doc")
        cb.on_section(0, "S1", "content1")
        cb.on_section(1, "S2", "content2")
        # History should have 3 events
        assert len(cb._history) == 3
        # Get events since the first event's ID
        first_id = cb._history[0].event_id
        since = cb.get_events_since(first_id)
        assert len(since) == 2  # skip the first, get 2
        cb.close()

    def test_history_max_limit(self):
        cb = StreamCallback(max_queue_size=200)
        cb._history_max = 5  # small limit for testing
        for i in range(10):
            cb.on_chunk(f"chunk_{i}")
        assert len(cb._history) <= 5
        cb.close()

    def test_queue_full_sheds_droppable_keeps_boundary(self):
        """P2 分级丢弃：队列满时丢 chunk/progress，边界事件不丢"""
        cb = StreamCallback(max_queue_size=3)
        cb.on_chunk("a")
        cb.on_chunk("b")
        cb.on_chunk("c")  # 队列满（全是 chunk）
        assert cb.metrics.events_dropped == 0
        cb.on_start(1, "Doc")  # 边界事件入队：淘汰队首单个 chunk 腾位
        snap = cb.metrics.snapshot()
        assert snap["events_dropped"] == 1
        assert snap["drops_by_type"].get("chunk") == 1
        events = cb.get_events()
        assert [e.event_type for e in events] == ["chunk", "chunk", "start"]
        # 被拒的 chunk 仍保留在重连历史中（历史与实时队列解耦）
        assert len(cb._history) == 4
        cb.close()

    def test_queue_full_progress_incoming_dropped_without_touching_queue(self):
        """P2 分级丢弃：可丢类型新事件在队满时被拒，不破坏既有队列内容"""
        cb = StreamCallback(max_queue_size=2)
        cb.on_start(1, "Doc")
        cb.on_section(0, "S1", "c1")
        for _ in range(5):
            cb.on_chunk("x")  # 全部被拒，start/section 保持完整
        snap = cb.metrics.snapshot()
        assert snap["drops_by_type"].get("chunk") == 5
        events = cb.get_events()
        assert [e.event_type for e in events] == ["start", "section"]
        cb.close()


class _CountingCallback(StreamCallback):
    """统计 _emit 进入次数，用于区分挂起与忙等自旋"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.emit_attempts = 0

    def _emit(self, *args, **kwargs):
        self.emit_attempts += 1
        super()._emit(*args, **kwargs)


class TestBackpressureBlocking:
    """pause 后生产者真正挂起（Condition），resume/close 唤醒（防 P2 自旋回归）"""

    def test_pause_blocks_producer_without_spin(self):
        cb = _CountingCallback()
        cb.pause()
        produced = threading.Event()

        def _produce():
            for i in range(200):
                cb.on_chunk(f"chunk_{i}")
            produced.set()

        t = threading.Thread(target=_produce, daemon=True)
        t.start()
        time.sleep(0.15)
        assert not produced.is_set()
        assert cb.emit_attempts <= 2
        assert len(cb.get_events()) == 0

        cb.resume()
        t.join(timeout=5)
        assert produced.is_set()
        assert not t.is_alive()
        assert len(cb._history) == 200
        cb.close()

    def test_close_wakes_paused_producer(self):
        cb = StreamCallback()
        cb.pause()
        done = threading.Event()

        def _emit():
            cb.on_start(1, "Doc")
            done.set()

        t = threading.Thread(target=_emit, daemon=True)
        t.start()
        time.sleep(0.05)
        assert t.is_alive()

        cb.close()
        t.join(timeout=2)
        assert not t.is_alive()
        assert done.is_set()

    def test_resume_notifies_blocked_producer_promptly(self):
        cb = _CountingCallback()
        cb.pause()
        emitted = threading.Event()

        def _emit():
            cb.on_start(2, "Doc")
            emitted.set()

        t = threading.Thread(target=_emit, daemon=True)
        t.start()
        time.sleep(0.05)
        assert not emitted.is_set()

        resume_at = time.monotonic()
        cb.resume()
        t.join(timeout=2)
        assert emitted.is_set()
        assert time.monotonic() - resume_at < 1.0
        cb.close()


class TestSSEReconnection:
    """SSE 重连（Last-Event-ID）测试"""

    def test_callback_registry_register_get(self):
        cb = StreamCallback()
        register_callback("task-001", cb)
        assert get_callback("task-001") is cb
        unregister_callback("task-001")
        assert get_callback("task-001") is None

    def test_callback_registry_overwrite(self):
        cb1 = StreamCallback()
        cb2 = StreamCallback()
        register_callback("task-002", cb1)
        register_callback("task-002", cb2)
        assert get_callback("task-002") is cb2
        unregister_callback("task-002")

    def test_reconnect_replays_missed_events(self):
        """模拟 SSE 重连场景：首次连接生成事件，断线后重连从断点继续"""
        cb = StreamCallback()
        register_callback("task-003", cb)

        # 首次连接：生成 5 个事件
        cb.on_start(3, "Reconnect Test")
        cb.on_section(0, "Intro", "intro content")
        cb.on_section(1, "Body", "body content")

        # 模拟客户端在 event_id=cb._history[1].event_id 处断线
        breakpoint_id = cb._history[1].event_id
        missed = cb.get_events_since(breakpoint_id)
        assert len(missed) == 1  # only the 3rd event (section 1)
        assert missed[0].data["section_name"] == "Body"

        # 继续生成新事件
        cb.on_section(2, "Conclusion", "conclusion content")
        missed2 = cb.get_events_since(breakpoint_id)
        assert len(missed2) == 2  # 3rd and 4th events

        cb.close()
        unregister_callback("task-003")

    def test_reconnect_with_no_history_returns_empty(self):
        """新 callback（无历史）重连应返回空列表"""
        cb = StreamCallback()
        events = cb.get_events_since(0)
        assert events == []
        cb.close()

    def test_reconnect_id_zero_returns_all(self):
        """Last-Event-ID=0 应返回全部历史"""
        cb = StreamCallback()
        cb.on_start(2, "Doc")
        cb.on_section(0, "S1", "c1")
        all_events = cb.get_events_since(0)
        assert len(all_events) == 2
        cb.close()

    def test_registry_thread_safety(self):
        """多线程注册/查找/注销不崩溃"""
        results = []
        def _worker(tid):
            cb = StreamCallback()
            register_callback(f"task-{tid}", cb)
            time.sleep(0.01)
            found = get_callback(f"task-{tid}")
            results.append(found is cb)
            unregister_callback(f"task-{tid}")

        threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert all(results)
