"""tests/test_observability.py — 结构化日志 + Prometheus 指标。"""
import threading
import time

from pipeline_core.observability import (
    MetricsRegistry,
    StructuredLogger,
    get_logger,
    get_metrics,
)


class TestStructuredLogger:
    def test_log_writes_to_file(self, tmp_path):
        logger = StructuredLogger(str(tmp_path), app_name="test")
        logger.info("hello", trace_id="t1", agent="writer")
        time.sleep(0.6)  # 等后台线程刷盘
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "hello" in content
        assert "t1" in content

    def test_log_levels(self, tmp_path):
        logger = StructuredLogger(str(tmp_path), app_name="test")
        logger.info("info-msg")
        logger.error("error-msg")
        time.sleep(0.6)
        content = list(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8")
        assert "INFO" in content
        assert "ERROR" in content

    def test_concurrent_logging(self, tmp_path):
        logger = StructuredLogger(str(tmp_path), app_name="test")
        errors = []

        def _log(i):
            try:
                logger.info(f"msg-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_log, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.8)
        assert not errors
        content = list(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8")
        assert content.count("msg-") == 50


class TestMetricsRegistry:
    def test_counter_increment(self):
        m = MetricsRegistry()
        m.counter("requests")
        m.counter("requests")
        output = m.to_prometheus()
        assert "docpipeline_requests 2" in output

    def test_gauge_set(self):
        m = MetricsRegistry()
        m.gauge("cpu", 42.5)
        output = m.to_prometheus()
        assert "docpipeline_cpu 42.5" in output

    def test_histogram_observe(self):
        m = MetricsRegistry()
        for v in [1.0, 2.0, 3.0]:
            m.observe("latency", v)
        output = m.to_prometheus()
        assert "docpipeline_latency_count 3" in output
        assert "docpipeline_latency_sum 6.0" in output

    def test_histogram_buckets(self):
        m = MetricsRegistry()
        m.observe("duration", 0.1)
        output = m.to_prometheus()
        assert 'le="0.1"' in output
        assert 'le="+Inf"' in output

    def test_output_has_type_comments(self):
        m = MetricsRegistry()
        m.counter("hits")
        output = m.to_prometheus()
        assert "# TYPE docpipeline_hits counter" in output


class TestSingletons:
    def test_get_logger_returns_singleton(self):
        # 重置单例以测试
        import pipeline_core.observability as obs
        obs._logger = None
        l1 = get_logger()
        l2 = get_logger()
        assert l1 is l2

    def test_get_metrics_returns_singleton(self):
        import pipeline_core.observability as obs
        obs._metrics = None
        m1 = get_metrics()
        m2 = get_metrics()
        assert m1 is m2
