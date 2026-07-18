"""
fast_json - 高性能 JSON 序列化/反序列化
======================================
优先使用 orjson（C 实现，3-10x 快于标准 json），回退到标准 json。
"""
from __future__ import annotations

import json
from typing import Any

try:
    import orjson  # type: ignore

    def dumps(obj: Any, *, default=None, **kwargs) -> str:
        """序列化为 JSON 字符串（orjson 优先）"""
        try:
            return orjson.dumps(obj, default=default).decode("utf-8")
        except Exception:
            return json.dumps(obj, default=default, **kwargs)

    def loads(data: str | bytes) -> Any:
        """反序列化 JSON（orjson 优先）"""
        try:
            return orjson.loads(data)
        except Exception:
            return json.loads(data)

    def dumps_bytes(obj: Any, *, default=None) -> bytes:
        """序列化为 JSON bytes（orjson 原生输出，零拷贝）"""
        try:
            return orjson.dumps(obj, default=default)
        except Exception:
            return json.dumps(obj).encode("utf-8")

    HAS_ORJSON = True
except ImportError:
    def dumps(obj: Any, *, default=None, **kwargs) -> str:
        return json.dumps(obj, default=default, **kwargs)

    def loads(data: str | bytes) -> Any:
        return json.loads(data)

    def dumps_bytes(obj: Any, *, default=None) -> bytes:
        return json.dumps(obj).encode("utf-8")

    HAS_ORJSON = False
