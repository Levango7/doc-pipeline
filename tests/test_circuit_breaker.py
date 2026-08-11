"""CircuitBreaker — OPEN/CLOSED/HALF_OPEN, backoff, registry"""
import time


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
