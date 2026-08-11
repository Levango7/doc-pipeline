"""AlertManager — 告警机制测试"""
import time

import pytest

from pipeline_core.alert_manager import alert, clear_alerts, get_alerts


@pytest.fixture(autouse=True)
def clean():
    clear_alerts()
    yield
    clear_alerts()


class TestAlert:
    def test_basic_alert(self):
        alert("warning", "circuit_breaker", "test agent 熔断")
        alerts = get_alerts()
        assert len(alerts) == 1
        assert alerts[0]["level"] == "warning"
        assert alerts[0]["category"] == "circuit_breaker"

    def test_level_filter(self):
        alert("info", "test", "a")
        alert("warning", "test", "b")
        alert("critical", "test", "c")
        warnings = get_alerts(level="warning")
        assert all(a["level"] == "warning" for a in warnings)
        assert len(warnings) == 1

    def test_category_filter(self):
        alert("warning", "circuit_breaker", "a")
        alert("warning", "dlq", "b")
        cb_alerts = get_alerts(category="circuit_breaker")
        assert len(cb_alerts) == 1
        assert cb_alerts[0]["category"] == "circuit_breaker"

    def test_since_filter(self):
        alert("info", "test", "old")
        cutoff = time.time() + 0.1
        time.sleep(0.15)
        alert("info", "test", "new")
        recent = get_alerts(since=cutoff)
        assert len(recent) == 1
        assert recent[0]["message"] == "new"

    def test_limit(self):
        for i in range(10):
            alert("info", "test", f"alert {i}")
        assert len(get_alerts(limit=5)) == 5

    def test_extra_data(self):
        alert("error", "rate_limit", "限流", extra={"agent": "writer", "rate": 5})
        alerts = get_alerts()
        assert alerts[0]["extra"]["agent"] == "writer"
        assert alerts[0]["extra"]["rate"] == 5

    def test_buffer_cap(self):
        for i in range(250):
            alert("info", "test", f"alert {i}")
        assert len(get_alerts(limit=300)) <= 200

    def test_clear(self):
        alert("info", "test", "a")
        clear_alerts()
        assert len(get_alerts()) == 0
