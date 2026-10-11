"""进程内 LRU+TTL 缓存——取数层的可选加速件。

搜索结果这类数据"同一查询在短窗口内重复出现"是常态，而每一次都打真实引擎
既慢又消耗配额；把这类缓存放进库而不是留给消费方，是因为**失效规则本身
就是取数语义的一部分**（TTL 到期、LRU 逐出、命中顺序影响逐出对象）。

TTL 的三档语义是刻意保留的，别当冗余分支删：

- `ttl > 0`：条目写入 `ttl` 秒后失效（读取时判定并顺手删除）；
- `ttl == 0`：**永不过期**，只受容量约束；
- `ttl < 0`：**整块缓存关闭**——`get` 恒 None、`set` 空转。
  给消费方一个"关掉缓存但别改调用点"的开关，比到处 `if cache:` 干净。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class LRUCache:
    """线程安全的 LRU + TTL 缓存。"""

    def __init__(self, max_size: int = 1000, ttl: int = 3600):
        if max_size <= 0:
            raise ValueError(f"max_size 必须为正，收到 {max_size}")
        self.max_size = max_size
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _expired(self, ts: float) -> bool:
        return self.ttl > 0 and time.time() - ts > self.ttl

    def get(self, key: str) -> Any | None:
        """命中返回值，未命中/已过期返回 None。命中会把该条移到 LRU 尾部。"""
        if self.ttl < 0:
            return None
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, ts = entry
            if self._expired(ts):
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        """写入。容量满时先逐出最久未使用的一条。"""
        if self.ttl < 0:
            return
        with self._lock:
            while len(self._data) >= self.max_size:
                self._data.popitem(last=False)
            self._data[key] = (value, time.time())
            self._data.move_to_end(key)

    def remove(self, key: str) -> None:
        """删除单条；不存在时不报错。"""
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._data)
