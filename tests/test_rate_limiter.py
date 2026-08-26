"""RateLimiter — burst, acquire, timeout, sliding window"""
import threading
import time

import pipeline_core.rate_limiter as rl_mod
from pipeline_core.rate_limiter import RateLimiter


class TestRateLimiterBasics:
    """限流器核心行为"""

    def test_initial_permits(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_test", rate=10, burst=5)
        assert rl.available == 5.0

    def test_acquire_consume_permits(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_acquire", rate=100, burst=10)
        ok = rl.acquire(1.0, block=False)
        assert ok is True
        assert rl.available < 10.0

    def test_acquire_block_timeout(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_timeout", rate=0.1, burst=1)
        ok = rl.acquire(1.0, block=True, timeout=2)
        assert ok is True
        # 第二次应超时（burst 耗尽，refill 慢）
        ok = rl.acquire(1.0, block=True, timeout=0.5)
        assert ok is False, "should timeout when burst exhausted"

    def test_no_block_when_burst_full(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_noblock", rate=100, burst=5)
        for _ in range(5):
            ok = rl.acquire(1.0, block=False)
            assert ok is True
        # burst 耗尽
        ok = rl.acquire(1.0, block=False)
        assert ok is False

    def test_rate_refill(self, rate_limiters):
        """随时间恢复 tokens"""
        rl = rate_limiters.get_or_create("rl_refill", rate=10, burst=5)
        for _ in range(5):
            rl.acquire(1.0, block=False)
        # 容忍微秒级 refill 误差（flaky fix: == 0 → < 0.01）
        assert rl.available < 0.01
        time.sleep(0.6)
        assert rl.available >= 1.0


class TestRateLimiterRegistry:
    """注册表行为"""

    def test_get_or_create(self, rate_limiters):
        rl1 = rate_limiters.get_or_create("rl_reg")
        rl2 = rate_limiters.get_or_create("rl_reg")
        assert rl1 is rl2

    def test_list(self, rate_limiters):
        rate_limiters.get_or_create("rl_a")
        rate_limiters.get_or_create("rl_b")
        names = list(rate_limiters.all().keys())
        assert "rl_a" in names
        assert "rl_b" in names

    def test_health_summary(self, rate_limiters):
        rate_limiters.get_or_create("rl_hlth", rate=10, burst=5)
        h = rate_limiters.all()
        assert "rl_hlth" in h
        assert "available_tokens" in h["rl_hlth"]
        assert "rate" in h["rl_hlth"]


class TestRateLimiterStats:
    """total_blocked 统计正确性（防 Bug 5 回归）"""

    def test_blocked_not_double_counted(self, rate_limiters):
        """阻塞 acquire 超时时 total_blocked 不能双倍计数"""
        rl = rate_limiters.get_or_create("rl_stats", rate=0.01, burst=1)
        # 耗尽 burst
        rl.acquire(1.0, block=False)
        # 阻塞获取，应超时
        ok = rl.acquire(1.0, block=True, timeout=0.1)
        assert ok is False
        s = rl.stats()
        assert s["total_blocked"] == 1, f"expected 1, got {s['total_blocked']}"

    def test_blocked_count_immediate_fail(self, rate_limiters):
        """非阻塞 acquire 失败时 total_blocked 应为 1"""
        rl = rate_limiters.get_or_create("rl_noblock_stats", rate=0.01, burst=1)
        rl.acquire(1.0, block=False)
        ok = rl.acquire(1.0, block=False)
        assert ok is False
        s = rl.stats()
        assert s["total_blocked"] == 1, f"expected 1, got {s['total_blocked']}"

    def test_acquired_separate_from_blocked(self, rate_limiters):
        """成功获取不增加 blocked 计数"""
        rl = rate_limiters.get_or_create("rl_sep", rate=100, burst=10)
        rl.acquire(1.0, block=False)
        rl.acquire(1.0, block=False)
        s = rl.stats()
        assert s["total_acquired"] == 2
        assert s["total_blocked"] == 0


class _FakeClock:
    def __init__(self, start: float):
        self.now = start

    def monotonic(self) -> float:
        return self.now


class TestRateLimiterClockRollback:
    """monotonic 时间基准 + 回拨防护（防 P1 回归）"""

    def test_rollback_does_not_negative_tokens(self, monkeypatch):
        clock = _FakeClock(1000.0)
        monkeypatch.setattr(rl_mod, "time", clock)
        rl = RateLimiter(rate=10, burst=5, name="rl_rollback")

        for _ in range(5):
            assert rl.acquire(1.0, block=False) is True
        assert rl.available < 0.01

        tokens_before = rl._tokens
        clock.now -= 100.0
        assert rl.available >= 0.0
        assert rl._tokens >= tokens_before - 1e-9

        for _ in range(20):
            clock.now -= 10.0
            assert rl.available >= 0.0
        assert rl.available >= 0.0

    def test_recovery_after_rollback(self, monkeypatch):
        clock = _FakeClock(2000.0)
        monkeypatch.setattr(rl_mod, "time", clock)
        rl = RateLimiter(rate=10, burst=5, name="rl_recover")

        for _ in range(5):
            rl.acquire(1.0, block=False)
        assert rl.available < 0.01

        clock.now -= 50.0
        clock.now += 1.0
        available = rl.available
        assert 0.0 <= available < 11.0
        clock.now += 2.0
        assert rl.available > available or rl.available >= 5.0
        assert rl.acquire(1.0, block=False) is True

    def test_blocking_acquire_deadline_monotonic(self, monkeypatch):
        clock = _FakeClock(3000.0)
        monkeypatch.setattr(rl_mod, "time", clock)
        rl = RateLimiter(rate=0.001, burst=1, name="rl_deadline")

        assert rl.acquire(1.0, block=False) is True

        stop = threading.Event()

        def _advance():
            while not stop.is_set():
                clock.now += 0.05
                time.sleep(0.005)

        t = threading.Thread(target=_advance, daemon=True)
        t.start()
        try:
            ok = rl.acquire(1.0, block=True, timeout=0.2)
        finally:
            stop.set()
            t.join(timeout=1)
        assert ok is False
        assert rl.stats()["total_blocked"] == 1


class TestRateLimiterRegistryUpdate:
    """Registry.get_or_create 热更新语义（防 P2 回归）"""

    def test_get_or_create_updates_existing_params(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_hot", rate=5, burst=4)
        rl2 = rate_limiters.get_or_create("rl_hot", rate=50, burst=40)
        assert rl is rl2
        assert rl.rate == 50
        assert rl.burst == 40

    def test_get_or_create_no_args_keeps_config(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_keep", rate=7, burst=3)
        rl2 = rate_limiters.get_or_create("rl_keep")
        assert rl is rl2
        assert rl.rate == 7
        assert rl.burst == 3

    def test_get_or_create_partial_update(self, rate_limiters):
        rl = rate_limiters.get_or_create("rl_partial", rate=7, burst=3)
        rl2 = rate_limiters.get_or_create("rl_partial", burst=30)
        assert rl is rl2
        assert rl.rate == 7
        assert rl.burst == 30
