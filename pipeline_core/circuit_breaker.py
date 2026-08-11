"""
CircuitBreaker v1 - 熔断器 + Backoff with Jitter
=================================================
状态机: CLOSED → OPEN (failure_threshold) → HALF_OPEN (recovery_timeout) → CLOSED (success) / OPEN (failure)
"""
from __future__ import annotations

import random
import threading
import time
from enum import Enum


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
        transition = None
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                # half_open_attempts 已由 allow_request 占用测试名额时原子递增，
                # 此处仅检查是否达到成功阈值，不再重复递增（避免双重计数）。
                if self.half_open_attempts >= self.half_open_max_tests:
                    transition = self._transition_to(CircuitState.CLOSED)
                    self.failure_count = 0
                    self.half_open_attempts = 0
            elif self.state == CircuitState.CLOSED:
                self.failure_count = 0  # 成功清零连续失败计数
        # 回调在锁外触发，避免持锁调用外部代码（emit_event/alert）导致死锁
        if transition:
            self._fire_state_change_callbacks(*transition)

    def record_failure(self) -> CircuitState:
        """记录失败，返回当前状态"""
        transition = None
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN or self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
                transition = self._transition_to(CircuitState.OPEN)
                current = CircuitState.OPEN
            else:
                current = self.state
        # 回调在锁外触发
        if transition:
            self._fire_state_change_callbacks(*transition)
        return current

    def allow_request(self) -> bool:
        """是否允许请求通过"""
        transition = None
        allowed = False
        with self._lock:
            if self.state == CircuitState.CLOSED:
                allowed = True
            elif self.state == CircuitState.OPEN:
                elapsed = time.time() - self.last_state_change
                if elapsed >= self.recovery_timeout:
                    transition = self._transition_to(CircuitState.HALF_OPEN)
                    self.half_open_attempts = 0
                    # 占用第一个测试名额（CAS 原子操作，修复 P0: 并发全部放行）
                    self.half_open_attempts += 1
                    allowed = True
                else:
                    allowed = False
            else:  # HALF_OPEN: 原子地占用一个测试名额
                # 修复 P0: 原先只读 half_open_attempts 不递增，导致并发请求全部放行
                if self.half_open_attempts < self.half_open_max_tests:
                    self.half_open_attempts += 1
                    allowed = True
                else:
                    allowed = False
        # OPEN→HALF_OPEN 转换的回调在锁外触发，避免死锁
        if transition:
            self._fire_state_change_callbacks(*transition)
        return allowed

    def _transition_to(self, new_state: CircuitState) -> tuple | None:
        """状态转换（必须在 self._lock 内调用）。

        仅做状态变更和内部计数器重置，**不触发外部回调**。
        返回 (old_state, new_state) 表示发生了转换，None 表示状态未变。
        外部回调由调用方在锁外通过 _fire_state_change_callbacks 触发，
        避免持锁调用 emit_event/alert 导致死锁（P0 修复）。
        """
        if self.state != new_state:
            old_state = self.state
            self.state = new_state
            self.last_state_change = time.time()
            # 进入 OPEN 时重置 success 计数
            if new_state == CircuitState.OPEN:
                self.success_count = 0
            return (old_state, new_state)
        return None

    def _fire_state_change_callbacks(self, old_state: CircuitState, new_state: CircuitState):
        """触发状态变更的外部回调（必须在锁外调用，避免死锁）。

        回调包括：
        - event_hook.emit_event: 事件钩子（可能触发 webhook HTTP POST）
        - alert_manager.alert: 告警通知（可能触发 webhook）

        这些外部调用可能阻塞或获取其他锁，因此绝不能在 self._lock
        持锁状态下执行（P0 修复：原先在 _transition_to 持锁时触发，存在死锁风险）。
        """
        try:
            from .event_hook import emit_event
            if new_state == CircuitState.OPEN:
                emit_event("circuit_breaker.open", {"agent": self.name, "failure_count": self.failure_count})
                from .alert_manager import alert
                alert("critical", "circuit_breaker",
                      f"Agent {self.name} 熔断器 OPEN（连续失败 {self.failure_count} 次）",
                      {"agent": self.name, "failure_count": self.failure_count})
            elif new_state == CircuitState.CLOSED:
                emit_event("circuit_breaker.close", {"agent": self.name, "from": old_state.value})
        except Exception:
            pass

    def reset(self):
        """完全重置熔断器（恢复 CLOSED）"""
        transition = None
        with self._lock:
            transition = self._transition_to(CircuitState.CLOSED)
            self.failure_count = 0
            self.success_count = 0
            self.half_open_attempts = 0
        if transition:
            self._fire_state_change_callbacks(*transition)

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

    def get(self, name: str) -> CircuitBreaker | None:
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
