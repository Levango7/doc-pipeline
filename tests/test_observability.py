"""tests/test_observability.py — 结构化日志 + Prometheus 指标。"""
import shutil
import threading
import time

from pipeline_core.observability import (
    MetricsRegistry,
    StructuredLogger,
    get_logger,
    get_metrics,
)


def _wait_until(predicate, timeout=3.0) -> bool:
    """轮询异步落盘：后台线程每 0.5s 收一次队列，固定 sleep 既慢又脆。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


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


class TestWriterThreadSurvival:
    """后台 flush 线程不能被一次写盘失败带走。

    线程一死，队列只进不出：之后每一条日志都"成功"入队却永远不落盘，整个引擎
    从此无声——排查时看到的是"没有异常"。真实触发路径是 log_dir 为相对路径，
    进程 chdir 或临时目录被清理后 open(..., "a") 直接 FileNotFoundError。
    """

    def test_vanished_log_dir_is_recreated_and_the_entry_still_lands(self, tmp_path):
        log_dir = tmp_path / "logs"
        logger = StructuredLogger(str(log_dir), app_name="t")
        logger.info("before")
        assert _wait_until(lambda: any(
            "before" in p.read_text(encoding="utf-8") for p in log_dir.glob("*.jsonl")
        )), "基线：日志本该落盘"

        shutil.rmtree(log_dir)
        logger.info("after")

        assert logger._writer_thread.is_alive(), "目录没了就把线程杀掉，之后全体日志静默丢失"
        assert _wait_until(lambda: any(
            "after" in p.read_text(encoding="utf-8") for p in log_dir.glob("*.jsonl")
        )), "重建目录后这条必须写进去，不能只入队不出队"

    def test_one_failing_flush_is_reported_not_swallowed_silently(self, tmp_path, capsys):
        logger = StructuredLogger(str(tmp_path), app_name="t")
        real = logger._get_file
        seen = {"n": 0}

        def flaky():
            seen["n"] += 1
            if seen["n"] == 1:
                raise OSError("disk gone")
            return real()

        logger._get_file = flaky
        logger.info("doomed")
        assert _wait_until(lambda: seen["n"] >= 1)

        err = capsys.readouterr().err
        assert "flush failed" in err and "disk gone" in err, err
        assert not list(tmp_path.glob("*.jsonl")), "这一次确实丢了，测试不能假装它写成功"

        logger.info("second")
        assert _wait_until(lambda: any(
            "second" in p.read_text(encoding="utf-8") for p in tmp_path.glob("*.jsonl")
        )), "第一次失败之后线程还活着，第二条才能落盘"


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
