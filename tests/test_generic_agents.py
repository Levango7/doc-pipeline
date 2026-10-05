"""通用 Agent 套件（http_request / transform）的行为与安全边界测试。

这两个 Agent 是"引擎能不能跑非文档工作流"的分水岭：没有通用件时，接一个新 API
就得新增一个领域 Agent。因此这里既测形状（挑字段、过滤、渲染），也重点测
**通用件带进来的攻击面**：出网校验、不跟随重定向、体积上限、以及
"取不到值就报错而不是渲染成空"。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from pipeline_core.base_agent import Message  # noqa: E402


def _msg(payload):
    return Message(topic="generic.input", payload=payload, from_agent="test")


class _Resp:
    """最小 requests.Response 替身。"""

    def __init__(self, body=b"{}", status=200, headers=None, encoding="utf-8", chunks=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}
        self.encoding = encoding
        self._chunks = chunks if chunks is not None else [body]

    def iter_content(self, chunk_size=8192):
        return iter(self._chunks)

    def close(self):
        pass


def _http_agent(**cfg):
    from agents.http_request_agent import HttpRequestAgent
    merged = {"url": "", "method": "GET", "headers": {}, "body_artifact": "",
              "timeout_s": 5.0, "max_bytes": 200_000, "expect": "json",
              "allow_hosts": []}
    merged.update(cfg)
    return HttpRequestAgent(name="http_request", meta=MagicMock(), config=merged,
                            message_bus=MagicMock(), registry=MagicMock())


def _transform_agent(**cfg):
    from agents.transform_agent import TransformAgent
    merged = {"items": "", "fields": [], "where": {}, "set": [], "template": ""}
    merged.update(cfg)
    return TransformAgent(name="transform", meta=MagicMock(), config=merged,
                          message_bus=MagicMock(), registry=MagicMock())


class TestHttpGuard:
    """出网校验是这轮改动唯一真正的新攻击面，逐条钉住。"""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080/x",
        "http://169.254.169.254/latest/meta-data/",      # 云元数据
        "http://localhost/x",
        "http://10.0.0.5/internal",
        "file:///etc/passwd",
    ])
    def test_private_and_non_http_targets_are_blocked(self, url):
        agent = _http_agent(url=url)
        with patch("agents.http_request_agent.requests.request") as req:
            out = agent.handle(_msg({}))
        assert out["status"] == "blocked", out
        assert "SSRF" in out["error"]
        req.assert_not_called(), "校验没过就不该发出任何请求"

    def test_url_from_upstream_goes_through_the_same_guard(self):
        """URL 由数据决定是工作流引擎的常态，不能因为是上游产物就跳过校验。"""
        agent = _http_agent()
        with patch("agents.http_request_agent.requests.request") as req:
            out = agent.handle(_msg({"upstream": {"url": "http://127.0.0.1/"}}))
        assert out["status"] == "error" and "url" in out["error"]
        req.assert_not_called()

    def test_allow_hosts_is_exact_and_leaves_a_trail(self):
        """内网放行只能精确主机名，且结果里要写明走的是名单而不是公网校验。"""
        agent = _http_agent(url="http://db.internal:5432/ping", allow_hosts=["db.internal"])
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(b'{"ok":true}')) as req:
            out = agent.handle(_msg({}))
        assert out["status"] == "ok" and out["guard"] == "allowlist", out
        assert req.called

        from agents.http_request_agent import _host_allowed
        # 前缀/后缀都不算命中：条目要等于 urlparse 出来的主机名
        assert _host_allowed("http://db.internal.attacker.com/x", ["db.internal"]) is False
        assert _host_allowed("http://notdb.internal/x", ["db.internal"]) is False
        assert _host_allowed("http://DB.INTERNAL/x", ["db.internal"]) is True
        # 名单没命中时，私网地址照旧被守卫拦下
        sneaky = _http_agent(url="http://127.0.0.1/x", allow_hosts=["db.internal"])
        with patch("agents.http_request_agent.requests.request") as req2:
            assert sneaky.handle(_msg({}))["status"] == "blocked"
            req2.assert_not_called()

    def test_redirects_are_never_followed(self):
        """跟一次跳转就等于把"校验过的 URL"换成"没校验过的 URL"。"""
        agent = _http_agent(url="https://example.com/api")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(b"", status=302,
                                       headers={"location": "http://127.0.0.1/x"})):
            out = agent.handle(_msg({}))
        assert out["http_status"] == 302 and out["redirect_to"] == "http://127.0.0.1/x"
        assert out["status"] == "ok", "重定向交给下游判断，引擎不代它绕校验"

    def test_request_is_made_without_redirect_following(self):
        agent = _http_agent(url="https://example.com/api")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(b"{}")) as req:
            agent.handle(_msg({}))
        assert req.call_args.kwargs["allow_redirects"] is False
        assert req.call_args.kwargs["timeout"] == 5.0

    def test_oversized_response_is_blocked_not_truncated(self):
        """截断的 JSON 看起来可能仍然"像数据"，必须显式失败。"""
        agent = _http_agent(url="https://example.com/api", max_bytes=10)
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(chunks=[b"x" * 8, b"y" * 8])):
            out = agent.handle(_msg({}))
        assert out["status"] == "blocked" and "max_bytes" in out["error"]

    def test_secrets_never_leak_into_the_result(self):
        """请求头里的凭据不回显，响应头里的 Set-Cookie 打码。

        产物会进 checkpoint、报表与日志，等于一次凭据外泄面。
        """
        agent = _http_agent(url="https://example.com/api",
                            headers={"Authorization": "Bearer sk-live-123",
                                     "X-Trace": "abc"})
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(b"{}", headers={"Set-Cookie": "session=xyz",
                                                      "X-Trace": "abc"})):
            out = agent.handle(_msg({}))
        assert "sk-live-123" not in json.dumps(out, ensure_ascii=False), out
        assert out["headers"]["Set-Cookie"] == "***"
        assert out["headers"]["X-Trace"] == "abc"


class TestHttpShape:
    def test_ok_json_response_becomes_the_declared_artifact(self):
        agent = _http_agent(url="https://example.com/api")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(json.dumps(
                       [{"id": 1, "title": "甲"}], ensure_ascii=False).encode("utf-8"))):
            out = agent.handle(_msg({}))
        assert out["status"] == "ok" and out["response"] == [{"id": 1, "title": "甲"}]
        assert out["guard"] == "public" and out["http_status"] == 200

    def test_4xx_and_5xx_are_business_failures(self):
        for code, body in ((404, b'{"message":"Not Found"}'), (500, b'oops')):
            agent = _http_agent(url="https://example.com/api", expect="text")
            with patch("agents.http_request_agent.requests.request",
                       return_value=_Resp(body, status=code)):
                out = agent.handle(_msg({}))
            assert out["status"] == "error" and str(code) in out["error"], out

    def test_json_expected_but_broken_is_an_error_not_a_string(self):
        agent = _http_agent(url="https://example.com/api")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(b"<html>nope</html>")):
            out = agent.handle(_msg({}))
        assert out["status"] == "error" and "JSON" in out["error"]

    def test_body_comes_from_a_declared_artifact(self):
        agent = _http_agent(url="https://example.com/api", method="POST",
                            body_artifact="data")
        with patch("agents.http_request_agent.requests.request",
                   return_value=_Resp(b'{"ok":1}')) as req:
            out = agent.handle(_msg({"upstream": {"data": {"a": 1}}}))
        assert out["status"] == "ok"
        assert req.call_args.kwargs["json"] == {"a": 1}

    def test_missing_body_artifact_fails_loudly(self):
        agent = _http_agent(url="https://example.com/api", method="POST",
                            body_artifact="data")
        out = agent.handle(_msg({"upstream": {}}))
        assert out["status"] == "error" and "body_artifact" in out["error"]

    @pytest.mark.parametrize("cfg,needle", [
        ({"url": ""}, "未给出 url"),
        ({"url": "https://x.dev", "method": "TRACE"}, "method 非法"),
        ({"url": "https://x.dev", "expect": "yaml"}, "expect 非法"),
    ])
    def test_bad_input_is_refused_before_any_socket(self, cfg, needle):
        agent = _http_agent(**cfg)
        with patch("agents.http_request_agent.requests.request") as req:
            out = agent.handle(_msg({}))
        assert out["status"] == "error" and needle in out["error"], out
        req.assert_not_called()

    def test_network_exception_is_reported_not_swallowed(self):
        """订阅者返回 None / 空 dict 会被引擎当失败，这里确保异常也带原因。"""
        agent = _http_agent(url="https://example.com/api")
        with patch("agents.http_request_agent.requests.request",
                   side_effect=ConnectionError("reset by peer")):
            out = agent.handle(_msg({}))
        assert out["status"] == "error" and "reset by peer" in out["error"]


class TestTransformShape:
    def test_picks_fields_from_a_list_artifact(self):
        agent = _transform_agent(items="artifacts.response", fields=["id", "title"])
        out = agent.handle(_msg({"upstream": {"response": [
            {"id": 1, "title": "甲", "junk": "x"}, {"id": 2, "title": "乙"}]}}))
        assert out["status"] == "ok"
        assert out["data"] == [{"id": 1, "title": "甲"}, {"id": 2, "title": "乙"}]
        assert out["count"] == 2

    def test_where_uses_the_same_condition_language(self):
        agent = _transform_agent(items="artifacts.response", fields=["id"],
                                 where={"path": "item.id", "op": ">", "value": 1})
        out = agent.handle(_msg({"upstream": {"response": [{"id": 1}, {"id": 2}]}}))
        assert out["data"] == [{"id": 2}]

    def test_set_supports_only_len_and_dotted_paths(self):
        agent = _transform_agent(items="artifacts.response",
                                 set=[{"name": "total", "from": "len(items)"},
                                      {"name": "first", "from": "items.0.id"}])
        out = agent.handle(_msg({"upstream": {"response": [{"id": 7}, {"id": 8}]}}))
        assert out["total"] == 2 and out["first"] == 7

    def test_template_renders_resolvable_paths(self):
        agent = _transform_agent(items="artifacts.response", fields=["id"],
                                 template="共 {{len(items)}} 条，首条 {{items.0.id}}")
        out = agent.handle(_msg({"upstream": {"response": [{"id": 9}]}}))
        assert out["text"] == "共 1 条，首条 9"

    def test_declared_produces_match_the_engine_contract(self):
        """产物键必须与 PRODUCES 声明一致，否则下游拿不到。

        `content` 是刻意多挂的一份：落盘与质检那批节点按 CONSUMES=["content"] 取正文，
        通用件要能直接接上它们。
        """
        from agents.transform_agent import PRODUCES
        agent = _transform_agent(items="artifacts.response", template="{{len(items)}}")
        out = agent.handle(_msg({"upstream": {"response": []}}))
        assert set(out) & set(PRODUCES) == {"data", "text", "content"}


class TestTransformRefusesToGuess:
    """这一组是"取不到值不许变成空"的判据：静默空值会渲染出看起来合法的产物。"""

    def test_missing_items_path_is_an_error(self):
        agent = _transform_agent(items="artifacts.nope")
        out = agent.handle(_msg({"upstream": {"response": []}}))
        assert out["status"] == "error" and "items 路径取不到" in out["error"]

    def test_items_must_be_a_list(self):
        agent = _transform_agent(items="artifacts.response")
        out = agent.handle(_msg({"upstream": {"response": {"a": 1}}}))
        assert out["status"] == "error" and "必须是列表" in out["error"]

    def test_missing_field_in_an_item_is_not_dropped_silently(self):
        agent = _transform_agent(items="artifacts.response", fields=["id", "title"])
        out = agent.handle(_msg({"upstream": {"response": [{"id": 1}]}}))
        assert out["status"] == "error" and "title" in out["error"]

    def test_unresolvable_template_variable_does_not_render_empty(self):
        agent = _transform_agent(template="标题 {{items.0.titel}}")
        out = agent.handle(_msg({"upstream": {}, "dependencies_results": {}}))
        assert out["status"] == "error" and "取不到值" in out["error"]

    def test_set_from_unresolvable_is_an_error(self):
        agent = _transform_agent(set=[{"name": "x", "from": "nope.path"}])
        out = agent.handle(_msg({"upstream": {}}))
        assert out["status"] == "error" and "set.from" in out["error"]

    def test_bad_where_spec_is_rejected_at_handle_time(self):
        agent = _transform_agent(items="artifacts.response",
                                 where={"path": "item.id", "op": "matches", "value": 1})
        out = agent.handle(_msg({"upstream": {"response": [{"id": 1}]}}))
        assert out["status"] == "error" and "where 非法" in out["error"]

    def test_set_entries_must_be_name_from_pairs(self):
        agent = _transform_agent(set=["id"])
        out = agent.handle(_msg({"upstream": {}}))
        assert out["status"] == "error" and "set 项必须是" in out["error"]

    def test_no_expression_language_is_evaluated(self):
        """配置里写代码片段只会被当成文本，绝不求值。

        带引号/方括号的占位符进不了 `_VAR_RE`，于是原样留在渲染结果里——
        既不执行，也不静默抹掉（抹掉就等于"少了一段却看不出问题"）。
        """
        agent = _transform_agent(template="{{__import__('os').getcwd()}}")
        out = agent.handle(_msg({"upstream": {}}))
        assert out["status"] == "ok"
        assert out["text"] == "{{__import__('os').getcwd()}}"
        assert len(out["text"]) < 40, "真的执行了的话，这里会是工作目录路径"


class TestGenericAgentsAreWired:
    """出厂必须真有人用：能力没接进任何流水线就等于零。"""

    def test_shipped_api_pipeline_uses_both_agents(self):
        from pipeline_core.naming import agent_of
        from pipeline_core.scheduler import Scheduler

        plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(PROJECT / "pipelines")).parse(
            "api-report", verify_lock=True)
        agents = [agent_of(n.agent_name) for lvl in plan.levels for n in lvl]
        assert "http_request" in agents and "transform" in agents
        assert "safe_writer" in agents, "没有落盘节点的通用流水线不该算交付"

    def test_registry_meta_declares_the_new_topics(self):
        from pipeline_core.agent_loader import AgentLoader
        from pipeline_core.registry import Registry

        reg = Registry(enable_health_check=False)
        AgentLoader(reg, MagicMock(), str(PROJECT / "agents"), strict_safety=True).register()
        for name in ("http_request", "transform"):
            meta = reg.get_meta(name)
            assert meta is not None, f"{name} 没被自动发现（文件名/AGENT_NAME 不一致？）"
            assert f"{name}.input" in (meta.input_topics or [])

    def test_both_agents_pass_the_ast_sandbox_scan(self):
        """出网与转换件必须能在 strict 沙箱下加载：真出网靠的是 url 校验而不是免检。"""
        from pipeline_core.agent_loader import _check_safety

        for fname in ("http_request_agent.py", "transform_agent.py"):
            dangers = _check_safety(PROJECT / "agents" / fname, strict=False)
            assert dangers == [], f"{fname} 命中黑名单: {dangers}"


class TestApiReportEndToEnd:
    """通用件必须能跑通一条出厂流水线，而不是只能被单测直接调用。

    离线跑法：只把 requests 换成罐头响应，其余全走真件
    （Scheduler → DAGExecutor → 真 transform → 真 safe_writer 落盘）。
    断言落在交付物本身：文件存在、含渲染结果、没有残留的占位符。
    """

    CANNED = json.dumps({
        "full_name": "apache/kafka",
        "description": "分布式事件流平台",
        "stargazers_count": 42,
        "forks_count": 7,
        "language": "Java",
    }, ensure_ascii=False).encode("utf-8")

    def _orch_and_plan(self, tmp_path, out_file):
        from pipeline_core import PipelineOrchestrator
        from pipeline_core.scheduler import Scheduler

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(PROJECT / "pipelines")).parse(
            "api-report", verify_lock=False)
        # safe_writer 的目标由 pipeline.output 决定；测试里指到 tmp，别写进仓库
        plan.raw.setdefault("pipeline", {})["output"] = str(out_file)
        inp = tmp_path / "in.md"
        inp.write_text("\n".join(["# 主题", "", "通用 API 工作流"]) + "\n",
                       encoding="utf-8")
        return orch, plan, inp

    def test_pipeline_runs_and_delivers(self, tmp_path, monkeypatch):
        from unittest.mock import patch as upatch

        monkeypatch.chdir(tmp_path)
        out = tmp_path / "report.md"
        orch, plan, inp = self._orch_and_plan(tmp_path, out)
        try:
            with upatch("agents.http_request_agent.requests.request",
                        return_value=_Resp(self.CANNED,
                                           headers={"Content-Type": "application/json"})):
                task = orch.run_plan(plan, input_file=str(inp), wait=True)
        finally:
            orch.shutdown()

        assert task.status.value == "done", f"{task.status.value}: {task.error}"
        assert out.exists(), "跑通了却没有交付物，就是 #13 那类假 done"
        body = out.read_text(encoding="utf-8")
        assert "apache/kafka" in body and "Stars: 42" in body, body[:300]
        assert "{{" not in body, f"模板没被渲染：{body[:300]}"

    def test_engine_sinks_still_govern_the_generic_pipeline(self, tmp_path, monkeypatch):
        """把接口打挂：通用流水线也必须如实失败，而不是拿空产物报 done。"""
        from unittest.mock import patch as upatch

        monkeypatch.chdir(tmp_path)
        orch, plan, inp = self._orch_and_plan(tmp_path, tmp_path / "never.md")
        try:
            with upatch("agents.http_request_agent.requests.request",
                        side_effect=ConnectionError("dns down")):
                task = orch.run_plan(plan, input_file=str(inp), wait=True)
        finally:
            orch.shutdown()
        assert task.status.value == "failed", task.status
        assert "dns down" in (task.error or ""), task.error


class TestLegacyAutoAdmission:
    """`LEGACY_AUTO = False`：通用件不进 legacy 自动图，但照旧注册、照旧被声明式调用。

    legacy 路径（`orch.run`）按设计不读 YAML，它的图就是"全体注册件"，于是任何
    "没有节点级配置就跑不出东西"的件被拉进来必然业务失败——实测：新增 http_request
    之后 `tests/test_resume_recovery.py` 的断点续传 E2E 报 "未给出 url"。
    准入放在件的声明里，而不是把这类件的失败洗成"跳过即成功"。
    """

    def _orch(self, tmp_path):
        from pipeline_core import PipelineOrchestrator

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        return orch

    def test_generic_agents_are_absent_from_the_legacy_auto_graph(self, tmp_path):
        orch = self._orch(tmp_path)
        try:
            order = orch._legacy_agent_order()
            assert "http_request" not in order and "transform" not in order, order
            # 预览与实跑必须同一套准入，否则 --plan 里看得见、实跑没有
            assert all(step["agent"] not in ("http_request", "transform")
                       for step in orch.plan("test_pipeline", "x.md"))
        finally:
            orch.shutdown()

    def test_domain_agents_still_join_by_default(self, tmp_path):
        """没声明 LEGACY_AUTO 的件默认照旧进图——否则这条修复本身就在改旧语义。"""
        orch = self._orch(tmp_path)
        try:
            order = orch._legacy_agent_order()
            for name in ("writer", "safe_writer", "quality_gate"):
                assert name in order, order
            assert orch.registry.get_meta("writer").legacy_auto is True
        finally:
            orch.shutdown()

    def test_excluded_agents_are_still_registered_and_callable(self, tmp_path):
        """排除只针对 legacy 自动图：注册、元信息与声明式调用一律照旧。"""
        orch = self._orch(tmp_path)
        try:
            for name in ("http_request", "transform"):
                meta = orch.registry.get_meta(name)
                assert meta is not None, f"{name} 不该因为 LEGACY_AUTO 就不注册"
                assert meta.legacy_auto is False
            assert "http_request" in orch.registry.deps_order()
        finally:
            orch.shutdown()
