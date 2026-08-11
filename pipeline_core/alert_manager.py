"""AlertManager — 告警机制

熔断器 OPEN / DLQ 堆积 / 限流拒绝 / 预算超限 时主动通知。

通知渠道：
  1. 事件钩子（webhook URL POST）
  2. 结构化日志
  3. 内存告警缓冲（供 API 查询）

用法：
    from .alert_manager import alert, get_alerts
    alert("critical", "circuit_breaker", f"{agent_name} 熔断器 OPEN")
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_ALERT_BUFFER: deque = deque(maxlen=200)
_LOCK = threading.Lock()

LEVELS = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def alert(level: str, category: str, message: str, extra: dict = None):
    """触发告警

    Args:
        level: info / warning / error / critical
        category: circuit_breaker / dlq / rate_limit / budget / quality / custom
        message: 告警消息
        extra: 附加数据
    """
    entry = {
        "timestamp": time.time(),
        "level": level,
        "category": category,
        "message": message,
        "extra": extra or {},
    }

    with _LOCK:
        _ALERT_BUFFER.append(entry)

    log_msg = f"[ALERT:{level}] {category}: {message}"
    if level == "critical" or level == "error":
        logger.error(log_msg)
    elif level == "warning":
        logger.warning(log_msg)
    else:
        logger.info(log_msg)

    try:
        from .event_hook import emit_event
        emit_event(f"alert.{level}", {
            "category": category,
            "message": message,
            "level": level,
            "extra": extra or {},
        })
    except Exception:
        pass


def get_alerts(level: str = None, category: str = None,
               since: float = 0, limit: int = 50) -> list[dict]:
    """查询告警历史"""
    with _LOCK:
        alerts = list(_ALERT_BUFFER)

    result = []
    for a in reversed(alerts):
        if a["timestamp"] < since:
            continue
        if level and a["level"] != level:
            continue
        if category and a["category"] != category:
            continue
        result.append(a)
        if len(result) >= limit:
            break
    return result


def clear_alerts():
    """清空告警缓冲"""
    with _LOCK:
        _ALERT_BUFFER.clear()
