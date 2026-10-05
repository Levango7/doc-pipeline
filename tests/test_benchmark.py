"""benchmark.py — 基准项 / 回归检测

测试原则：
  - 用 mock 模拟外部依赖（selectolax、numpy 等）
  - 不实际运行完整基准（耗时）
  - 每个测试方法聚焦一个行为
"""
import sys
from pathlib import Path
from unittest.mock import patch

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
            METRIC_HIGHER_BETTER,
            _check_regression,
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
            METRIC_LOWER_BETTER,
            _check_regression,
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
        with patch("benchmark.ITERATIONS", 2), patch("benchmark.LARGE_HTML_SIZE", 1000):
            result = bench_html_extraction()
        assert isinstance(result, dict)
        assert "regex" in result  # regex 总是可用

    def test_bench_html_extraction_selectolax_optional(self):
        """selectolax 不可用时结果为 None"""
        from benchmark import bench_html_extraction
        # 模拟 selectolax 不可用
        with patch.dict("sys.modules", {"selectolax": None}), \
                patch("benchmark.ITERATIONS", 2), \
                patch("benchmark.LARGE_HTML_SIZE", 1000):
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


class TestSamplesFlag:
    """--samples 解析：CI 默认 3 轮，本地 1 轮，越界夹住，坏值回退。"""

    def _reload(self, argv):
        with patch("sys.argv", argv):
            if "benchmark" in sys.modules:
                del sys.modules["benchmark"]
            import benchmark
            return benchmark

    def test_ci_defaults_to_three_samples(self):
        assert self._reload(["benchmark.py", "--ci"]).SAMPLES == 3

    def test_local_defaults_to_one_sample(self):
        assert self._reload(["benchmark.py"]).SAMPLES == 1

    def test_explicit_value_wins(self):
        assert self._reload(["benchmark.py", "--ci", "--samples", "5"]).SAMPLES == 5

    def test_out_of_range_is_clamped_not_trusted(self):
        """0/负数会让"取中位数"变成取空；99 轮是把门禁当压测开关。"""
        assert self._reload(["benchmark.py", "--ci", "--samples", "0"]).SAMPLES == 1
        assert self._reload(["benchmark.py", "--ci", "--samples", "99"]).SAMPLES == 9

    def test_invalid_value_falls_back_to_default(self):
        assert self._reload(["benchmark.py", "--ci", "--samples", "abc"]).SAMPLES == 3

    def test_samples_without_value_falls_back(self):
        assert self._reload(["benchmark.py", "--ci", "--samples"]).SAMPLES == 3


class TestNoiseAwareGate:
    """回归判据要区分"确实变慢"与"这台机器此刻测不出答案"。

    起因是 CI run 37350496361：并行执行 serial 0.02656→0.03671 秒（差 10 毫秒、
    测量本身被进程池启动支配）被判 38.2% 回归，同一 job 复验仍"确认"——因为两次
    采样取自同一台被负载污染的机器。门禁全红到没人再看它，跟没有门禁一样。
    """

    PARALLEL = "并行执行 (Thread vs Process)"

    def test_ci_evidence_case_is_unverified_not_fail(self):
        """就是那组把 CI 判红的数字——差 10 毫秒，绝对量根本不成立。"""
        from benchmark import _classify_all
        # 相对变化 36%（超阈），绝对差 0.0004 秒（低于 selectolax 的 0.0005 秒下限）
        current = {self.PARALLEL: {"selectolax": 0.0015}}
        baseline = {self.PARALLEL: {"selectolax": 0.0011}}
        hits, unverified = _classify_all(current, baseline, 0.30)
        assert hits == [], hits
        assert len(unverified) == 1 and "可测下限" in unverified[0], unverified

    def test_parallel_pool_timings_are_observed_but_never_gate(self):
        """并行执行那三项测的是池 spawn 成本，随机器状态漂移，不许判红。

        正确性由 tests/test_executor_factory.py 负责；这里若能用它判红，
        只会把"今天 runner 忙"变成"你的提交有性能问题"。
        """
        from benchmark import REPORT_ONLY_METRICS, _classify_all
        assert {"serial", "thread_pool", "process_pool"} <= REPORT_ONLY_METRICS
        current = {self.PARALLEL: {"serial": 0.03671, "thread_pool": 0.03579,
                                   "process_pool": 0.60}}
        baseline = {self.PARALLEL: {"serial": 0.02656, "thread_pool": 0.02584,
                                   "process_pool": 0.1461}}
        hits, unverified = _classify_all(current, baseline, 0.30)
        assert hits == [] and unverified == []

    def test_same_ratio_above_floor_is_a_real_regression(self):
        """判据必须能命中：把 CI 那个比例搬到明显超出噪声的量级上就该判红。"""
        from benchmark import classify_change
        got = classify_change(self.PARALLEL, "selectolax", 0.0034, 0.0102, 0.30)
        assert got is not None and got[0] == "REGRESSION", got
        assert "REGRESSION" in got[1]

    def test_spread_as_big_as_the_delta_is_unverified(self):
        """同一份代码自己就抖 20%，就不能说 32% 的变化是回归。"""
        from benchmark import classify_change
        got = classify_change(self.PARALLEL, "selectolax", 1.0, 1.32, 0.30, spread=0.20)
        assert got is not None and got[0] == "UNVERIFIED", got
        assert "波动" in got[1]

    def test_small_spread_does_not_excuse_a_big_delta(self):
        from benchmark import classify_change
        got = classify_change(self.PARALLEL, "selectolax", 1.0, 1.32, 0.30, spread=0.02)
        assert got is not None and got[0] == "REGRESSION", got

    def test_within_threshold_reports_nothing(self):
        from benchmark import classify_change
        assert classify_change(self.PARALLEL, "serial", 1.0, 1.10, 0.30) is None

    def test_metric_without_a_direction_reports_nothing(self):
        """未注册方向的指标一律跳过——包括两个方向集合都没有的名字。"""
        from benchmark import classify_change
        assert classify_change(self.PARALLEL, "n_paragraphs", 10.0, 1.0, 0.30) is None

    def test_throughput_has_no_absolute_floor_but_is_noise_gated(self):
        """ops/s 的绝对值跨数量级，固定下限没意义：不设 floor，靠采样波动判。"""
        from benchmark import classify_change
        # 无波动信息时：1000→600（-40%）就是确认回归
        assert classify_change("CacheManager 吞吐", "set_ops_per_sec", 1000, 600, 0.30)[0] \
            == "REGRESSION"
        # 同一份代码自己就抖 25% 时，-40% 不够可信（判据要求超过波动的 2 倍）
        assert classify_change("CacheManager 吞吐", "set_ops_per_sec", 1000, 600,
                               0.30, spread=0.25)[0] == "UNVERIFIED"

    def test_check_regression_returns_only_confirmed(self):
        from benchmark import _check_regression
        current = {self.PARALLEL: {"selectolax": 0.0015}}
        baseline = {self.PARALLEL: {"selectolax": 0.0011}}
        assert _check_regression(current, baseline, 0.30) == []

    def test_classify_all_skips_metadata_keys(self):
        """baseline 里带 _env / _spreads 之后，它们不能被当成基准项比对。"""
        from benchmark import _classify_all
        current = {"_env": {"system": "Linux"}, "_spreads": {"a.b": 0.4},
                   self.PARALLEL: {"selectolax": 0.0015}}
        baseline = {"_env": {"system": "Linux"}, "_spreads": {"a.b": 0.4},
                    self.PARALLEL: {"selectolax": 0.0011}}
        hits, unverified = _classify_all(current, baseline, 0.30)
        assert hits == []
        assert len(unverified) == 1 and "selectolax" in unverified[0]

    def test_worst_of_three_noise_estimates_wins(self):
        """本轮波动、基线记录的波动、历史跨 run 抖动——取最大者当分母。

        只算本轮会漏判：基线若是单次采样留下的（老版本行为），它自带的抖动会被
        读成"当前变慢了"。本机 4 次独立跑实测 set_ms_per_op 跨次抖 57.6%、
        get_hit_ms_per_op 抖 42.9%，而阈值只有 30%。
        """
        from benchmark import _classify_all
        key = "CacheManager 吞吐.set_ms_per_op"
        current = {"CacheManager 吞吐": {"set_ms_per_op": 0.0060}}
        baseline = {"CacheManager 吞吐": {"set_ms_per_op": 0.0013},
                    "_spreads": {key: 0.10}}
        assert _classify_all(current, baseline, 0.30, {key: 0.03})[0] != []
        # 历史把抖动撑到 400%：同一组数字就再也判不红了
        hits, unverified = _classify_all(current, baseline, 0.30, {key: 0.03},
                                         {key: 4.0})
        assert hits == [] and len(unverified) == 1

    def test_report_only_metrics_never_reach_a_verdict(self):
        """并行那三项只观测：既不判红，也不该以"不可判"的名义刷屏。"""
        from benchmark import REPORT_ONLY_METRICS, _classify_all
        assert {"serial", "thread_pool", "process_pool"} <= REPORT_ONLY_METRICS
        current = {self.PARALLEL: {"serial": 0.9, "thread_pool": 0.9, "process_pool": 0.9}}
        baseline = {self.PARALLEL: {"serial": 0.01, "thread_pool": 0.01, "process_pool": 0.01}}
        assert _classify_all(current, baseline, 0.30) == ([], [])

    def test_floor_table_covers_every_gated_metric(self):
        """每个参与判定的指标都要有规则：定值下限、"噪声成比例不设下限"，或只观测。

        漏在三种规则之外的指标会退化成纯相对阈——正是这次要修掉的行为。
        下限表里也不许留已改判"只观测"的指标：那是死条目，还会误导读代码的人。
        """
        from benchmark import (
            METRIC_FLOORS,
            METRIC_HIGHER_BETTER,
            METRIC_LOWER_BETTER,
            PROPORTIONAL_NOISE_METRICS,
            REPORT_ONLY_METRICS,
            _floor_for,
        )
        covered = set(METRIC_FLOORS) | PROPORTIONAL_NOISE_METRICS | REPORT_ONLY_METRICS
        missing = sorted((METRIC_HIGHER_BETTER | METRIC_LOWER_BETTER) - covered)
        assert not missing, f"这些参与判定的指标没有噪声规则: {missing}"
        assert not set(METRIC_FLOORS) & REPORT_ONLY_METRICS,             sorted(set(METRIC_FLOORS) & REPORT_ONLY_METRICS)
        assert _floor_for("selectolax", 1.0) == 0.0005
        assert _floor_for("set_ops_per_sec", 1000) == 0.0

    def test_env_fingerprint_has_the_comparable_fields(self):
        from benchmark import _env_fingerprint
        env = _env_fingerprint()
        assert set(env) >= {"system", "machine", "python", "cpu_count", "load1"}
        assert env["cpu_count"] and env["system"]


class TestHistoricalSpreads:
    """跨 run 波动带：历史才是区分"代码慢了"与"机器慢了"的唯一参照。"""

    def _write(self, path, rows):
        import json
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")

    def test_needs_five_runs_before_claiming_a_band(self, tmp_path):
        from benchmark import _historical_spreads
        p = tmp_path / "benchmark_history.jsonl"
        self._write(p, [{"b.serial": 1.0} for _ in range(4)])
        assert _historical_spreads(p) == {}

    def test_computes_relative_range_over_the_window(self, tmp_path):
        from benchmark import _historical_spreads
        p = tmp_path / "benchmark_history.jsonl"
        self._write(p, [{"b.elapsed_ms": v} for v in (40, 50, 45, 60, 30)])
        got = _historical_spreads(p)
        assert got["b.elapsed_ms"] == (60 - 30) / 45

    def test_only_the_recent_window_counts(self, tmp_path):
        """波动带只看最近 HISTORY_WINDOW 轮：机器换了、依赖换了，老抖动不该
        继续给现在的变化背书（否则一次异常值会永久免死）。"""
        from benchmark import HISTORY_WINDOW, _historical_spreads
        p = tmp_path / "benchmark_history.jsonl"
        recent = [40.0, 44.0, 42.0, 41.0, 43.0] * (HISTORY_WINDOW // 5 + 1)
        self._write(p, [{"b.elapsed_ms": 1000.0}] * 30 + [{"b.elapsed_ms": v} for v in recent])
        got = _historical_spreads(p)
        assert got["b.elapsed_ms"] < 0.2, got

    def test_tolerates_blank_and_malformed_lines(self, tmp_path):
        import json

        from benchmark import _historical_spreads
        p = tmp_path / "benchmark_history.jsonl"
        rows = [{"b.elapsed_ms": v} for v in (40, 44, 42, 41, 43)]
        text = "\n".join(json.dumps(r) for r in rows) + "\n\n{not json\n"
        p.write_text(text, encoding="utf-8")
        assert "b.elapsed_ms" in _historical_spreads(p)

    def test_ignores_metadata_columns(self, tmp_path):
        from benchmark import _historical_spreads
        p = tmp_path / "benchmark_history.jsonl"
        self._write(p, [{"_ts": "2026-10-06T00:00:00Z", "b.elapsed_ms": v}
                        for v in (40, 44, 42, 41, 43)])
        got = _historical_spreads(p)
        assert set(got) == {"b.elapsed_ms"}

    def test_missing_file_is_not_an_error(self, tmp_path):
        from benchmark import _historical_spreads
        assert _historical_spreads(tmp_path / "nope.jsonl") == {}


class TestAggregateSamples:
    """多样本取中位数并量出波动——这是"同码两遍必须自洽"的证据来源。"""

    def test_median_is_not_dragged_by_an_outlier(self):
        from benchmark import _aggregate
        samples = [
            {"b": {"serial": 1.0}},
            {"b": {"serial": 1.05}},
            {"b": {"serial": 9.0}},          # 一次被抢占的采样
        ]
        medians, spreads = _aggregate(samples)
        assert medians["b"]["serial"] == 1.05
        assert spreads["b.serial"] == (9.0 - 1.0) / 1.05

    def test_single_sample_has_no_spread(self):
        from benchmark import _aggregate
        medians, spreads = _aggregate([{"b": {"serial": 1.0}}])
        assert medians["b"]["serial"] == 1.0
        assert spreads == {}                  # 一轮采样谈不出波动

    def test_non_numeric_values_pass_through(self):
        """基准项报错时是 {"error": str}：要留住，别被聚合吞成空。"""
        from benchmark import _aggregate
        medians, _ = _aggregate([{"b": {"error": "boom"}}, {"b": {"error": "boom"}}])
        assert medians["b"]["error"] == "boom"

    def test_metric_missing_from_one_sample_still_aggregates(self):
        from benchmark import _aggregate
        medians, spreads = _aggregate([{"b": {"serial": 1.0, "thread_pool": 2.0}},
                                       {"b": {"serial": 1.2}}])
        assert medians["b"]["serial"] == 1.1
        assert medians["b"]["thread_pool"] == 2.0
        assert "b.thread_pool" not in spreads


class TestHistoryIgnoresMetadata:
    def test_flatten_skips_underscore_benches(self):
        from benchmark import _flatten_for_history
        flat = _flatten_for_history({"_env": {"system": "Linux"}, "_spreads": {"x.y": 0.1},
                                     "bench": {"serial": 1.5}})
        assert flat == {"bench.serial": 1.5}


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
