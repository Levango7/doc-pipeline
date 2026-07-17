"""
EventHook - 事件钩子系统
========================
供 PEV 框架或其他外部系统订阅 doc-pipeline 生命周期事件。

支持的事件类型：
  - task.created       任务创建
  - task.started       任务开始执行
  - task.completed     任务完成
  - task.failed        任务失败
  - task.cancelled     任务取消
  - agent.started      Agent 启动
  - agent.stopped      Agent 停止
  - agent.error        Agent 异常
  - quality_gate.evaluated  质量门控评分完成
  - quality_gate.regenerate 质量门控触发重做
  - circuit_breaker.open    熔断器打开
  - circuit_breaker.close   熔断器恢复

钩子触发方式：
  - webhook: HTTP POST 回调到指定 URL
  - callback: Python 可调用对象（进程内）

线程安全：所有操作均通过 _lock 保护。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional
from dataclasses import dataclass, field, asdict

_logger = logging.getLogger(__name__)


@dataclass
class Hook:
    """事件钩子定义"""
    id: str
    event: str                          # 订阅的事件类型（支持通配符 * ）
    url: Optional[str] = None           # webhook URL（None 则为 callback 模式）
    headers: dict = field(default_factory=dict)  # webhook 自定义 header
    callback: Optional[Callable] = None # 进程内回调函数
    created_at: float = field(default_factory=time.time)
    call_count: int = 0                # 触发次数
    last_called: float = 0             # 最后触发时间
    enabled: bool = True

    def matches(self, event: str) -> bool:
        """检查事件是否匹配此钩子"""
        if not self.enabled:
            return False
        if self.event == "*" or self.event == event:
            return True
        # 支持前缀通配: "task.*" 匹配 "task.completed"
        if self.event.endswith(".*"):
            prefix = self.event[:-2]
            return event.startswith(prefix + ".")
        return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("callback", None)  # callback 不可序列化
        return d


class EventHookManager:
    """事件钩子管理器（线程安全单例）"""

    def __init__(self):
        self._hooks: dict[str, Hook] = {}
        self._lock = threading.RLock()

    def register(
        self,
        event: str,
        url: Optional[str] = None,
        headers: Optional[dict] = None,
        callback: Optional[Callable] = None,
    ) -> str:
        """注册事件钩子，返回 hook_id"""
        hook_id = str(uuid.uuid4())[:8]
        hook = Hook(
            id=hook_id,
            event=event,
            url=url,
            headers=headers or {},
            callback=callback,
        )
        with self._lock:
            self._hooks[hook_id] = hook
        _logger.info(f"[EventHook] 注册钩子: {event} -> {url or 'callback'} (id={hook_id})")
        return hook_id

    def unregister(self, hook_id: str) -> bool:
        """注销事件钩子"""
        with self._lock:
            if hook_id in self._hooks:
                del self._hooks[hook_id]
                return True
            return False

    def enable(self, hook_id: str) -> bool:
        """启用钩子"""
        with self._lock:
            hook = self._hooks.get(hook_id)
            if hook:
                hook.enabled = True
                return True
            return False

    def disable(self, hook_id: str) -> bool:
        """禁用钩子"""
        with self._lock:
            hook = self._hooks.get(hook_id)
            if hook:
                hook.enabled = False
                return True
            return False

    def list_hooks(self) -> list[dict]:
        """列出所有钩子"""
        with self._lock:
            return [h.to_dict() for h in self._hooks.values()]

    def emit(self, event: str, payload: dict) -> int:
        """触发事件，返回成功调用的钩子数"""
        with self._lock:
            matched = [h for h in self._hooks.values() if h.matches(event)]

        if not matched:
            return 0

        count = 0
        for hook in matched:
            try:
                if hook.callback:
                    hook.callback(event, payload)
                elif hook.url:
                    self._fire_webhook(hook, event, payload)
                hook.call_count += 1
                hook.last_called = time.time()
                count += 1
            except Exception as e:
                _logger.error(f"[EventHook] 钩子 {hook.id} 触发失败: {e}")
        return count

    def _fire_webhook(self, hook: Hook, event: str, payload: dict):
        """发送 webhook HTTP POST 请求"""
        import requests
        body = json.dumps({
            "event": event,
            "payload": payload,
            "hook_id": hook.id,
            "timestamp": time.time(),
        }, ensure_ascii=False, default=str)
        headers = {"Content-Type": "application/json"}
        headers.update(hook.headers)
        try:
            resp = requests.post(hook.url, data=body, headers=headers, timeout=10)
            if resp.status_code >= 400:
                _logger.warning(
                    f"[EventHook] webhook {hook.url} 返回 {resp.status_code}"
                )
        except Exception as e:
            _logger.error(f"[EventHook] webhook {hook.url} 请求失败: {e}")

    def clear(self):
        """清空所有钩子"""
        with self._lock:
            self._hooks.clear()


# ── 全局单例 ──────────────────────────────────────

_global_manager: Optional[EventHookManager] = None
_singleton_lock = threading.Lock()


def get_hook_manager() -> EventHookManager:
    """获取全局 EventHookManager 单例"""
    global _global_manager
    if _global_manager is None:
        with _singleton_lock:
            if _global_manager is None:
                _global_manager = EventHookManager()
    return _global_manager


def emit_event(event: str, payload: dict) -> int:
    """便捷函数：触发事件"""
    return get_hook_manager().emit(event, payload)
