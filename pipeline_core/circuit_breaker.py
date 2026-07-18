"""
CircuitBreaker v1 - 熔断器 + Backoff with Jitter
=================================================
状态机: CLOSED → OPEN (failure_threshold) → HALF_OPEN (recovery_timeout) → CLOSED (success) / OPEN (failure)
"""
from __future__ import annotations

import random
import threading
import time
from enum import Enum, auto
from typing import Optional


class CircuitState(Enum):
    CLOSED = "closed"          # 正常工作
    OPEN = "open"              # 熔断中
    HALF_OPEN = "half_open"    # 尝试恢复


class CircuitBreaker:
    """Per-agent 熔断器"""

    def __init__(self, name: str, failure_threshold: int = 5,
                 recovery_timeout: float = 60.0, half_open_max_tests: int = 1):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_tests = half_open_max_tests

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0
        self.last_state_change = time.time()
        self.half_open_attempts = 0
        self._lock = threading.RLock()

    def record_success(self):
        """记录成功"""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.half_open_attempts += 1
                if self.half_open_attempts >= self.half_open_max_tests:
                    self._transition_to(CircuitState.CLOSED)
                    self.failure_count = 0
                    self.half_open_attempts = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0  # 成功清零连续失败计数

    def record_failure(self) -> CircuitState:
        """记录失败，返回当前状态"""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)
                return CircuitState.OPEN

            if self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
                self._transition_to(CircuitState.OPEN)
                return CircuitState.OPEN

            return self.state

    def allow_request(self) -> bool:
        """是否允许请求通过"""
        with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                elapsed = time.time() - self.last_state_change
                if elapsed >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    self.half_open_attempts = 0
                    return True
                return False

            # HALF_OPEN
            return self.half_open_attempts < self.half_open_max_tests

    def _transition_to(self, new_state: CircuitState):
        if self.state != new_state:
            old_state = self.state
            self.state = new_state
            self.last_state_change = time.time()
            # 进入 OPEN 时重置 success 计数
            if new_state == CircuitState.OPEN:
                self.success_count = 0
            # ── 事件钩子 ──
            try:
                from .event_hook import emit_event
                if new_state == CircuitState.OPEN:
                    emit_event("circuit_breaker.open", {"agent": self.name, "failure_count": self.failure_count})
                elif new_state == CircuitState.CLOSED:
                    emit_event("circuit_breaker.close", {"agent": self.name, "from": old_state.value})
            except Exception:
                pass

    def reset(self):
        """完全重置熔断器（恢复 CLOSED）"""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)
            self.failure_count = 0
            self.success_count = 0
            self.half_open_attempts = 0

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "last_failure_time": self.last_failure_time,
                "last_state_change": self.last_state_change,
                "uptime": time.time() - (self.last_state_change or time.time()),
            }


class CircuitBreakerRegistry:
    """全局熔断器注册中心"""

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def get_or_create(self, name: str, **kwargs) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name=name, **kwargs)
            return self._breakers[name]

    def get(self, name: str) -> Optional[CircuitBreaker]:
        with self._lock:
            return self._breakers.get(name)

    def reset(self, name: str):
        with self._lock:
            if name in self._breakers:
                self._breakers[name].reset()

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._breakers.items()}


# ─── Backoff with Jitter ────────────────────

def backoff_with_jitter(base_delay: float, attempt: int,
                        max_delay: float = 120.0, jitter_range: float = 0.3,
                        strategy: str = "exponential") -> float:
    """带 jitter 的重试退避计算

    Args:
        base_delay: 基础延迟秒数
        attempt: 第几次重试（从 1 开始）
        max_delay: 最大延迟
        jitter_range: jitter 比例（±30%）
        strategy: exponential | linear | fixed

    Returns:
        实际等待秒数（带随机 jitter）
    """
    if strategy == "linear":
        delay = base_delay * attempt
    elif strategy == "fixed":
        delay = base_delay
    else:  # exponential
        delay = base_delay * (2 ** (attempt - 1))

    # 上限
    delay = min(delay, max_delay)

    # ±jitter_range 的随机抖动
    jitter = delay * jitter_range
    delay += random.uniform(-jitter, jitter)

    return max(0.1, delay)  # 至少 100ms
