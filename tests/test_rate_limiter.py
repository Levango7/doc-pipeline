"""RateLimiter — burst, acquire, timeout, sliding window"""
import time
import pytest
from threading import Thread, Event


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
        assert rl.available == 0
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
        rl = rate_limiters.get_or_create("rl_hlth", rate=10, burst=5)
        h = rate_limiters.all()
        assert "rl_hlth" in h
        assert "available_tokens" in h["rl_hlth"]
        assert "rate" in h["rl_hlth"]