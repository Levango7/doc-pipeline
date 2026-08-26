"""CircuitBreaker — OPEN/CLOSED/HALF_OPEN, backoff, registry"""
import time

import pipeline_core.circuit_breaker as cb_mod
from pipeline_core.circuit_breaker import CircuitBreaker


class TestCircuitBreakerBasics:
    """熔断器核心行为"""

    def test_initial_state_closed(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("test_cb")
        assert cb.state.name == "CLOSED"

    def test_open_after_threshold(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("open_cb", failure_threshold=3, recovery_timeout=60)

        for _i in range(3):
            cb.record_failure()

        assert cb.state.name == "OPEN", f"should open after 3 failures, got {cb.state}"

    def test_half_open_after_cooldown(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("half_cb", failure_threshold=2, recovery_timeout=0.2)

        for _ in range(2):
            cb.record_failure()

        assert cb.state.name == "OPEN"
        time.sleep(0.3)
        # allow_request 触发 OPEN→HALF_OPEN 转换
        allowed = cb.allow_request()
        assert cb.state.name == "HALF_OPEN", f"should become HALF_OPEN, got {cb.state}"
        assert allowed is True

    def test_close_after_success_in_half_open(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("close_cb", failure_threshold=2, recovery_timeout=0.2)

        for _ in range(2):
            cb.record_failure()

        time.sleep(0.3)
        cb.allow_request()  # → HALF_OPEN
        cb.record_success()  # → CLOSED
        assert cb.state.name == "CLOSED"

    def test_backoff_jitter(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("backoff_cb", failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        d = cb.to_dict()
        assert "last_failure_time" in d
        assert d["state"] == "open"


class TestCircuitBreakerRegistry:
    """注册表行为"""

    def test_get_or_create(self, circuit_breakers):
        cb1 = circuit_breakers.get_or_create("reg_cb")
        cb2 = circuit_breakers.get_or_create("reg_cb")
        assert cb1 is cb2

    def test_list(self, circuit_breakers):
        circuit_breakers.get_or_create("cb_a")
        circuit_breakers.get_or_create("cb_b")
        names = list(circuit_breakers.all().keys())
        assert "cb_a" in names
        assert "cb_b" in names

    def test_reset(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("reset_cb", failure_threshold=1, recovery_timeout=10)
        cb.record_failure()
        assert cb.state.name == "OPEN"
        circuit_breakers.reset("reset_cb")
        assert cb.state.name == "CLOSED"

    def test_health_summary(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("hlth_cb")
        cb.record_failure()
        h = circuit_breakers.all()
        assert "hlth_cb" in h
        assert "state" in h["hlth_cb"]

    def test_get_or_create_updates_existing_params(self, circuit_breakers):
        """已存在实例应用新传入阈值（热更新，防 P2 回归）"""
        cb = circuit_breakers.get_or_create("hot_cb", failure_threshold=5, recovery_timeout=60)
        cb2 = circuit_breakers.get_or_create("hot_cb", failure_threshold=2, recovery_timeout=1.5, half_open_max_tests=3)
        assert cb is cb2
        assert cb.failure_threshold == 2
        assert cb.recovery_timeout == 1.5
        assert cb.half_open_max_tests == 3

    def test_get_or_create_no_kwargs_keeps_config(self, circuit_breakers):
        cb = circuit_breakers.get_or_create("keep_cb", failure_threshold=3, recovery_timeout=30, half_open_max_tests=2)
        cb2 = circuit_breakers.get_or_create("keep_cb")
        assert cb is cb2
        assert cb.failure_threshold == 3
        assert cb.recovery_timeout == 30
        assert cb.half_open_max_tests == 2

    def test_updated_threshold_takes_effect_immediately(self, circuit_breakers):
        """热更新后的 failure_threshold 立即生效"""
        cb = circuit_breakers.get_or_create("effective_cb", failure_threshold=10)
        for _ in range(4):
            cb.record_failure()
        assert cb.state.name == "CLOSED"
        circuit_breakers.get_or_create("effective_cb", failure_threshold=2)
        cb.record_failure()
        assert cb.state.name == "OPEN"


class _FakeClock:
    def __init__(self, start: float):
        self.now = start

    def monotonic(self) -> float:
        return self.now


class TestCircuitBreakerMonotonicClock:
    """monotonic 时间基准：墙钟回拨不影响 OPEN→HALF_OPEN 判定（防 P1 回归）"""

    def test_half_open_transition_uses_monotonic_duration(self, monkeypatch):
        clock = _FakeClock(5000.0)
        monkeypatch.setattr(cb_mod, "time", clock)
        cb = CircuitBreaker("mono_cb", failure_threshold=1, recovery_timeout=10.0)

        cb.record_failure()
        assert cb.state.name == "OPEN"

        clock.now += 9.9
        assert cb.allow_request() is False
        assert cb.state.name == "OPEN"

        clock.now += 0.2
        assert cb.allow_request() is True
        assert cb.state.name == "HALF_OPEN"

    def test_clock_rollback_keeps_open(self, monkeypatch):
        clock = _FakeClock(6000.0)
        monkeypatch.setattr(cb_mod, "time", clock)
        cb = CircuitBreaker("rollback_cb", failure_threshold=1, recovery_timeout=30.0)

        cb.record_failure()
        assert cb.state.name == "OPEN"

        clock.now -= 1000.0
        assert cb.allow_request() is False
        assert cb.state.name == "OPEN"

    def test_no_real_sleep_needed_for_transition(self, monkeypatch):
        """单调时长到达即可转换，无需真实等待（旧 wall-clock 实现需 sleep 60s）"""
        clock = _FakeClock(100.0)
        monkeypatch.setattr(cb_mod, "time", clock)
        cb = CircuitBreaker("nosleep_cb", failure_threshold=1, recovery_timeout=3600.0)

        cb.record_failure()
        clock.now += 3600.0
        assert cb.allow_request() is True
        assert cb.state.name == "HALF_OPEN"
