"""HTML 解析后端兼容层。

背景：selectolax 1.0 起 `selectolax.parser.HTMLParser`（modest 内核）在导入时
主动 raise ImportError 提示改用 `selectolax.lexbor.LexborHTMLParser`。此前 fetcher
直接 `from selectolax.parser import HTMLParser` 并在外层 `except Exception: pass`，
于是生产安装（selectolax 1.0）静默退到正则启发式，而 `run.py --check` 因为只探测
顶层包仍报 OK——快路径烂掉了没有任何信号。

本模块把三件事补齐：
  1. 按后端可用性选择解析器（modest → lexbor → quick），统一适配
     `css/css_first/decompose/tag/text(separator, strip)`；
  2. 降级一律显式记录（logger.warning + 计数），不再静默；
  3. 供 `--check`/测试断言"到底在用哪个后端"，而不是仅"包能否 import"。
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Any

logger = logging.getLogger("pipeline_core.selectolax_compat")

# 优先级：modest（0.x 的 C 内核，与既有测试行为一致）→ lexbor（1.0 起的推荐内核）
MODULES = ("modest", "lexbor")
ENV_OVERRIDE = "DOC_PIPELINE_HTML_BACKEND"

_registry: dict[str, str] = {
    "modest": "selectolax.parser.HTMLParser",
    "lexbor": "selectolax.lexbor.LexborHTMLParser",
}

_cache: dict[str, type | None] = {}


class _NodeAdapter:
    """统一 modest Node 与 lexbor Node 的 .text() / .css() 调用面。

    两个内核都接受 `text(separator=..., strip=...)`（本机 0.4.11 与 1.0.0 实测），
    保留一次无参重试只是为了内核签名再变时显式降级，而不是静默抛给调用方。
    """

    __slots__ = ("_node", "_backend", "_tag")

    def __init__(self, node: Any, backend: str, tag: str) -> None:
        self._node = node
        self._backend = backend
        self._tag = tag

    @property
    def tag(self) -> str:
        return self._tag

    def css(self, selector: str) -> list[_NodeAdapter]:
        return [_NodeAdapter(n, self._backend, _tag_of(n))
                for n in (self._node.css(selector) or [])]

    def css_first(self, selector: str) -> _NodeAdapter | None:
        node = self._node.css_first(selector)
        return _NodeAdapter(node, self._backend, _tag_of(node)) if node else None

    def text(self, **kwargs: Any) -> str:
        try:
            return str(self._node.text(**kwargs))
        except (TypeError, AttributeError):
            return str(self._node.text())


def _tag_of(node: Any) -> str:
    try:
        return str(node.tag()) if callable(node.tag) else str(node.tag)
    except Exception:
        return ""


class _ParserAdapter:
    """统一三类 selectolax 解析器接口；不支持的操作显式抛错由调用方降级。"""

    def __init__(self, parser: Any, backend: str) -> None:
        self._parser = parser
        self._backend = backend

    @property
    def backend(self) -> str:
        return self._backend

    def css(self, selector: str) -> list[_NodeAdapter]:
        return [_NodeAdapter(n, self._backend, _tag_of(n))
                for n in (self._parser.css(selector) or [])]

    def css_first(self, selector: str) -> _NodeAdapter | None:
        node = self._parser.css_first(selector)
        if node is None:
            return None
        return _NodeAdapter(node, self._backend, _tag_of(node))

    def decompose(self, node: _NodeAdapter) -> None:
        node._node.decompose()

    def text(self, **kwargs: Any) -> str:
        # 两个内核都有全树 text()；只有参数签名可能不同，退一次就够
        try:
            return str(self._parser.text(**kwargs))
        except (TypeError, AttributeError):
            return str(self._parser.text())


def requested_backend() -> str:
    """测试/运维可用环境变量固定后端；空串表示按可用性自动选。"""
    return (os.environ.get(ENV_OVERRIDE) or "").strip().lower()


def _load(name: str) -> type | None:
    if name in _cache:
        return _cache[name]
    dotted = _registry.get(name)
    cls: type | None = None
    if dotted:
        module_path, _, attr = dotted.rpartition(".")
        try:
            cls = getattr(importlib.import_module(module_path), attr)
        except Exception as e:  # 除 ImportError 外，内核在导入期主动报错也要捕获
            logger.debug("HTML 后端 %s 不可用: %s", name, e)
            cls = None
    _cache[name] = cls
    return cls


def resolve_backend() -> str | None:
    """返回当前可用的 C 后端名，全部不可用时 None（调用方须走正则）。"""
    override = requested_backend()
    order = (override,) if override in MODULES else MODULES
    for name in order:
        if _load(name) is not None:
            return name
    return None


def get_parser(html: str) -> tuple[_ParserAdapter, str] | None:
    """构造解析器；无可用后端或构造失败返回 None（调用方显式降级）。"""
    backend = resolve_backend()
    if backend is None:
        return None
    cls = _load(backend)
    if cls is None:
        return None
    try:
        return _ParserAdapter(cls(html), backend), backend
    except Exception as e:
        logger.warning("HTML 后端 %s 构造失败，本次降级: %s", backend, e)
        return None
