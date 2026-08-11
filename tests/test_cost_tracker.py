"""CostTracker — LLM 成本追踪测试"""
import pytest

from pipeline_core.cost_tracker import (
    CostTracker,
    calc_cost,
    estimate_tokens,
    reset_cost_tracker,
)


@pytest.fixture
def tracker(tmp_path):
    reset_cost_tracker()
    return CostTracker(db_path=str(tmp_path / "test_cost.db"))


class TestEstimateTokens:
    def test_english(self):
        assert estimate_tokens("hello world") == 3

    def test_chinese(self):
        assert estimate_tokens("你好世界") == 3  # 4 chars // 2 + 1

    def test_mixed(self):
        tokens = estimate_tokens("hello 你好")
        assert tokens > 0

    def test_empty(self):
        assert estimate_tokens("") == 0


class TestCalcCost:
    def test_known_provider(self):
        cost = calc_cost("cloudflare", 1000, 500)
        assert cost > 0

    def test_ollama_free(self):
        cost = calc_cost("ollama", 10000, 5000)
        assert cost == 0

    def test_unknown_provider_uses_default(self):
        cost = calc_cost("unknown_provider", 1000, 500)
        assert cost > 0


class TestCostTracker:
    def test_record_and_total(self, tracker):
        tracker.record("cloudflare", 500, 200, cost=0.01)
        tracker.record("glm", 300, 100, cost=0.005)
        assert tracker.total_cost() == pytest.approx(0.015)

    def test_record_call_auto_estimate(self, tracker):
        tracker.record_call(
            provider="cloudflare",
            messages=[{"role": "user", "content": "写一篇关于 Python 的文章"}],
            response="Python 是一种编程语言...",
        )
        assert tracker.total_cost() > 0

    def test_budget_not_exceeded(self, tracker):
        tracker.set_budget(10.0)
        tracker.record("cloudflare", 500, 200, cost=0.01)
        assert tracker.check_budget() is True

    def test_budget_exceeded(self, tracker):
        tracker.set_budget(0.005)
        tracker.record("cloudflare", 500, 200, cost=0.01)
        assert tracker.check_budget() is False

    def test_no_budget_unlimited(self, tracker):
        assert tracker.check_budget() is True

    def test_stats_by_provider(self, tracker):
        tracker.record("cloudflare", 500, 200, cost=0.01)
        tracker.record("cloudflare", 300, 100, cost=0.005)
        tracker.record("glm", 200, 50, cost=0.003)
        stats = tracker.stats()
        assert "cloudflare" in stats
        assert stats["cloudflare"]["calls"] == 2
        assert stats["glm"]["calls"] == 1

    def test_stats_by_task(self, tracker):
        tracker.record("cloudflare", 500, 200, cost=0.01, task_id="t1")
        tracker.record("glm", 300, 100, cost=0.005, task_id="t1")
        stats = tracker.stats_by_task("t1")
        assert stats["total_cost"] == pytest.approx(0.015)
        assert "cloudflare" in stats["by_provider"]
        assert "glm" in stats["by_provider"]

    def test_summary(self, tracker):
        tracker.set_budget(1.0)
        tracker.record("cloudflare", 500, 200, cost=0.01)
        s = tracker.summary()
        assert s["total_cost"] == pytest.approx(0.01)
        assert s["budget"] == 1.0
        assert s["budget_remaining"] == pytest.approx(0.99)
        assert s["budget_exceeded"] is False

    def test_cleanup(self, tracker):
        tracker.record("cloudflare", 500, 200, cost=0.01)
        tracker.cleanup(max_age_days=0)
        assert tracker.total_cost() == 0
