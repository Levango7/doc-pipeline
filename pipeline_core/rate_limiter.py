"""
RateLimiter v1 - 令牌桶限流器
=================================
用于搜索引擎调用、API 请求等外部资源的频率控制。

特点：
  - 令牌桶算法（平滑突发）
  - 每 agent/每引擎 独立限流
  - 线程安全
  - 支持自适应动态调整速率
"""
from __future__ import annotations

import logging
import threading
import time

_logger = logging.getLogger(__name__)


class RateLimiter:
    """令牌桶限流器"""

    def __init__(self, rate: float = 10.0, burst: int = 20,
                 name: str = "default"):
        """
        Args:
            rate:  每秒填充的令牌数（QPS）
            burst: 最大桶容量（允许的瞬时突发量）
            name:  限流器名称（用于日志/标识）
        """
        self.rate = rate
        self.burst = burst
        self.name = name

        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.RLock()
        # P0 修复: Condition 必须关联 self._lock，否则 wait/notify 无法正确协调
        # （原 threading.Condition() 使用独立内部锁，导致 notify_all 无法唤醒
        # 在 self._lock 上等待的线程，自适应限流失效）
        self._condition = threading.Condition(self._lock)

        # 统计
        self._total_acquired = 0
        self._total_blocked = 0
        self._total_wait_ms = 0.0

    def acquire(self, tokens: float = 1.0, block: bool = True,
                timeout: float | None = None) -> bool:
        """获取令牌

        Args:
            tokens: 需要的令牌数
            block:  是否阻塞等待
            timeout: 最大等待秒数（None = 无限）

        Returns:
            True = 获取成功, False = 超时/被拒绝
        """
        if tokens <= 0:
            return True

        if not block:
            ok = self._try_acquire(tokens)
            if not ok:
                with self._lock:
                    self._total_blocked += 1
            return ok

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            got = self._try_acquire(tokens)
            if got:
                return True

            if deadline and time.monotonic() >= deadline:
                with self._lock:
                    self._total_blocked += 1
                return False

            # 等待一个令牌的时间
            wait_time = tokens / max(self.rate, 1)
            if deadline:
                wait_time = min(wait_time, deadline - time.monotonic() + 0.001)
            if wait_time <= 0:
                return False
            with self._condition:
                self._condition.wait(wait_time)

    def _try_acquire(self, tokens: float) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                self._total_acquired += 1
                return True
            return False

    def _refill(self):
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate)
        self._last_refill = now

    def configure(self, rate: float | None = None, burst: int | None = None) -> None:
        """热更新限流参数（None 表示保持不变）。

        供 Registry.get_or_create 对已存在实例应用新配置，
        使同名 agent 的速率/容量变更立即生效。
        """
        with self._lock:
            if rate is not None:
                self.rate = rate
            if burst is not None:
                self.burst = burst
            self._condition.notify_all()

    def update_rate(self, new_rate: float):
        """动态调整速率"""
        with self._lock:
            self.rate = new_rate
            # P0 修复: 速率变更后必须唤醒所有等待线程，使其重新计算等待时间
            # （原先不调用 notify_all，导致速率调高后阻塞的 acquire 无法及时唤醒）
            self._condition.notify_all()
            _logger.info(f"[RateLimiter] {self.name} 速率调整为 {new_rate}/s")

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "rate": self.rate,
                "burst": self.burst,
                "available_tokens": round(self._tokens, 1),
                "total_acquired": self._total_acquired,
                "total_blocked": self._total_blocked,
            }


class RateLimiterRegistry:
    """全局限流器注册中心"""

    def __init__(self):
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.RLock()

    def get_or_create(self, name: str, rate: float | None = None,
                      burst: int | None = None) -> RateLimiter:
        """按名称获取限流器，不存在则以默认 rate=10.0/burst=20 创建。

        更新策略（update_if_exists 语义）：实例已存在时，将本次显式
        传入的 rate/burst 通过 configure() 热更新到该实例并复用，
        保证同名 agent 的限流配置变更立即生效；未传参数则保持原配置。
        """
        with self._lock:
            limiter = self._limiters.get(name)
            if limiter is None:
                limiter = RateLimiter(rate=10.0 if rate is None else rate,
                                      burst=20 if burst is None else burst,
                                      name=name)
                self._limiters[name] = limiter
            elif rate is not None or burst is not None:
                limiter.configure(rate=rate, burst=burst)
            return limiter

    def get(self, name: str) -> RateLimiter | None:
        with self._lock:
            return self._limiters.get(name)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return {k: v.stats() for k, v in self._limiters.items()}
