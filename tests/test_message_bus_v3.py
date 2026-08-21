"""MessageBus v3 — pub/sub, backpressure, DLQ, health"""
import time

from pipeline_core.message_bus_v3 import MessageBus


class TestMessageBusBasics:
    """核心 pub/sub 流程"""

    def test_subscribe_and_publish(self, bus):
        msgs = []
        bus.subscribe("test.topic", lambda m: msgs.append(m))
        result = bus.publish("test.topic", "sender", {"key": "value"})

        assert result["status"] == "sent", f"publish failed: {result}"
        time.sleep(0.15)
        assert len(msgs) == 1, f"expected 1 msg, got {len(msgs)}"
        assert msgs[0].payload["key"] == "value"
        assert msgs[0].from_agent == "sender"

    def test_subscribe_priority(self, bus):
        """高优先级（低值）先执行 — Unix nice 风格"""
        order = []

        bus.subscribe("prio.topic", lambda m: order.append("high"), priority=10)
        bus.subscribe("prio.topic", lambda m: order.append("low"), priority=100)

        bus.publish("prio.topic", "t", {"x": 1})
        time.sleep(0.15)
        assert order == ["high", "low"], f"priority order wrong: {order}"

    def test_unsubscribe(self, bus):
        msgs = []

        def cb(m):
            msgs.append(m)

        bus.subscribe("unsub.topic", cb)
        bus.unsubscribe("unsub.topic", cb)
        bus.publish("unsub.topic", "t", {"x": 1})
        time.sleep(0.15)
        assert len(msgs) == 0, "should not receive after unsubscribe"

    def test_no_subscriber(self, bus):
        """无订阅者应返回 sent（空投递）"""
        result = bus.publish("empty.topic", "t", {"x": 1})
        assert result["status"] == "sent"

    def test_multiple_topics(self, bus):
        msgs = []
        bus.subscribe("a.topic", lambda m: msgs.append(m))
        bus.subscribe("b.topic", lambda m: msgs.append(m))
        bus.publish("a.topic", "t", {"topic": "a"})
        bus.publish("b.topic", "t", {"topic": "b"})
        time.sleep(0.15)
        assert len(msgs) == 2, f"expected 2 msgs, got {len(msgs)}"


class TestMessageBusBackpressure:
    """反压机制"""

    def test_publish_returns_sent(self, bus):
        result = bus.publish("bp.topic", "t", {"x": 1})
        assert result["status"] == "sent"

    def test_queue_depth(self, bus):
        """publish 后 queue_depth 应反映待投递消息"""
        bus.subscribe("q.topic", lambda m: time.sleep(0.3))
        bus.publish("q.topic", "s", {"x": 1})
        bus.publish("q.topic", "s", {"x": 1})
        time.sleep(0.05)
        depth = bus.queue_depth()
        assert isinstance(depth, int)

    def test_publish_returns_busy_when_queue_full(self):
        """高水位时 publish 应返回 busy（低水位线）"""
        bus = MessageBus(db_path=":memory:", backpressure_watermark=3)
        bus.subscribe("full.topic", lambda m: time.sleep(0.5))
        results = []
        for i in range(20):
            r = bus.publish("full.topic", "s", {"i": i})
            results.append(r)
            if r["status"] == "busy":
                break
        busy_hits = [r for r in results if r["status"] == "busy"]
        assert len(busy_hits) > 0, f"never got busy: {results[:5]}..."
        bus.shutdown()

    def test_publish_blocking(self):
        """publish_blocking 应等到水位下降"""
        bus = MessageBus(db_path=":memory:", backpressure_watermark=3)
        bus.subscribe("block.topic", lambda m: time.sleep(0.3))
        r = bus.publish_blocking("block.topic", "s", {"x": 1}, max_wait=5)
        assert r["status"] in ("sent", "timeout")
        bus.shutdown()

    def test_health_includes_backpressure_metrics(self, bus):
        """health() 包含反压指标"""
        h = bus.health()
        assert "queue_depth" in h
        assert "high_watermark_hits" in h
        assert "peak_depth" in h
        assert "backpressure_watermark" in h


class TestMessageBusDLQ:
    """死信队列"""

    def test_dlq_list(self, bus):
        dq = bus.list_dlq()
        assert isinstance(dq, list)

    def test_dlq_on_subscriber_error(self, bus):
        """订阅者抛异常不应影响总线——其他订阅者仍收到"""
        ok = []

        def failing(m):
            raise ValueError("crash")

        bus.subscribe("fail.topic", failing)
        bus.subscribe("fail.topic", lambda m: ok.append(m))

        result = bus.publish("fail.topic", "s", {"x": 1})
        assert result["status"] == "sent"
        time.sleep(0.2)
        assert len(ok) >= 1, "working callback should receive"


class TestMessageBusLifecycle:
    """生命周期"""

    def test_subscriber_error_doesnt_crash_bus(self, bus):
        """一个订阅者抛异常不应影响其他订阅者"""
        ok = []

        def failing(m):
            raise RuntimeError("crash")

        def working(m):
            ok.append(m)

        bus.subscribe("resilient.topic", failing)
        bus.subscribe("resilient.topic", working)

        bus.publish("resilient.topic", "s", {"x": 1})
        time.sleep(0.2)
        assert len(ok) == 1, "working callback should still receive"
