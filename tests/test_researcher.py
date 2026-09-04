"""ResearcherAgent 单元测试 — 补齐覆盖率薄弱区（原 19%）。

覆盖范围：
- handle() 全流程：空查询 / 噪音清洗 / spec.scope 增强 / 配置覆盖 / 并行与串行
- _search 缓存命中、mock 短路、SearchEngineManager 路径、内置引擎回退
- 各搜索引擎：prosearch（subprocess）、HTML 抓取（requests mock）、Bing 解析、DuckDuckGo
- 结果处理：_normalize_results / _clean_queries / _clean_query_text /
  _is_spam_url / _normalize_url / _score_and_filter / _relevance_score / _deduplicate
- 历史记录、异步搜索（search_async / _search_http_async / parallel_search_async）、is_healthy
"""
import asyncio
import json
import sys
import types
from unittest.mock import MagicMock, patch

from agents.researcher import ResearcherAgent, SearchResult
from pipeline_core.base_agent import AgentMeta, Message


def _make_agent(**cfg) -> ResearcherAgent:
    config = {"quiet": True, "search_engines": ["mock"]}
    config.update(cfg)
    return ResearcherAgent(
        "researcher",
        AgentMeta(name="researcher", version="2.0"),
        config, None, None,
    )


def _msg(payload: dict) -> Message:
    return Message(topic="researcher.input", payload=payload, from_agent="test")


class TestSearchResult:
    def test_to_dict_roundtrip(self):
        r = SearchResult(title="t", url="u", snippet="s", source="src",
                         query="q", score=0.5, relevance=0.8)
        d = r.to_dict()
        assert d["title"] == "t" and d["score"] == 0.5 and d["relevance"] == 0.8
        assert "fetched_at" in d


class TestConfig:
    def test_nested_researcher_config(self):
        agent = _make_agent(researcher={"search_engines": ["bing"]})
        assert agent._search_engines == ["bing"]

    def test_low_quality_domains_from_config(self):
        agent = _make_agent(low_quality_domains=["bad.example.com"])
        assert agent._low_quality_domains == {"bad.example.com"}

    def test_prosearch_paths_from_env_and_config(self, monkeypatch):
        monkeypatch.setenv("PROSEARCH_SCRIPT_PATH", "/env/path.cjs")
        agent = _make_agent(prosearch_paths=["/cfg/path.cjs"])
        assert "/env/path.cjs" in agent._prosearch_paths
        assert "/cfg/path.cjs" in agent._prosearch_paths

    def test_on_config_update_resets_manager(self):
        agent = _make_agent()
        agent._search_manager = object()
        agent.on_config_update(["search_engines"])
        assert agent._search_manager is None


class TestHandle:
    def test_empty_queries_returns_empty(self):
        agent = _make_agent()
        res = agent.handle(_msg({"task_id": "t1", "queries": []}))
        assert res["total"] == 0 and res["results"] == []

    def test_noise_queries_cleaned_to_empty(self):
        agent = _make_agent()
        res = agent.handle(_msg({"task_id": "t2",
                                 "queries": ["这是一个测试", "ab", "请生成一份文档"]}))
        assert res["total"] == 0

    def test_mock_engine_end_to_end(self):
        agent = _make_agent()
        res = agent.handle(_msg({"task_id": "t3", "queries": ["Kafka 核心架构"]}))
        assert res["status"] == "ok"
        assert res["total"] >= 1
        assert res["engines_used"] == ["mock"]
        assert res["results"][0]["source"] == "mock"
        # 历史记录已落
        assert agent.get_search_history()[-1]["task_id"] == "t3"

    def test_spec_scope_extends_queries(self):
        agent = _make_agent()
        res = agent.handle(_msg({
            "task_id": "t4", "queries": [],
            "spec": {"scope": ["分布式消息队列", "高可用架构"]},
        }))
        assert res["query_count"] == 2

    def test_payload_config_overrides_max_results(self):
        agent = _make_agent()
        res = agent.handle(_msg({
            "task_id": "t5",
            "queries": ["查询甲", "查询乙", "查询丙"],
            "config": {"max_results": 1},
        }))
        assert res["total"] <= 1

    def test_mock_agent_ignores_real_engines_in_payload(self):
        agent = _make_agent()  # search_engines=["mock"]
        res = agent.handle(_msg({
            "task_id": "t6", "queries": ["Kafka 架构"],
            "config": {"search_engines": ["bing", "sogou"]},
        }))
        assert res["engines_used"] == ["mock"]  # mock 模式不被 payload 覆盖

    def test_parallel_search_path(self):
        agent = _make_agent()
        res = agent.handle(_msg({
            "task_id": "t7",
            "queries": ["查询词甲", "查询词乙", "查询词丙", "查询词丁"],
            "parallel": True,
        }))
        assert res["total"] >= 1

    def test_serial_search_exception_logged(self):
        agent = _make_agent()
        with patch.object(agent, "_search", side_effect=RuntimeError("boom")):
            res = agent.handle(_msg({"task_id": "t8", "queries": ["Kafka 架构"],
                                     "parallel": False}))
        assert res["total"] == 0


class TestSearch:
    def test_cache_hit(self):
        agent = _make_agent()
        cached = [SearchResult(title="CT", url="https://c.com", snippet="CS",
                               source="mock", query="q").to_dict()]
        # C13：缓存 key 不含 task_id（跨任务缓存语义），格式为 query|engines=<集合>
        key = f"cached-query|engines={','.join(agent._search_engines)}"
        agent._cache.set(key, cached)
        out = agent._search("cached-query", "t1")
        assert len(out) == 1 and out[0].title == "CT"
        # 跨任务同查询同样命中（task_id 不参与 key）
        out2 = agent._search("cached-query", "another-task")
        assert len(out2) == 1 and out2[0].title == "CT"

    def test_mock_only_short_circuit(self):
        agent = _make_agent()
        out = agent._search("任意查询", "t2", engines=["mock"])
        assert len(out) == 1 and out[0].source == "mock"

    def test_manager_path_returns_early(self):
        agent = _make_agent(search_engines=["bing"])
        mgr = MagicMock()
        mgr.is_available.return_value = True
        mgr.get_engine_names.return_value = ["bing"]
        items = [MagicMock(title=f"T{i}", url=f"https://x{i}.com",
                           snippet=f"S{i}", source="bing") for i in range(6)]
        mgr.search_with_sites.return_value = items
        agent._search_manager = mgr
        out = agent._search("查询", "t3")
        assert len(out) == 6
        mgr.search_with_sites.assert_called_once()

    def test_fallback_to_builtin_engines(self):
        agent = _make_agent(search_engines=["unknown_engine"])
        agent._search_manager = None
        with patch("pipeline_core.search_engines.SearchEngineManager.from_env",
                   side_effect=RuntimeError("no mgr")):
            out = agent._search("查询", "t4")
        # 未知引擎走 else 分支 → _mock_search
        assert len(out) == 1 and out[0].source == "unknown_engine"

    def test_engine_exception_continues(self):
        agent = _make_agent(search_engines=["boom_engine"])
        with patch.object(agent, "_mock_search", side_effect=RuntimeError("eng down")), \
                patch("pipeline_core.search_engines.SearchEngineManager.from_env",
                      side_effect=RuntimeError("no mgr")):
            out = agent._search("查询", "t5")
        assert out == []


class TestProsearch:
    def test_no_script_returns_empty(self):
        agent = _make_agent()
        agent._prosearch_paths = []
        assert agent._prosearch("q") == []

    def test_script_success(self, tmp_path):
        agent = _make_agent()
        script = tmp_path / "prosearch.cjs"
        script.write_text("// fake", encoding="utf-8")
        agent._prosearch_paths = [str(script)]
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = json.dumps(
            {"results": [{"title": "PT", "url": "https://p.com", "snippet": "PS"}]})
        with patch("subprocess.run", return_value=fake_result):
            out = agent._prosearch("q")
        assert len(out) == 1 and out[0].title == "PT"

    def test_script_nonzero_exit(self, tmp_path):
        agent = _make_agent()
        script = tmp_path / "prosearch.cjs"
        script.write_text("// fake", encoding="utf-8")
        agent._prosearch_paths = [str(script)]
        fake_result = MagicMock(returncode=1, stdout="")
        with patch("subprocess.run", return_value=fake_result):
            assert agent._prosearch("q") == []

    def test_subprocess_exception(self, tmp_path):
        agent = _make_agent()
        script = tmp_path / "prosearch.cjs"
        script.write_text("// fake", encoding="utf-8")
        agent._prosearch_paths = [str(script)]
        with patch("subprocess.run", side_effect=OSError("node missing")):
            assert agent._prosearch("q") == []


class _FakeRequestsResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestHtmlSearch:
    HTML = (
        '<a href="https://good.com/page" title="好结果标题">链接文本</a>'
        '这是摘要文本，足够长以便被提取出来作为 snippet 使用。'
        '<a href="https://skip.sogou.com/x" title="跳过域名">x</a>'
        '<a href="https://good.com/style.css">css</a>'
        '<a href="https://good.com/page" title="重复 URL">dup</a>'
    )

    def test_extract_results(self):
        agent = _make_agent()
        with patch("requests.get",
                   return_value=_FakeRequestsResponse(200, self.HTML)):
            out = agent._html_search("q", "sogou", "https://www.sogou.com/web",
                                     {"query": "q"}, ["sogou.com"])
        assert len(out) == 1
        assert out[0].url == "https://good.com/page"
        assert out[0].title == "好结果标题"
        assert out[0].source == "sogou"

    def test_non_200_returns_empty(self):
        agent = _make_agent()
        with patch("requests.get", return_value=_FakeRequestsResponse(503, "")):
            assert agent._html_search("q", "e", "https://x", {}, []) == []

    def test_request_exception_returns_empty(self):
        agent = _make_agent()
        with patch("requests.get", side_effect=OSError("net down")):
            assert agent._html_search("q", "e", "https://x", {}, []) == []


class TestBingSearch:
    HTML = (
        '<li class="b_algo"><h2><a href="https://real.com/a">真实结果</a></h2>'
        '<p>这是 Bing 搜索结果的摘要文本内容。</p></li>'
        '<li class="b_algo"><h2><a href="https://www.microsoft.com/x">MS</a></h2></li>'
        '<li class="b_algo"><h2>no link</h2></li>'
    )

    def test_parse_b_algo_blocks(self):
        agent = _make_agent()
        with patch("requests.get", return_value=_FakeRequestsResponse(200, self.HTML)):
            out = agent._bing_search("q")
        assert len(out) == 1
        assert out[0].url == "https://real.com/a"
        assert out[0].title == "真实结果"

    def test_non_200(self):
        agent = _make_agent()
        with patch("requests.get", return_value=_FakeRequestsResponse(403, "")):
            assert agent._bing_search("q") == []

    def test_exception(self):
        agent = _make_agent()
        with patch("requests.get", side_effect=OSError("boom")):
            assert agent._bing_search("q") == []


class TestSogouAnd360:
    def test_sogou_delegates_to_html_search(self):
        agent = _make_agent()
        agent._html_search = MagicMock(return_value=[])
        agent._sogou_search("q")
        kwargs = agent._html_search.call_args.kwargs
        assert kwargs["engine_name"] == "sogou"

    def test_360_delegates_to_html_search(self):
        agent = _make_agent()
        agent._html_search = MagicMock(return_value=[])
        agent._360_search("q")
        kwargs = agent._html_search.call_args.kwargs
        assert kwargs["engine_name"] == "360"


class TestDuckDuckGoSearch:
    def test_module_not_installed_returns_empty(self):
        agent = _make_agent()
        # 显式强制 ImportError（CI 安装了 duckduckgo_search，不能依赖环境假设）
        with patch.dict(sys.modules, {"duckduckgo_search": None}):
            assert agent._duckduckgo_search("q") == []

    def test_success_with_fake_module(self):
        agent = _make_agent()
        fake_mod = types.ModuleType("duckduckgo_search")

        class _DDGS:
            def __init__(self, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, query, max_results=10):
                return [{"title": "DT", "href": "https://d.com", "body": "DB"}]

        fake_mod.DDGS = _DDGS
        with patch.dict(sys.modules, {"duckduckgo_search": fake_mod}):
            out = agent._duckduckgo_search("q")
        assert len(out) == 1 and out[0].title == "DT"

    def test_ddgs_runtime_error(self):
        agent = _make_agent()
        fake_mod = types.ModuleType("duckduckgo_search")

        class _DDGS:
            def __init__(self, timeout=None):
                raise RuntimeError("blocked")

        fake_mod.DDGS = _DDGS
        with patch.dict(sys.modules, {"duckduckgo_search": fake_mod}):
            assert agent._duckduckgo_search("q") == []


class TestNormalizeResults:
    def test_dict_with_results(self):
        agent = _make_agent()
        data = {"results": [{"title": "T", "url": "U", "snippet": "S", "source": "x"}]}
        out = agent._normalize_results(data, "q")
        assert len(out) == 1 and out[0].title == "T"

    def test_content_field_fallback(self):
        agent = _make_agent()
        data = {"results": [{"title": "T", "url": "U", "content": "C"}]}
        out = agent._normalize_results(data, "q")
        assert out[0].snippet == "C"

    def test_non_dict_items_skipped(self):
        agent = _make_agent()
        data = {"results": ["not-a-dict", {"title": "T", "url": "U"}]}
        assert len(agent._normalize_results(data, "q")) == 1


class TestQueryCleaning:
    def test_clean_queries_filters_noise(self):
        agent = _make_agent()
        kept = agent._clean_queries([
            "Kafka 核心架构",      # 保留
            "这是一个测试用例",     # 噪音
            "abc",                # 太短
            "！！？？",            # 纯标点
            "用于验证流水线的查询",  # 噪音
        ])
        assert kept == ["Kafka 核心架构"]

    def test_clean_query_text(self):
        agent = _make_agent()
        assert agent._clean_query_text("1. **Kafka 架构**") == "Kafka 架构"
        assert agent._clean_query_text("[链接文本](https://x.com)") == "链接文本"
        assert agent._clean_query_text("- 列表项") == "列表项"
        assert agent._clean_query_text("A—B") == "A - B"


class TestSpamAndUrlNorm:
    def test_is_spam_url(self):
        agent = _make_agent()
        assert agent._is_spam_url("https://doubleclick.net/ad")
        assert agent._is_spam_url("https://sub.doubleclick.net/x")
        assert not agent._is_spam_url("https://example.com/")
        assert not agent._is_spam_url("not a url")

    def test_normalize_url_strips_tracking(self):
        agent = _make_agent()
        out = agent._normalize_url("https://x.com/p?a=1&utm_source=feed&fbclid=abc")
        assert "utm_source" not in out and "fbclid" not in out
        assert "a=1" in out

    def test_normalize_url_keeps_plain(self):
        agent = _make_agent()
        assert agent._normalize_url("https://x.com/p") == "https://x.com/p"


class TestScoreAndFilter:
    def _r(self, title="Kafka 核心架构详解", url="https://good.com/a",
           snippet="Kafka 是一个分布式消息队列系统，核心架构包括 broker 与分区。",
           source="bing", query="Kafka 核心架构"):
        return SearchResult(title=title, url=url, snippet=snippet,
                            source=source, query=query)

    def test_spam_and_tracking_dropped(self):
        agent = _make_agent()
        results = [
            self._r(url="https://doubleclick.net/ad"),
            self._r(url="https://x.com/click/redirect?to=1"),
            self._r(),
        ]
        out = agent._score_and_filter(results)
        assert len(out) == 1

    def test_bad_title_dropped(self):
        agent = _make_agent()
        out = agent._score_and_filter([self._r(title="!!!")])
        assert out == []

    def test_low_quality_domain_penalty(self):
        agent = _make_agent(low_quality_domains=["lowq.com"])
        penalized = agent._score_and_filter([self._r(url="https://lowq.com/a")])
        normal = agent._score_and_filter([self._r(url="https://good.com/a")])
        assert (penalized[0].score if penalized else 0) < (normal[0].score if normal else 0)

    def test_sorted_by_score_desc(self):
        agent = _make_agent()
        strong = self._r()  # 标题+摘要全命中
        weak = self._r(title="Kafka 相关", snippet="完全不相关的摘要文本内容填充。",
                       url="https://other.com/b")
        out = agent._score_and_filter([weak, strong])
        assert len(out) == 2 and out[0].score >= out[1].score


class TestRelevanceScore:
    def test_empty_query_neutral(self):
        agent = _make_agent()
        r = SearchResult(title="t", url="u", snippet="s", source="x", query="")
        assert agent._relevance_score(r, "") == 0.5

    def test_stopword_only_query_neutral(self):
        agent = _make_agent()
        r = SearchResult(title="t", url="u", snippet="s", source="x", query="的 了 是")
        assert agent._relevance_score(r, "的 了 是") == 0.5

    def test_full_and_partial_hit(self):
        agent = _make_agent()
        r = SearchResult(title="Kafka 架构", url="u",
                         snippet="Kafka 架构 详解", source="x", query="Kafka 架构")
        assert agent._relevance_score(r, "Kafka 架构") == 1.0
        r2 = SearchResult(title="无关标题", url="u", snippet="无关摘要",
                          source="x", query="Kafka 架构")
        assert agent._relevance_score(r2, "Kafka 架构") == 0.0


class TestDeduplicate:
    def test_url_and_title_dedup(self):
        agent = _make_agent()
        results = [
            SearchResult(title="T1", url="https://x.com/a?utm_source=1",
                         snippet="s", source="x", query="q"),
            SearchResult(title="T1-dup", url="https://x.com/a",
                         snippet="s", source="x", query="q"),  # 同裸 URL
            SearchResult(title="T1", url="https://y.com/b",
                         snippet="s", source="x", query="q"),  # 同标题
            SearchResult(title="T2", url="https://z.com/c",
                         snippet="s", source="x", query="q"),
        ]
        out = agent._deduplicate(results)
        assert len(out) == 2
        assert {r.title for r in out} == {"T1", "T2"}


class TestHistoryAndCache:
    def test_history_trims_to_max(self):
        agent = _make_agent(max_history=3)
        for i in range(5):
            agent._record_history(f"t{i}", ["q"], 1)
        hist = agent.get_search_history()
        assert len(hist) == 3
        assert hist[-1]["task_id"] == "t4"

    def test_clear_cache(self):
        agent = _make_agent()
        agent._cache.set("k", "v")
        agent.clear_cache()
        assert agent._cache.get("k") is None


class TestAsyncSearch:
    def test_search_async_falls_back_to_executor(self):
        agent = _make_agent()
        out = asyncio.run(agent.search_async("Kafka 架构", ["mock"], "t1"))
        assert len(out) == 1 and out[0].source == "mock"

    def test_search_http_async_no_http_engines_returns_none(self):
        agent = _make_agent()
        out = asyncio.run(agent._search_http_async("q", ["mock"]))
        assert out is None  # 触发同步回退

    def test_search_http_async_with_fake_engine(self):
        agent = _make_agent()
        eng = MagicMock()
        eng.api_url = "https://api.example.com/search"
        eng.build_params.return_value = {"q": "q"}
        eng.headers = {}
        eng.parse_response.return_value = [
            SearchResult(title="AT", url="https://a.com", snippet="AS",
                         source="api", query="q")]
        mgr = MagicMock()
        mgr._engines = {"api_eng": eng}
        agent._search_manager = mgr

        from tests.test_writer import _fake_aiohttp_module
        fake = _fake_aiohttp_module(post_result={"ignored": True})
        with patch.dict(sys.modules, {"aiohttp": fake}):
            out = asyncio.run(agent._search_http_async("q", ["api_eng"]))
        assert out is not None and len(out) == 1 and out[0].title == "AT"

    def test_search_http_async_engine_error_logged(self):
        agent = _make_agent()
        eng = MagicMock()
        eng.api_url = "https://api.example.com/search"
        eng.build_params.return_value = {"q": "q"}
        eng.headers = {}
        mgr = MagicMock()
        mgr._engines = {"api_eng": eng}
        agent._search_manager = mgr

        from tests.test_writer import _fake_aiohttp_module
        fake = _fake_aiohttp_module(post_error=OSError("api down"))
        with patch.dict(sys.modules, {"aiohttp": fake}):
            out = asyncio.run(agent._search_http_async("q", ["api_eng"]))
        assert out == []

    def test_parallel_search_async_mixed_results(self):
        agent = _make_agent()

        async def _fake_search(query, engines=None, task_id=""):
            if query == "bad":
                raise RuntimeError("query failed")
            return [SearchResult(title=query, url=f"https://x.com/{query}",
                                 snippet="s", source="mock", query=query)]

        agent.search_async = _fake_search
        out = asyncio.run(agent.parallel_search_async(["good1", "bad", "good2"], "t"))
        assert len(out) == 2

    def test_is_healthy(self):
        agent = _make_agent()
        assert agent.is_healthy() is True
        with patch.object(agent._cache, "set", side_effect=RuntimeError("cache down")):
            assert agent.is_healthy() is False
