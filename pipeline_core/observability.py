"""
Observability v1 - 结构化日志 + Prometheus 风格指标
====================================================
特点：
  - JSON Lines 格式日志（每行一个事件，可被 fluentd/logstash 消费）
  - 每行携带 trace_id，跨 agent/step 串联
  - Prometheus 兼容的 metrics 端点（/metrics 输出）
  - 零外部依赖
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

# ─── 结构化日志 ─────────────────────

class StructuredLogger:
    """JSON Lines 结构化日志"""

    def __init__(self, log_dir: str = "logs", app_name: str = "doc-pipeline",
                 max_file_mb: int = 100):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.app_name = app_name
        self.max_file_mb = max_file_mb
        self._lock = threading.RLock()
        self._current_file = None

        # 异步写入队列：后台线程批量刷盘，减少主线程 I/O 阻塞
        import queue as _queue
        self._write_queue: _queue.Queue = _queue.Queue(maxsize=10000)
        self._writer_stop = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._async_writer_loop, daemon=True, name="log-writer"
        )
        self._writer_thread.start()

    def _async_writer_loop(self):
        """后台日志写入线程：从队列批量取出日志条目写入文件"""
        import queue as _queue
        batch: list = []
        while not self._writer_stop.is_set():
            try:
                entry = self._write_queue.get(timeout=0.5)
                batch.append(entry)
                # 批量取出（最多 100 条一次写入）
                while len(batch) < 100:
                    try:
                        batch.append(self._write_queue.get_nowait())
                    except _queue.Empty:
                        break
            except _queue.Empty:
                continue

            if batch:
                self._flush_batch(batch)
                batch = []

        # 关闭前刷完剩余
        while not self._write_queue.empty():
            try:
                batch.append(self._write_queue.get_nowait())
            except _queue.Empty:
                break
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list):
        """批量写入日志条目到文件"""
        if not batch:
            return
        with self._lock:
            filepath = self._get_file()
            self._rotate_if_needed(filepath)
            lines = [json.dumps(entry, ensure_ascii=False) + "\n" for entry in batch]
            with open(filepath, "a", encoding="utf-8") as f:
                f.writelines(lines)

    def _get_file(self) -> Path:
        now = datetime.now()
        filename = f"{self.app_name}_{now.strftime('%Y%m%d')}.jsonl"
        return self.log_dir / filename

    def _rotate_if_needed(self, filepath: Path):
        if filepath.exists() and filepath.stat().st_size > self.max_file_mb * 1024 * 1024:
            # 保留最多 5 个备份, .1.jsonl ~ .5.jsonl
            for i in range(4, 0, -1):
                older = filepath.with_suffix(f".{i}.jsonl")
                newer = filepath.with_suffix(f".{i+1}.jsonl")
                if older.exists():
                    older.rename(newer)
            rotated = filepath.with_suffix(".1.jsonl")
            filepath.rename(rotated)

    def log(self, level: str, message: str, trace_id: str = "",
            agent: str = "", task_id: str = "",
            **extra):
        """写入一条结构化日志（异步写入队列，减少主线程 I/O 阻塞）"""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level.upper(),
            "message": message,
            "app": self.app_name,
        }
        if trace_id:
            entry["trace_id"] = trace_id
        if agent:
            entry["agent"] = agent
        if task_id:
            entry["task_id"] = task_id
        if extra:
            # 展平 extra 中可序列化的字段
            for k, v in extra.items():
                try:
                    json.dumps(v)
                    entry[k] = v
                except (TypeError, ValueError):
                    entry[k] = str(v)

        # 异步写入：放入队列由后台线程刷盘，减少主线程 I/O 阻塞
        # P1 修复: 使用 put_nowait 避免队列满时阻塞主线程（原 put 会无限阻塞）
        try:
            self._write_queue.put_nowait(entry)
        except Exception:
            # 队列满或已关闭：丢弃日志条目并记录到 stderr（避免递归调用 logger）
            import sys as _sys
            _sys.stderr.write(f"[StructuredLogger] write queue full, dropping log: {message}\n")

    def info(self, msg: str, **kw):
        self.log("info", msg, **kw)

    def warning(self, msg: str, **kw):
        self.log("warning", msg, **kw)

    def error(self, msg: str, **kw):
        self.log("error", msg, **kw)

    def debug(self, msg: str, **kw):
        self.log("debug", msg, **kw)


# ─── Prometheus 风格指标 ─────────────────────

class MetricsRegistry:
    """轻量级指标注册中心（Prometheus text 格式输出）"""

    def __init__(self, namespace: str = "docpipeline"):
        self.namespace = namespace
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    def counter(self, name: str, labels: dict | None = None) -> str:
        """自增计数器，返回完整 metric 名称"""
        full = f"{self.namespace}_{name}"
        key = full
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{full}{{{label_str}}}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
        return key

    def gauge(self, name: str, value: float, labels: dict | None = None):
        """设置 gauge 值"""
        full = f"{self.namespace}_{name}"
        key = full
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{full}{{{label_str}}}"
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: dict | None = None):
        """记录直方图观测值"""
        full = f"{self.namespace}_{name}"
        key = full
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{full}{{{label_str}}}"
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = []
            self._histograms[key].append(value)
            # keep last 1000
            if len(self._histograms[key]) > 1000:
                self._histograms[key] = self._histograms[key][-1000:]

    def to_prometheus(self) -> str:
        """输出 Prometheus text 格式"""
        lines = []
        with self._lock:
            for key, value in sorted(self._counters.items()):
                lines.append(f"# TYPE {key.split('{')[0]} counter")
                lines.append(f"{key} {value}")

            for key, value in sorted(self._gauges.items()):  # type: ignore[assignment]
                lines.append(f"# TYPE {key.split('{')[0]} gauge")
                lines.append(f"{key} {value}")

            for key, values in sorted(self._histograms.items()):
                if not values:
                    continue
                base = key.split('{')[0]
                labels = key[len(base):] if key.startswith(base) else ""
                lines.append(f"# TYPE {base} histogram")
                lines.append(f"# HELP {base} Request duration in ms")
                # 定义标准 bucket 边界（Prometheus 默认）
                buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
                counts = [0] * len(buckets)
                for v in values:
                    for i, le in enumerate(buckets):
                        if v <= le:
                            counts[i] += 1
                cum = 0
                for i, le in enumerate(buckets):
                    cum += counts[i]
                    lines.append(f"{base}_bucket{{le=\"{le}\"{labels}}} {cum}")
                lines.append(f"{base}_bucket{{le=\"+Inf\"{labels}}} {len(values)}")
                lines.append(f"{base}_count{labels} {len(values)}")
                lines.append(f"{base}_sum{labels} {sum(values)}")

        return "\n".join(lines) + "\n"


# ─── 全局实例 ─────────────────────

_logger: StructuredLogger | None = None
_metrics: MetricsRegistry | None = None
_singleton_lock = threading.Lock()


def get_logger() -> StructuredLogger:
    global _logger
    if _logger is None:
        with _singleton_lock:
            if _logger is None:
                _logger = StructuredLogger()
    return _logger


def get_metrics() -> MetricsRegistry:
    global _metrics
    if _metrics is None:
        with _singleton_lock:
            if _metrics is None:
                _metrics = MetricsRegistry()
    return _metrics
