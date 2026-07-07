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
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


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
        """写入一条结构化日志"""
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

        with self._lock:
            filepath = self._get_file()
            self._rotate_if_needed(filepath)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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

    def counter(self, name: str, labels: Optional[dict] = None) -> str:
        """自增计数器，返回完整 metric 名称"""
        full = f"{self.namespace}_{name}"
        key = full
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{full}{{{label_str}}}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
        return key

    def gauge(self, name: str, value: float, labels: Optional[dict] = None):
        """设置 gauge 值"""
        full = f"{self.namespace}_{name}"
        key = full
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            key = f"{full}{{{label_str}}}"
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: Optional[dict] = None):
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

            for key, value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {key.split('{')[0]} gauge")
                lines.append(f"{key} {value}")

            for key, values in sorted(self._histograms.items()):
                if not values:
                    continue
                base = key.split('{')[0]
                lines.append(f"# TYPE {base} histogram")
                lines.append(f"# HELP {base} Request duration in ms")
                for v in values[-100:]:  # last 100
                    lines.append(f"{key} {v}")
                lines.append(f"{base}_bucket{{le=\"+Inf\"}} {len(values)}")
                lines.append(f"{base}_count {len(values)}")
                lines.append(f"{base}_sum {sum(values)}")

        return "\n".join(lines) + "\n"


# ─── 全局实例 ─────────────────────

_logger: Optional[StructuredLogger] = None
_metrics: Optional[MetricsRegistry] = None


def get_logger() -> StructuredLogger:
    global _logger
    if _logger is None:
        _logger = StructuredLogger()
    return _logger


def get_metrics() -> MetricsRegistry:
    global _metrics
    if _metrics is None:
        _metrics = MetricsRegistry()
    return _metrics
