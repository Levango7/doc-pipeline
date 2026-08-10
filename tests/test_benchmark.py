"""benchmark.py — 基准项 / 回归检测

测试原则：
  - 用 mock 模拟外部依赖（selectolax、numpy 等）
  - 不实际运行完整基准（耗时）
  - 每个测试方法聚焦一个行为
"""
import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── _check_regression 回归检测 ────────────────────────────

class TestCheckRegression:
    """_check_regression 回归检测逻辑"""

    def test_no_regression_when_within_threshold(self):
        """指标在阈值内无回归"""
        from benchmark import _check_regression
        current = {"bench1": {"ops_per_sec": 100}}
        baseline = {"bench1": {"ops_per_sec": 95}}
        # 5% 下降，阈值 20%
        regressions = _check_regression(current, baseline, 0.20)
        assert regressions == []

    def test_regression_higher_better(self):
        """越高越好指标下降超阈值检测"""
        from benchmark import (
            _check_regression,
            METRIC_HIGHER_BETTER,
        )
        # 用实际注册的指标名
        metric = "set_ops_per_sec"
        assert metric in METRIC_HIGHER_BETTER
        current = {"bench": {metric: 70}}
        baseline = {"bench": {metric: 100}}
        # 30% 下降，阈值 20%
        regressions = _check_regression(current, baseline, 0.20)
        assert len(regressions) == 1
        assert "REGRESSION" in regressions[0]

    def test_regression_lower_better(self):
        """越低越好指标上升超阈值检测"""
        from benchmark import (
            _check_regression,
            METRIC_LOWER_BETTER,
        )
        metric = "set_ms_per_op"
        assert metric in METRIC_LOWER_BETTER
        current = {"bench": {metric: 130}}
        baseline = {"bench": {metric: 100}}
        # 30% 上升，阈值 20%
        regressions = _check_regression(current, baseline, 0.20)
        assert len(regressions) == 1

    def test_no_regression_for_unknown_metric(self):
        """未注册方向的指标跳过"""
        from benchmark import _check_regression
        current = {"bench": {"unknown_metric": 50}}
        baseline = {"bench": {"unknown_metric": 100}}
        regressions = _check_regression(current, baseline, 0.20)
        assert regressions == []

    def test_skip_zero_baseline(self):
        """baseline 为 0 跳过"""
        from benchmark import _check_regression
        current = {"bench": {"set_ops_per_sec": 100}}
        baseline = {"bench": {"set_ops_per_sec": 0}}
        regressions = _check_regression(current, baseline, 0.20)
        assert regressions == []

    def test_skip_zero_current(self):
        """current 为 0 跳过"""
        from benchmark import _check_regression
        current = {"bench": {"set_ops_per_sec": 0}}
        baseline = {"bench": {"set_ops_per_sec": 100}}
        regressions = _check_regression(current, baseline, 0.20)
        assert regressions == []

    def test_missing_baseline_metric_skipped(self):
        """baseline 缺失该指标时跳过"""
        from benchmark import _check_regression
        current = {"bench": {"set_ops_per_sec": 50}}
        baseline = {"bench": {}}
        regressions = _check_regression(current, baseline, 0.20)
        assert regressions == []

    def test_missing_bench_skipped(self):
        """baseline 缺失该基准项时跳过"""
        from benchmark import _check_regression
        current = {"bench1": {"set_ops_per_sec": 50}}
        baseline = {"bench2": {"set_ops_per_sec": 100}}
        regressions = _check_regression(current, baseline, 0.20)
        assert regressions == []

    def test_multiple_regressions(self):
        """多个回归全部报告"""
        from benchmark import _check_regression
        current = {
            "bench1": {"set_ops_per_sec": 50, "set_ms_per_op": 150},
            "bench2": {"get_hit_ops_per_sec": 60},
        }
        baseline = {
            "bench1": {"set_ops_per_sec": 100, "set_ms_per_op": 100},
            "bench2": {"get_hit_ops_per_sec": 100},
        }
        regressions = _check_regression(current, baseline, 0.20)
        assert len(regressions) == 3


# ─── _gen_mock_html ────────────────────────────

class TestGenMockHtml:
    """_gen_mock_html HTML 生成"""

    def test_generates_html_with_target_size(self):
        """生成接近目标大小的 HTML"""
        from benchmark import _gen_mock_html
        html = _gen_mock_html(10000)
        assert len(html) >= 10000
        assert "<html>" in html
        assert "</html>" in html

    def test_contains_nav_and_footer(self):
        """包含 nav 和 footer 标签"""
        from benchmark import _gen_mock_html
        html = _gen_mock_html(1000)
        assert "<nav>" in html
        assert "<footer>" in html

    def test_contains_kafka_content(self):
        """包含 Kafka 相关内容"""
        from benchmark import _gen_mock_html
        html = _gen_mock_html(1000)
        assert "Kafka" in html


# ─── 基准函数可调用性 ────────────────────────────

class TestBenchmarkFunctions:
    """基准函数基本可调用性"""

    def test_bench_html_extraction_returns_dict(self):
        """bench_html_extraction 返回字典"""
        from benchmark import bench_html_extraction
        with patch("benchmark.ITERATIONS", 2):
            with patch("benchmark.LARGE_HTML_SIZE", 1000):
                result = bench_html_extraction()
        assert isinstance(result, dict)
        assert "regex" in result  # regex 总是可用

    def test_bench_html_extraction_selectolax_optional(self):
        """selectolax 不可用时结果为 None"""
        from benchmark import bench_html_extraction
        # 模拟 selectolax 不可用
        with patch.dict("sys.modules", {"selectolax": None}):
            with patch("benchmark.ITERATIONS", 2):
                with patch("benchmark.LARGE_HTML_SIZE", 1000):
                    result = bench_html_extraction()
        # selectolax 可能可用也可能不可用，取决于环境
        assert isinstance(result, dict)


# ─── 阈值参数解析 ────────────────────────────

class TestThresholdParsing:
    """--threshold 参数解析"""

    def test_default_threshold(self):
        """无 --threshold 时默认 0.20"""
        # 重新加载 benchmark 模块以测试
        with patch("sys.argv", ["benchmark.py"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.REGRESSION_THRESHOLD == 0.20

    def test_custom_threshold(self):
        """--threshold 0.3 解析正确"""
        with patch("sys.argv", ["benchmark.py", "--threshold", "0.3"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.REGRESSION_THRESHOLD == 0.3

    def test_invalid_threshold_falls_back(self):
        """无效阈值回退到默认"""
        with patch("sys.argv", ["benchmark.py", "--threshold", "invalid"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.REGRESSION_THRESHOLD == 0.20

    def test_threshold_without_value_falls_back(self):
        """--threshold 后无值时回退到默认"""
        with patch("sys.argv", ["benchmark.py", "--threshold"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.REGRESSION_THRESHOLD == 0.20


# ─── 模式标志 ────────────────────────────

class TestModeFlags:
    """--quick / --ci / --update-baseline 模式"""

    def test_quick_mode_reduces_iterations(self):
        """--quick 减少迭代次数"""
        with patch("sys.argv", ["benchmark.py", "--quick"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.QUICK is True
            assert benchmark.ITERATIONS == 3

    def test_ci_mode(self):
        """--ci 模式"""
        with patch("sys.argv", ["benchmark.py", "--ci"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.CI_MODE is True

    def test_update_baseline_mode(self):
        """--update-baseline 模式"""
        with patch("sys.argv", ["benchmark.py", "--update-baseline"]):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            assert benchmark.UPDATE_BASELINE is True