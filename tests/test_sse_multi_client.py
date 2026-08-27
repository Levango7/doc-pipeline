"""SSE 多客户端并发订阅回归 —— 游标式只读历史，互不瓜分

背景（架构债修复）：
  原 ``AdminAPIHandler._pump_sse`` 用 ``StreamCallback.get_events()``（破坏性出队）
  拉取事件。多个 SSE 客户端并发订阅同一任务时，第二个客户端会"抢走"第一个
  尚未消费的事件，导致各自只拿到残缺流。
  修复后 ``_pump_sse`` 改为游标式 ``get_events_since(cursor)``：每个客户端持独立
  event_id 游标，从只读事件历史读取，互不干扰。

本文件守护该不变量：
  - 多个游标读取者各自拿到完整、有序的事件流；
  - 读取不消费历史（后到者/重复读不受影响）；
  - 对照：破坏性 get_events() 会瓜分队列（说明 pump 不能用它）。
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.streaming import StreamCallback  # noqa: E402


def _emit_n(cb: StreamCallback, n: int):
    """发出 1 个 start + n 个 section 事件（不用 complete，避免 close）"""
    cb.on_start(total_sections=n, title="多客户端测试")
    for i in range(n):
        cb.on_section(i + 1, f"章节 {i + 1}", f"内容 {i + 1}")


class TestMultiClientCursorReads:
    """游标式 get_events_since 多客户端安全"""

    def test_multiple_cursor_readers_each_get_full_stream(self):
        """K 个客户端各自从 cursor=0 读，均拿到全部事件（互不瓜分）"""
        cb = StreamCallback()
        n = 20
        _emit_n(cb, n)
        total_events = n + 1  # start + n sections

        results: list[list[int]] = []
        lock = threading.Lock()

        def reader():
            ids = [e.event_id for e in cb.get_events_since(0)]
            with lock:
                results.append(ids)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        for ids in results:
            assert len(ids) == total_events, \
                f"客户端应拿到全部 {total_events} 个事件，实际 {len(ids)}"
            assert ids == sorted(ids), "事件流必须按 event_id 有序"

    def test_reads_do_not_consume_history(self):
        """游标读是只读：读过后其他客户端仍能拿到完整流"""
        cb = StreamCallback()
        _emit_n(cb, 10)
        total = 11

        first = cb.get_events_since(0)
        second = cb.get_events_since(0)
        third = cb.get_events_since(0)
        assert len(first) == len(second) == len(third) == total

    def test_late_joiner_resumes_from_cursor(self):
        """后到客户端从中间游标续读，只拿剩余事件；先到者不受影响"""
        cb = StreamCallback()
        _emit_n(cb, 10)
        all_events = cb.get_events_since(0)
        midpoint = all_events[5].event_id

        late = cb.get_events_since(midpoint)
        assert len(late) == len(all_events) - 6  # 游标之后的事件
        assert all(e.event_id > midpoint for e in late)

        # 先到者重新读仍拿全量
        assert len(cb.get_events_since(0)) == len(all_events)

    def test_concurrent_readers_during_production(self):
        """生产者持续发事件，多个读者并发游标跟进；生产结束后各自都能读全有序流"""
        cb = StreamCallback()
        n = 50
        done = threading.Event()

        def producer():
            _emit_n(cb, n)
            done.set()

        def reader():
            cursor = 0
            while True:
                events = cb.get_events_since(cursor)
                if events:
                    cursor = events[-1].event_id
                if done.is_set():
                    # 收尾再读一次，补齐生产末尾的事件
                    cb.get_events_since(cursor)
                    break

        prod = threading.Thread(target=producer)
        readers = [threading.Thread(target=reader) for _ in range(4)]
        prod.start()
        for t in readers:
            t.start()
        prod.join()
        for t in readers:
            t.join()

        # 并发读取未破坏历史：事后每个客户端仍拿到完整有序流
        expected_total = n + 1
        for _ in range(4):
            ids = [e.event_id for e in cb.get_events_since(0)]
            assert len(ids) == expected_total
            assert ids == sorted(ids)


class TestDestructiveGetEventsSplits:
    """对照：破坏性 get_events() 会瓜分队列 —— 说明 pump 必须用游标读"""

    def test_two_drains_partition_the_queue(self):
        cb = StreamCallback()
        _emit_n(cb, 10)
        total = 11

        first = cb.get_events()
        second = cb.get_events()
        # 出队语义：第一次拿走全部，第二次为空（多客户端会互相抢空）
        assert len(first) + len(second) == total
        assert len(second) == 0, "破坏性出队后第二个客户端拿不到任何事件"
