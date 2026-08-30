"""search_engines 补充测试 — 补齐覆盖率薄弱区（原 45%）。

与 test_search_engines.py 互补，覆盖：
- SearchEngineBase 默认实现（NotImplementedError / search_async 包装）
- MockEngine / MetasoEngine / DuckDuckGoEngine / HTML 引擎（Bing/Sogou/360/Baidu）解析
- Bocha/Tavily/Serper 的异步路径与异常分支
- FirecrawlExtractor 空内容/异常分支
- ProSearchEngine（env 路径 + subprocess）
- SearchEngineManager：缓存降级、search_async 并发与提前取消、
  search_with_sites / search_with_sites_async、from_env 各 API Key 分支
"""
import asyncio
import io
import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from pipeline_core.search_engines import (
    BaiduEngine,
    BingEngine,
    BochaEngine,
    DuckDuckGoEngine,
    FirecrawlExtractor,
    HtmlSearchEngine,
    MetasoEngine,
    MockEngine,
    ProSearchEngine,
    SearchEngineBase,
    SearchEngineManager,
    SearchItem,
    SerperEngine,
    So360Engine,
    SogouEngine,
    TavilyEngine,
    create_engine,
)
from tests.test_writer import _fake_aiohttp_module


def _fake_urlopen(body: bytes):
    resp = io.BytesIO(body)
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestBaseEngine:
    def test_base_search_raises(self):
        with pytest.raises(NotImplementedError):
            SearchEngineBase().search("q")

    def test_base_search_async_wraps_sync(self):
        class _Eng(SearchEngineBase):
            def search(self, query, max_results=10):
                return [SearchItem(title="t", url="u", snippet="s",
                                   source="e", query=query)]

        out = asyncio.run(_Eng().search_async("q"))
        assert len(out) == 1 and out[0].title == "t"

    def test_base_is_available_default_true(self):
        assert SearchEngineBase().is_available()


class TestMockEngine:
    def test_search_returns_placeholder(self):
        eng = MockEngine()
        assert eng.is_available()
        out = eng.search("主题")
        assert len(out) == 1
        assert "主题" in out[0].title
        assert out[0].source == "mock"


class TestMetasoEngine:
    def test_search_no_key(self):
        assert MetasoEngine(api_key="").search("q") == []

    def test_search_success_list_items(self):
        eng = MetasoEngine(api_key="k")
        body = json.dumps({"data": {"webpages": [
            {"title": "T", "url": "https://x.com", "snippet": "S"}]}}).encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = eng.search("q")
        assert len(out) == 1 and out[0].title == "T"

    def test_search_dict_items_variant(self):
        eng = MetasoEngine(api_key="k")
        body = json.dumps({"data": {"webpages": {"list": [
            {"title": "T2", "link": "https://y.com", "summary": "S2"}]}}}).encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = eng.search("q")
        assert len(out) == 1 and out[0].url == "https://y.com"
        assert out[0].snippet == "S2"

    def test_search_exception_returns_empty(self):
        eng = MetasoEngine(api_key="k")
        with patch("urllib.request.urlopen", side_effect=OSError("net")):
            assert eng.search("q") == []

    def test_search_async_no_key(self):
        assert asyncio.run(MetasoEngine(api_key="").search_async("q")) == []

    def test_search_async_success(self):
        eng = MetasoEngine(api_key="k")
        body = {"data": {"webpages": [{"title": "AT", "url": "u", "snippet": "s"}]}}
        fake = _fake_aiohttp_module(post_result=body)
        with patch.dict(sys.modules, {"aiohttp": fake}):
            out = asyncio.run(eng.search_async("q"))
        assert len(out) == 1 and out[0].title == "AT"

    def test_search_async_exception(self):
        eng = MetasoEngine(api_key="k")
        fake = _fake_aiohttp_module(post_error=OSError("boom"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            assert asyncio.run(eng.search_async("q")) == []


class TestDuckDuckGoEngine:
    HTML = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.com%2Fp">'
        '真实标题</a>'
        '<a class="result__snippet">摘要文本内容</a>'
    )

    def test_search_parses_and_decodes_uddg(self):
        eng = DuckDuckGoEngine()
        body = self.HTML.encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = eng.search("q")
        assert len(out) == 1
        assert out[0].url == "https://real.com/p"
        assert out[0].title == "真实标题"

    def test_search_exception_returns_empty(self):
        eng = DuckDuckGoEngine()
        with patch("urllib.request.urlopen", side_effect=OSError("blocked")):
            assert eng.search("q") == []


class TestHtmlEngines:
    def test_fetch_html(self):
        eng = HtmlSearchEngine()
        body = "<html>内容</html>".encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            assert "内容" in eng._fetch_html("https://x.com")

    def test_extract_results_filters(self):
        eng = HtmlSearchEngine()
        html = (
            '<a href="https://good.com/a">足够长的标题文本</a>'
            '<a href="https://good.com/a">足够长的标题文本</a>'  # 重复 URL
            '<a href="https://www.bing.com/x">必应自身链接</a>'
            '<a href="https://good.com/b">短</a>'
        )
        out = eng._extract_results(html, "q", 10, "src")
        assert len(out) == 1 and out[0].url == "https://good.com/a"

    def test_extract_results_max_results(self):
        eng = HtmlSearchEngine()
        html = "".join(
            f'<a href="https://good.com/{i}">标题号码{i}号</a>' for i in range(10))
        assert len(eng._extract_results(html, "q", 3, "src")) == 3

    def test_bing_search_parses_blocks(self):
        eng = BingEngine()
        html = ('<li class="b_algo"><a href="https://r.com/a">结果标题</a>'
                '<p>摘要内容文本</p></li>')
        eng._fetch_html = MagicMock(return_value=html)
        out = eng.search("q")
        assert len(out) == 1 and out[0].title == "结果标题"
        assert out[0].snippet == "摘要内容文本"

    def test_bing_search_block_without_link_skipped(self):
        eng = BingEngine()
        eng._fetch_html = MagicMock(return_value='<li class="b_algo">无链接</li>')
        assert eng.search("q") == []

    def test_bing_search_exception(self):
        eng = BingEngine()
        eng._fetch_html = MagicMock(side_effect=OSError("net"))
        assert eng.search("q") == []

    def test_sogou_search_parses_vrwrap(self):
        eng = SogouEngine()
        html = ('<div class="vrwrap"><a href="https://s.com/a">搜狗结果标题</a>'
                '<p class="str-text-info">搜狗摘要</p></div></div>')
        eng._fetch_html = MagicMock(return_value=html)
        out = eng.search("q")
        assert len(out) == 1 and out[0].title == "搜狗结果标题"

    def test_sogou_search_exception(self):
        eng = SogouEngine()
        eng._fetch_html = MagicMock(side_effect=OSError("x"))
        assert eng.search("q") == []

    def test_360_search_parses_res_list(self):
        eng = So360Engine()
        html = ('<li class="res-list"><a href="https://h.com/a">360结果标题</a>'
                '<p>360摘要</p></li>')
        eng._fetch_html = MagicMock(return_value=html)
        out = eng.search("q")
        assert len(out) == 1 and out[0].title == "360结果标题"

    def test_360_search_exception(self):
        eng = So360Engine()
        eng._fetch_html = MagicMock(side_effect=OSError("x"))
        assert eng.search("q") == []

    def test_baidu_search_parses_and_dedups(self):
        eng = BaiduEngine()
        html = (
            '<div class="result c-container"><a href="https://b.com/a">百度标题甲</a>'
            '<span class="content-right_abc">百度摘要甲</span></div></div>'
            '<div class="c-container"><a href="https://b.com/a">百度标题甲</a></div></div>'
            '<div class="c-container"><a href="https://b.com/c">百度标题乙</a></div></div>'
        )
        eng._fetch_html = MagicMock(return_value=html)
        out = eng.search("q")
        assert [r.url for r in out] == ["https://b.com/a", "https://b.com/c"]
        assert out[0].snippet == "百度摘要甲"

    def test_baidu_search_exception(self):
        eng = BaiduEngine()
        eng._fetch_html = MagicMock(side_effect=OSError("x"))
        assert eng.search("q") == []


class TestBochaEngine:
    def test_search_items_dict_variant(self):
        eng = BochaEngine(api_key="k")
        body = json.dumps({"data": {"webPages": {"value": {"value": [
            {"name": "BN", "url": "https://b.com", "summary": "BS", "score": 0.9}]}}}}).encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = eng.search("q")
        assert len(out) == 1 and out[0].title == "BN"
        assert out[0].score == 0.9

    def test_search_exception(self):
        eng = BochaEngine(api_key="k")
        with patch("urllib.request.urlopen", side_effect=OSError("x")):
            assert eng.search("q") == []

    def test_search_async_no_key(self):
        assert asyncio.run(BochaEngine(api_key="").search_async("q")) == []

    def test_search_async_success(self):
        eng = BochaEngine(api_key="k")
        body = {"data": {"webPages": {"value": [
            {"name": "BA", "url": "u", "summary": "s"}]}}}
        fake = _fake_aiohttp_module(post_result=body)
        with patch.dict(sys.modules, {"aiohttp": fake}):
            out = asyncio.run(eng.search_async("q"))
        assert len(out) == 1 and out[0].title == "BA"

    def test_search_async_exception(self):
        eng = BochaEngine(api_key="k")
        fake = _fake_aiohttp_module(post_error=OSError("x"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            assert asyncio.run(eng.search_async("q")) == []


class TestTavilyEngine:
    def test_search_no_key(self):
        assert TavilyEngine(api_key="").search("q") == []

    def test_search_exception(self):
        eng = TavilyEngine(api_key="k")
        with patch("urllib.request.urlopen", side_effect=OSError("x")):
            assert eng.search("q") == []

    def test_search_async_no_key(self):
        assert asyncio.run(TavilyEngine(api_key="").search_async("q")) == []

    def test_search_async_success(self):
        eng = TavilyEngine(api_key="k")
        body = {"results": [{"title": "TT", "url": "u", "content": "c", "score": 0.5}]}
        fake = _fake_aiohttp_module(post_result=body)
        with patch.dict(sys.modules, {"aiohttp": fake}):
            out = asyncio.run(eng.search_async("q"))
        assert len(out) == 1 and out[0].title == "TT"

    def test_search_async_exception(self):
        eng = TavilyEngine(api_key="k")
        fake = _fake_aiohttp_module(post_error=OSError("x"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            assert asyncio.run(eng.search_async("q")) == []


class TestSerperEngine:
    def test_search_no_key(self):
        assert SerperEngine(api_key="").search("q") == []

    def test_search_exception(self):
        eng = SerperEngine(api_key="k")
        with patch("urllib.request.urlopen", side_effect=OSError("x")):
            assert eng.search("q") == []

    def test_search_async_no_key(self):
        assert asyncio.run(SerperEngine(api_key="").search_async("q")) == []

    def test_search_async_with_knowledge_graph(self):
        eng = SerperEngine(api_key="k")
        body = {
            "organic": [{"title": "ST", "link": "https://s.com", "snippet": "ss"}],
            "knowledgeGraph": {"title": "KG", "website": "https://kg.com",
                               "description": "kg-desc"},
        }
        fake = _fake_aiohttp_module(post_result=body)
        with patch.dict(sys.modules, {"aiohttp": fake}):
            out = asyncio.run(eng.search_async("q"))
        assert out[0].source == "serper[kg]"  # 知识卡片插到最前
        assert out[1].title == "ST"

    def test_search_async_exception(self):
        eng = SerperEngine(api_key="k")
        fake = _fake_aiohttp_module(post_error=OSError("x"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            assert asyncio.run(eng.search_async("q")) == []


class TestFirecrawlExt:
    def test_scrape_empty_content(self):
        fc = FirecrawlExtractor(api_key="k")
        body = json.dumps({"data": {"markdown": "", "metadata": {"title": "T"}}}).encode()
        with patch("urllib.request.urlopen", return_value=_fake_urlopen(body)):
            out = fc.scrape("https://x.com")
        assert out["success"] is False
        assert out["error"] == "empty content"

    def test_scrape_exception(self):
        fc = FirecrawlExtractor(api_key="k")
        with patch("urllib.request.urlopen", side_effect=OSError("api down")):
            out = fc.scrape("https://x.com")
        assert out["success"] is False and "api down" in out["error"]


class TestProSearchEngine:
    def test_env_script_path_picked_up(self, tmp_path, monkeypatch):
        script = tmp_path / "prosearch.cjs"
        script.write_text("// fake", encoding="utf-8")
        monkeypatch.setenv("PROSEARCH_SCRIPT_PATH", str(script))
        eng = ProSearchEngine()
        assert eng.is_available()
        assert eng._script == str(script)

    def test_search_no_script(self):
        eng = ProSearchEngine()
        eng._script = None
        assert eng.search("q") == []

    def test_search_list_output(self, tmp_path):
        eng = ProSearchEngine()
        eng._script = str(tmp_path / "x.cjs")
        fake = MagicMock(returncode=0, stdout=json.dumps(
            [{"title": "PT", "url": "https://p.com", "snippet": "PS"}]))
        with patch("subprocess.run", return_value=fake):
            out = eng.search("q")
        assert len(out) == 1 and out[0].title == "PT"

    def test_search_dict_output(self, tmp_path):
        eng = ProSearchEngine()
        eng._script = str(tmp_path / "x.cjs")
        fake = MagicMock(returncode=0, stdout=json.dumps(
            {"results": [{"title": "PD", "link": "https://d.com", "summary": "S"}]}))
        with patch("subprocess.run", return_value=fake):
            out = eng.search("q")
        assert len(out) == 1 and out[0].url == "https://d.com"

    def test_search_nonzero_exit_and_exception(self, tmp_path):
        eng = ProSearchEngine()
        eng._script = str(tmp_path / "x.cjs")
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            assert eng.search("q") == []
        with patch("subprocess.run", side_effect=OSError("node missing")):
            assert eng.search("q") == []


class TestCreateEngineExt:
    def test_bad_kwargs_returns_none(self):
        assert create_engine("mock", nonexistent_kwarg=1) is None


class _FakeEngine(SearchEngineBase):
    def __init__(self, name, items=None, fail=False, delay=0.0):
        self.name = name
        self._items = items or []
        self._fail = fail
        self._delay = delay

    def is_available(self):
        return True

    def search(self, query, max_results=10):
        if self._fail:
            raise RuntimeError(f"{self.name} failed")
        return self._items[:max_results]

    async def search_async(self, query, max_results=10):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError(f"{self.name} async failed")
        return self._items[:max_results]


def _items(n, prefix):
    return [SearchItem(title=f"{prefix}{i}", url=f"https://{prefix}.com/{i}",
                       snippet="s", source=prefix, query="q") for i in range(n)]


class TestSearchEngineManagerExt:
    def test_cache_unavailable_degrades(self):
        with patch("pipeline_core.cache_manager.CacheManager",
                   side_effect=RuntimeError("no cache")):
            mgr = SearchEngineManager({"mock": MockEngine()})
        assert mgr._cache is None
        out = mgr.search("q")  # 无缓存仍可搜索
        assert len(out) == 1

    def test_is_available_and_names_and_add(self):
        mgr = SearchEngineManager({})
        assert not mgr.is_available()
        mgr.add_engine("mock", MockEngine())
        assert mgr.is_available()
        assert mgr.get_engine_names() == ["mock"]

    def test_search_async_parallel_merges(self):
        mgr = SearchEngineManager({
            "e1": _FakeEngine("e1", _items(2, "a")),
            "e2": _FakeEngine("e2", _items(2, "b")),
        })
        out = asyncio.run(mgr.search_async("q", max_results=10))
        assert len(out) == 4

    def test_search_async_failure_counted(self):
        mgr = SearchEngineManager({
            "bad": _FakeEngine("bad", fail=True),
            "good": _FakeEngine("good", _items(1, "g")),
        })
        out = asyncio.run(mgr.search_async("q"))
        assert len(out) == 1
        assert mgr._fail_counts.get("bad") == 1

    def test_search_async_early_cancel_when_enough(self):
        mgr = SearchEngineManager({
            "fast": _FakeEngine("fast", _items(5, "f")),
            "slow": _FakeEngine("slow", _items(5, "s"), delay=2.0),
        })
        import time
        t0 = time.monotonic()
        out = asyncio.run(mgr.search_async("q", max_results=3))
        elapsed = time.monotonic() - t0
        assert len(out) == 3
        assert elapsed < 1.5  # 拿够即停，不等慢引擎

    def test_search_with_sites_sync(self):
        mgr = SearchEngineManager({"mock": MockEngine()})
        out = mgr.search_with_sites("主题", max_results=5,
                                    sites=[("example.com", "示例")])
        assert len(out) >= 1
        # 站点结果来源带站点名后缀
        assert any("[示例]" in item.source for item in out)

    def test_search_with_sites_async(self):
        mgr = SearchEngineManager({"mock": MockEngine()})

        async def _run():
            return await mgr.search_with_sites_async(
                "主题", max_results=5, sites=[("example.com", "示例")])
        out = asyncio.run(_run())
        assert len(out) >= 1

    def test_search_with_sites_skips_site_batch_when_full(self):
        """性能护栏：常规搜索已满额时跳过站点批次（不发起多余 site: 查询）"""
        calls = []

        class _CountingMock(MockEngine):
            def search(self, query, max_results=10):
                calls.append(query)
                return super().search(query, max_results)

        mgr = SearchEngineManager({"mock": _CountingMock()})
        # mock 每次返回 1 条；max_results=1 → 常规搜索后立即满额
        out = mgr.search_with_sites("主题", max_results=1,
                                    sites=[("example.com", "示例")])
        assert len(out) == 1
        assert calls == ["主题"]  # 只有常规查询，没有 site: 查询

    def test_search_with_sites_uses_only_first_engine_for_sites(self):
        """性能护栏：站点搜索只用首个引擎（防 9 站点 × 多引擎串行超时）"""
        calls = []

        class _RecMock(MockEngine):
            def search(self, query, max_results=10):
                calls.append((self.name, query))
                return super().search(query, max_results)

        # 两引擎：站点查询若走多引擎会被调用多次
        mgr = SearchEngineManager({"e1": _RecMock(), "e2": MockEngine()})
        out = mgr.search_with_sites("主题", max_results=5,
                                    sites=[("example.com", "示例")])
        assert len(out) >= 1
        site_calls = [c for _, c in calls if c.startswith("site:")]
        assert len(site_calls) == 1  # 站点查询只发起一次（首引擎）
        # 显式传多引擎时站点也只用首个（换查询词避开 manager 缓存）
        calls.clear()
        out2 = mgr.search_with_sites("主题乙", max_results=5,
                                     sites=[("example.com", "示例")],
                                     engines=["e1", "e2"])
        assert len(out2) >= 1
        site_calls2 = [c for _, c in calls if c.startswith("site:")]
        assert len(site_calls2) == 1

    def test_status_reports_fail_counts(self):
        mgr = SearchEngineManager({"e": _FakeEngine("e", fail=True)})
        mgr.search("q")
        st = mgr.status()
        assert st["engines"]["e"]["fail_count"] == 1
        assert st["engines"]["e"]["available"] is True


class TestFromEnv:
    def test_from_env_with_api_keys(self, tmp_path):
        env = {
            "BOCHA_API_KEY": "bk",
            "TAVILY_API_KEY": "tk",
            "SERPER_API_KEY": "sk",
            "METASO_API_KEY": "mk",
        }
        with patch("pipeline_core.llm_router._load_env", return_value=env):
            mgr = SearchEngineManager.from_env()
        names = mgr.get_engine_names()
        for expected in ("bocha", "tavily", "serper", "metaso",
                         "duckduckgo", "bing", "baidu", "sogou", "360"):
            assert expected in names

    def test_from_env_without_keys_warns_and_html_only(self):
        with patch("pipeline_core.llm_router._load_env", return_value={}):
            mgr = SearchEngineManager.from_env()
        names = mgr.get_engine_names()
        assert not ({"bocha", "tavily", "serper", "metaso"} & set(names))
        assert "bing" in names

    def test_from_env_with_prosearch_script(self, tmp_path, monkeypatch):
        script = tmp_path / "prosearch.cjs"
        script.write_text("// fake", encoding="utf-8")
        monkeypatch.setenv("PROSEARCH_SCRIPT_PATH", str(script))
        with patch("pipeline_core.llm_router._load_env", return_value={}):
            mgr = SearchEngineManager.from_env()
        assert "prosearch" in mgr.get_engine_names()
