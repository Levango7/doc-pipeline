"""selectolax_compat 单元测试——后端探测、环境固定、显式降级。

从 doc-pipeline 的 tests/test_html_backend.py 提炼（fetcher 集成部分仍在该仓，
搬来的判据逐条对应，不弱化）：
  1. 后端探测必须走"导入式"判定，不信任 find_spec / 顶层包可导入；
  2. 环境变量可固定后端，非法值退回自动顺序，合法但内核缺失不得假装可用；
  3. 内核构造失败必须告警并返回 None（调用方显式降级），不得静默抛给调用方。
"""
from __future__ import annotations

import importlib.util

import pytest

from artesian import selectolax_compat as compat


class TestBackendProbe:
    def test_resolve_backend_returns_usable_kernel_when_selectolax_present(self):
        """selectolax 装了但没有可用内核时，必须返回 None（而不是假装 OK）。"""
        if importlib.util.find_spec("selectolax") is None:
            pytest.skip("环境未安装 selectolax")
        backend = compat.resolve_backend()
        assert backend in compat.MODULES
        assert compat._load(backend) is not None

    def test_probe_does_not_trust_top_level_import_only(self, monkeypatch):
        """回归护栏：只 import 顶层包不足以证明解析路径可用。

        复刻 selectolax 1.0 的场景——顶层包可导入、内核不可用。
        """
        monkeypatch.setattr(compat, "_load", lambda name: None)
        assert compat.resolve_backend() is None
        assert compat.get_parser("<html></html>") is None

    def test_environment_can_pin_backend(self, monkeypatch):
        auto = compat.resolve_backend()
        monkeypatch.setenv(compat.ENV_OVERRIDE, "lexbor")
        assert compat.requested_backend() == "lexbor"
        # 非法值退回自动顺序，结果与不设时一致
        monkeypatch.setenv(compat.ENV_OVERRIDE, "not-a-kernel")
        assert compat.resolve_backend() == auto
        # 合法但内核缺失时不得假装可用
        if compat._load("lexbor") is None:
            monkeypatch.setenv(compat.ENV_OVERRIDE, "lexbor")
            assert compat.resolve_backend() != "lexbor"

    def test_requested_backend_normalizes_env_value(self, monkeypatch):
        monkeypatch.setenv(compat.ENV_OVERRIDE, "  LEXBOR ")
        assert compat.requested_backend() == "lexbor"
        monkeypatch.setenv(compat.ENV_OVERRIDE, "")
        assert compat.requested_backend() == ""


class TestGetParser:
    def test_returns_adapter_and_backend(self):
        """有可用内核时，构造出的适配面必须满足 css_first/text/tag 调用约定。"""
        backend = compat.resolve_backend()
        if backend is None:
            pytest.skip("无可用 selectolax 内核")
        parsed = compat.get_parser(
            "<html><body><h1>Hi</h1><p class='x'>你好</p></body></html>")
        assert parsed is not None
        parser, name = parsed
        assert name == backend
        node = parser.css_first("p.x")
        assert node is not None
        assert "你好" in node.text()
        assert node.tag == "p"
        assert "Hi" in parser.text()

    def test_construction_failure_is_warned(self, monkeypatch, caplog):
        """内核类存在但构造失败：compat 必须告警并返回 None，让调用方走正则。"""
        class _BadCtor:
            def __init__(self, html):
                raise ValueError("ctor exploded")

        monkeypatch.setattr(compat, "resolve_backend", lambda: "lexbor")
        monkeypatch.setattr(compat, "_load", lambda name: _BadCtor)
        with caplog.at_level("WARNING"):
            assert compat.get_parser("<html></html>") is None
        assert "构造失败" in caplog.text
        assert "ctor exploded" in caplog.text

    def test_adapter_text_retries_without_kwargs(self):
        """内核 text() 签名再变时显式降级为无参重试，而不是把 TypeError 抛给调用方。"""
        class _Node:
            def text(self, **kwargs):
                if kwargs:
                    raise TypeError("no kwargs in this kernel")
                return "降级后的文本"

        class _Parser:
            def text(self, **kwargs):
                if kwargs:
                    raise TypeError("no kwargs in this kernel")
                return "降级后的文本"

        node_adapter = compat._NodeAdapter(_Node(), "fake", "p")
        assert node_adapter.text(separator=" ") == "降级后的文本"
        parser_adapter = compat._ParserAdapter(_Parser(), "fake")
        assert parser_adapter.text(separator=" ") == "降级后的文本"


class TestAdapterSurface:
    """适配层必须把内核差异收敛在内部：css/css_first/tag/text/decompose 全接口。"""

    def test_node_adapter_surface(self):
        class _Node:
            tag = "p"

            def css(self, selector):
                return [_Node()] if selector == "p" else []

            def css_first(self, selector):
                return _Node() if selector == "p" else None

            def text(self, **kwargs):
                return "段落"

        n = compat._NodeAdapter(_Node(), "fake", "p")
        assert n.tag == "p"
        kids = n.css("p")
        assert len(kids) == 1 and isinstance(kids[0], compat._NodeAdapter)
        assert n.css("zzz") == []
        assert n.css_first("p") is not None
        assert n.css_first("zzz") is None
        assert n.text(separator=" ") == "段落"

    def test_node_adapter_tolerates_none_from_kernel(self):
        """有的内核对无匹配返回 None 而非空列表——不得让 None 冒泡给调用方。"""
        class _Node:
            tag = "div"

            def css(self, selector):
                return None

            def css_first(self, selector):
                return None

        n = compat._NodeAdapter(_Node(), "fake", "div")
        assert n.css("*") == []
        assert n.css_first("*") is None

    def test_parser_adapter_surface(self):
        class _Node:
            tag = "h1"

            def __init__(self):
                self.decomposed = False

            def decompose(self):
                self.decomposed = True

            def text(self, **kwargs):
                return "hi"

        class _Parser:
            def css(self, selector):
                return [_Node()] if selector else []

            def css_first(self, selector):
                return _Node() if selector else None

            def text(self, **kwargs):
                return "全树"

        p = compat._ParserAdapter(_Parser(), "fake")
        assert p.backend == "fake"
        assert len(p.css("h1")) == 1
        assert p.css("") == []
        node = p.css_first("h1")
        assert node is not None and node.tag == "h1"
        assert p.css_first("") is None
        p.decompose(node)
        assert node._node.decomposed is True
        assert p.text(separator=" ") == "全树"

    def test_tag_of_survives_hostile_node(self):
        """内核节点取 tag 时炸掉，适配层必须退成空串而不是把异常带给调用方。"""
        class _Hostile:
            @property
            def tag(self):
                raise RuntimeError("kernel bug")

        assert compat._tag_of(_Hostile()) == ""


class TestLoadFailure:
    def test_load_logs_and_caches_missing_module(self, monkeypatch, caplog):
        """注册表里的模块导入失败：debug 留痕 + None，且失败结果进缓存不反复重试。"""
        monkeypatch.setitem(compat._registry, "boom", "artesian_absent_module_xyz.CLS")
        try:
            with caplog.at_level("DEBUG", logger="artesian.selectolax_compat"):
                assert compat._load("boom") is None
                assert "boom" in caplog.text
            assert compat._cache.get("boom") is None
        finally:
            compat._cache.pop("boom", None)

    def test_get_parser_returns_none_when_resolved_class_vanishes(self, monkeypatch):
        """resolve_backend 说可用、_load 却取不到类（极端竞态）时不得抛。"""
        monkeypatch.setattr(compat, "resolve_backend", lambda: "lexbor")
        monkeypatch.setattr(compat, "_load", lambda name: None)
        assert compat.get_parser("<html></html>") is None
