"""SearchEngines — 多引擎 fallback / 排序 / 去重 / LRU 缓存

测试原则：
  - 用 unittest.mock 模拟 HTTP 调用，不实际请求网络
  - 每个测试方法聚焦一个行为
"""
import sys
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.search_engines import (
    SearchItem,
    SearchEngineBase,
    SearchEngineManager,
    BochaEngine,
    TavilyEngine,
    SerperEngine,
    MetasoEngine,
    DuckDuckGoEngine,
    BingEngine,
    BaiduEngine,
    SogouEngine,
    So360Engine,
    ProSearchEngine,
    FirecrawlExtractor,
    create_engine,
    _ENGINE_REGISTRY,
)


# ─── SearchItem 数据类 ────────────────────────────

class TestSearchItem:
    """SearchItem 序列化/反序列化"""

    def test_to_dict_roundtrip(self):
        """to_dict → from_dict 往返保持数据"""
        item = SearchItem(
            title="Kafka 架构", url="http://kafka.org",
            snippet="分布式流处理", source="bocha", query="kafka",
            score=0.9, relevance=0.8,
        )
        d = item.to_dict()
        restored = SearchItem.from_dict(d)
        assert restored.title == item.title
        assert restored.url == item.url
        assert restored.snippet == item.snippet
        assert restored.source == item.source
        assert restored.score == item.score

    def test_from_dict_defaults(self):
        """from_dict 缺失字段使用默认值"""
        item = SearchItem.from_dict({"title": "t", "url": "u"})
        assert item.snippet == ""
        assert item.source == ""
        assert item.score == 0.0


# ─── 引擎可用性 ────────────────────────────

class TestEngineAvailability:
    """各引擎 is_available 行为"""

    def test_bocha_requires_api_key(self):
        """Bocha 无 key 不可用"""
        assert BochaEngine(api_key="").is_available() is False
        assert BochaEngine(api_key="test").is_available() is True

    def test_tavily_requires_api_key(self):
        """Tavily 无 key 不可用"""
        assert TavilyEngine(api_key="").is_available() is False
        assert TavilyEngine(api_key="test").is_available() is True

    def test_serper_requires_api_key(self):
        """Serper 无 key 不可用"""
        assert SerperEngine(api_key="").is_available() is False
        assert SerperEngine(api_key="test").is_available() is True

    def test_metaso_requires_api_key(self):
        """Metaso 无 key 不可用"""
        assert MetasoEngine(api_key="").is_available() is False
        assert MetasoEngine(api_key="test").is_available() is True

    def test_duckduckgo_always_available(self):
        """DuckDuckGo 无需 key，始终可用"""
        assert DuckDuckGoEngine().is_available() is True

    def test_bing_always_available(self):
        """Bing HTML 抓取始终可用"""
        assert BingEngine().is_available() is True

    def test_baidu_always_available(self):
        """百度 HTML 抓取始终可用"""
        assert BaiduEngine().is_available() is True

    def test_sogou_always_available(self):
        """搜狗 HTML 抓取始终可用"""
        assert SogouEngine().is_available() is True

    def test_360_always_available(self):
        """360 HTML 抓取始终可用"""
        assert So360Engine().is_available() is True

    def test_prosearch_requires_script(self):
        """ProSearch 需要本地脚本"""
        eng = ProSearchEngine()
        # 脚本存在性取决于环境，但 is_available 应返回 bool
        assert isinstance(eng.is_available(), bool)


# ─── 引擎搜索（mock HTTP）────────────────────────────

class TestBochaSearch:
    """Bocha 引擎搜索"""

    def test_search_no_key_returns_empty(self):
        """无 key 时返回空列表"""
        eng = BochaEngine(api_key="")
        assert eng.search("query") == []

    def test_search_with_mock_response(self):
        """mock 响应解析正确"""
        eng = BochaEngine(api_key="test")
        mock_body = (
            '{"data":{"webPages":{"value":['
            '{"name":"Kafka","url":"http://kafka.org","summary":"stream","score":0.9},'
            '{"name":"Spark","url":"http://spark.org","summary":"compute","score":0.8}'
            ']}}}'
        ).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_body
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = eng.search("kafka", max_results=10)
        assert len(results) == 2
        assert results[0].title == "Kafka"
        assert results[0].source == "bocha"

    def test_search_http_error_returns_empty(self):
        """HTTP 错误时返回空列表（不抛异常）"""
        eng = BochaEngine(api_key="test")
        with patch("urllib.request.urlopen", side_effect=Exception("conn refused")):
            results = eng.search("query")
        assert results == []


class TestTavilySearch:
    """Tavily 引擎搜索"""

    def test_search_with_mock_response(self):
        """mock 响应解析正确"""
        eng = TavilyEngine(api_key="test")
        mock_body = (
            b'{"results":['
            b'{"title":"Doc","url":"http://doc.io","content":"content","score":0.95}'
            b']}'
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_body
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = eng.search("test", max_results=5)
        assert len(results) == 1
        assert results[0].title == "Doc"


class TestSerperSearch:
    """Serper 引擎搜索"""

    def test_search_with_knowledge_graph(self):
        """知识卡片插入到结果开头"""
        eng = SerperEngine(api_key="test")
        mock_body = (
            b'{"organic":['
            b'{"title":"Result","link":"http://r.com","snippet":"s","score":0.5}'
            b'],"knowledgeGraph":{"title":"Kafka","website":"http://kafka.org","description":"streaming"}}'
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_body
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = eng.search("kafka")
        assert results[0].title == "Kafka"  # 知识卡片在前
        assert results[0].source == "serper[kg]"


# ─── SearchEngineManager fallback ────────────────────────────

class TestSearchEngineManagerFallback:
    """多引擎 fallback 行为"""

    def _make_manager(self):
        """创建带 mock 引擎的管理器"""
        eng1 = MagicMock(spec=SearchEngineBase)
        eng1.name = "eng1"
        eng1.is_available.return_value = True
        eng1.search.return_value = [
            SearchItem(title="a", url="http://a", snippet="", source="eng1", query="q"),
        ]
        eng2 = MagicMock(spec=SearchEngineBase)
        eng2.name = "eng2"
        eng2.is_available.return_value = True
        eng2.search.return_value = [
            SearchItem(title="b", url="http://b", snippet="", source="eng2", query="q"),
        ]
        return SearchEngineManager({"eng1": eng1, "eng2": eng2})

    def test_search_returns_results(self):
        """search 返回结果"""
        mgr = self._make_manager()
        results = mgr.search("q", max_results=10)
        assert len(results) >= 1
        assert all(isinstance(r, SearchItem) for r in results)

    def test_search_falls_back_on_exception(self):
        """第一个引擎异常时 fallback 到第二个"""
        eng1 = MagicMock(spec=SearchEngineBase)
        eng1.name = "eng1"
        eng1.is_available.return_value = True
        eng1.search.side_effect = Exception("eng1 down")
        eng2 = MagicMock(spec=SearchEngineBase)
        eng2.name = "eng2"
        eng2.is_available.return_value = True
        eng2.search.return_value = [
            SearchItem(title="b", url="http://b", snippet="", source="eng2", query="q"),
        ]
        mgr = SearchEngineManager({"eng1": eng1, "eng2": eng2})
        results = mgr.search("q")
        assert len(results) == 1
        assert results[0].source == "eng2"

    def test_search_deduplicates_by_url(self):
        """相同 URL 去重"""
        eng1 = MagicMock(spec=SearchEngineBase)
        eng1.name = "eng1"
        eng1.is_available.return_value = True
        eng1.search.return_value = [
            SearchItem(title="a", url="http://dup", snippet="", source="eng1", query="q"),
        ]
        eng2 = MagicMock(spec=SearchEngineBase)
        eng2.name = "eng2"
        eng2.is_available.return_value = True
        eng2.search.return_value = [
            SearchItem(title="b", url="http://dup", snippet="", source="eng2", query="q"),
            SearchItem(title="c", url="http://unique", snippet="", source="eng2", query="q"),
        ]
        mgr = SearchEngineManager({"eng1": eng1, "eng2": eng2})
        results = mgr.search("q", max_results=10)
        urls = [r.url for r in results]
        assert urls.count("http://dup") == 1
        assert "http://unique" in urls

    def test_search_skips_unavailable_engine(self):
        """不可用引擎被跳过"""
        eng1 = MagicMock(spec=SearchEngineBase)
        eng1.name = "eng1"
        eng1.is_available.return_value = False
        eng2 = MagicMock(spec=SearchEngineBase)
        eng2.name = "eng2"
        eng2.is_available.return_value = True
        eng2.search.return_value = [
            SearchItem(title="b", url="http://b", snippet="", source="eng2", query="q"),
        ]
        mgr = SearchEngineManager({"eng1": eng1, "eng2": eng2})
        results = mgr.search("q", engines=["eng1", "eng2"])
        assert len(results) == 1
        eng1.search.assert_not_called()

    def test_search_stops_when_enough_results(self):
        """结果足够时停止后续引擎"""
        eng1 = MagicMock(spec=SearchEngineBase)
        eng1.name = "eng1"
        eng1.is_available.return_value = True
        eng1.search.return_value = [
            SearchItem(title=f"a{i}", url=f"http://a{i}", snippet="", source="eng1", query="q")
            for i in range(5)
        ]
        eng2 = MagicMock(spec=SearchEngineBase)
        eng2.name = "eng2"
        eng2.is_available.return_value = True
        mgr = SearchEngineManager({"eng1": eng1, "eng2": eng2})
        mgr.search("q", max_results=5, engines=["eng1", "eng2"])
        eng2.search.assert_not_called()

    def test_search_caches_results(self):
        """相同查询命中缓存"""
        eng1 = MagicMock(spec=SearchEngineBase)
        eng1.name = "eng1"
        eng1.is_available.return_value = True
        eng1.search.return_value = [
            SearchItem(title="a", url="http://a", snippet="", source="eng1", query="q"),
        ]
        mgr = SearchEngineManager({"eng1": eng1})
        # 第一次调用
        r1 = mgr.search("q", max_results=10)
        # 第二次相同查询
        r2 = mgr.search("q", max_results=10)
        assert len(r1) == len(r2)
        # 引擎只被调用一次（第二次命中缓存）
        assert eng1.search.call_count == 1


# ─── SearchEngineManager 排序/去重 ────────────────────────────

class TestSearchEngineManagerSorting:
    """结果排序与限制"""

    def test_max_results_limit(self):
        """max_results 限制返回数量"""
        eng = MagicMock(spec=SearchEngineBase)
        eng.name = "eng"
        eng.is_available.return_value = True
        eng.search.return_value = [
            SearchItem(title=f"r{i}", url=f"http://r{i}", snippet="", source="eng", query="q")
            for i in range(20)
        ]
        mgr = SearchEngineManager({"eng": eng})
        results = mgr.search("q", max_results=5)
        assert len(results) == 5


# ─── create_engine 注册表 ────────────────────────────

class TestCreateEngine:
    """create_engine 工厂函数"""

    def test_create_known_engine(self):
        """创建已知引擎"""
        eng = create_engine("bing")
        assert eng is not None
        assert isinstance(eng, BingEngine)

    def test_create_unknown_engine_returns_none(self):
        """未知引擎返回 None"""
        eng = create_engine("nonexistent")
        assert eng is None

    def test_engine_registry_has_all_engines(self):
        """注册表包含所有引擎"""
        expected = {"bocha", "tavily", "serper", "metaso", "duckduckgo",
                    "bing", "baidu", "sogou", "360", "prosearch"}
        assert expected.issubset(set(_ENGINE_REGISTRY.keys()))


# ─── FirecrawlExtractor ────────────────────────────

class TestFirecrawlExtractor:
    """Firecrawl 网页提取"""

    def test_no_key_returns_error(self):
        """无 key 时返回错误"""
        ext = FirecrawlExtractor(api_key="")
        result = ext.scrape("http://example.com")
        assert result["success"] is False
        assert "no API key" in result["error"]

    def test_scrape_success(self):
        """mock 提取成功"""
        ext = FirecrawlExtractor(api_key="test")
        mock_body = b'{"data":{"markdown":"# Title\\n\\nContent","metadata":{"title":"Title"}}}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_body
        mock_resp.__enter__ = lambda self: mock_resp
        mock_resp.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = ext.scrape("http://example.com")
        assert result["success"] is True
        assert "Title" in result["markdown"]


# ─── 异步搜索 ────────────────────────────

class TestAsyncSearch:
    """异步搜索行为"""

    def test_search_async_returns_coroutine(self):
        """search_async 返回协程"""
        eng = MagicMock(spec=SearchEngineBase)
        eng.name = "eng"
        eng.is_available.return_value = True
        async def mock_async(query, max_results):
            return [SearchItem(title="a", url="http://a", snippet="", source="eng", query=query)]
        eng.search_async = mock_async
        mgr = SearchEngineManager({"eng": eng})
        results = asyncio.run(mgr.search_async("q"))
        assert len(results) == 1