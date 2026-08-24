"""第九轮修复验证：鉴权本机信任模式 / 版本端点路由 / 404 语义 / OpenAPI 安全声明 / MCP get_pipeline_info

采用真实 ThreadingHTTPServer（随机端口）做 HTTP 层验证，而非 mock handler，
确保路由接线与鉴权矩阵端到端生效。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.admin_api import AdminAPI, AdminHandler


@pytest.fixture
def orch_mock():
    o = MagicMock()
    o.list_tasks.return_value = []
    o.registry.list.return_value = []
    o.bus.health.return_value = {"status": "ok", "subscribers": 0}
    o.get_task.return_value = None
    return o


def _start(api_key, host="127.0.0.1", orch=None):
    """起一个真实 API server（随机端口），返回 (base_url, AdminAPI)"""
    api = AdminAPI(host=host, port=0, api_key=api_key)
    assert api.start(orch if orch is not None else MagicMock()) is True
    port = api._server.server_address[1]
    return f"http://127.0.0.1:{port}", api


def _get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"raw": body}


@pytest.fixture(autouse=True)
def reset_handler_state():
    """类属性跨测试共享，逐一复位避免串扰"""
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None


class TestAuthLocalTrustMode:

    def test_loopback_without_key_allows_access(self, orch_mock):
        """未配置 ADMIN_API_KEY + 回环绑定 → 放行（docs/api.md 文档化行为）"""
        base, api = _start("", orch=orch_mock)
        try:
            status, data = _get(base + "/tasks")
            assert status == 200
        finally:
            api.stop()

    def test_with_key_without_credentials_returns_401(self, orch_mock):
        base, api = _start("secret-key", orch=orch_mock)
        try:
            status, data = _get(base + "/tasks")
            assert status == 401
            assert data == {"error": "unauthorized"}
        finally:
            api.stop()

    def test_with_key_wrong_token_returns_401(self, orch_mock):
        base, api = _start("secret-key", orch=orch_mock)
        try:
            status, _ = _get(base + "/tasks", token="wrong")
            assert status == 401
        finally:
            api.stop()

    def test_with_key_correct_bearer_passes(self, orch_mock):
        base, api = _start("secret-key", orch=orch_mock)
        try:
            status, data = _get(base + "/tasks", token="secret-key")
            assert status == 200
            assert "tasks" in data
        finally:
            api.stop()

    def test_health_stays_exempt_with_key(self, orch_mock):
        """/health 免鉴权不受 key 配置影响"""
        base, api = _start("secret-key", orch=orch_mock)
        try:
            status, _ = _get(base + "/health")
            assert status == 200
        finally:
            api.stop()

    def test_non_loopback_without_key_refuses_to_start(self):
        """安全门：非回环绑定 + 未设 key → 拒绝启动"""
        api = AdminAPI(host="0.0.0.0", port=0, api_key="")
        assert api.start(MagicMock()) is False
        assert api._server is None


class TestVersionEndpointsWired:

    def test_versions_stats_routed(self, orch_mock):
        """/api/versions/stats 此前有 handler 无路由（404），现已接线"""
        base, api = _start("", orch=orch_mock)
        try:
            status, data = _get(base + "/api/versions/stats")
            assert status == 200
            assert "status" in data or "total" in data or data != {}
        finally:
            api.stop()

    def test_versions_list_missing_file_param_400(self, orch_mock):
        base, api = _start("", orch=orch_mock)
        try:
            status, data = _get(base + "/api/versions")
            assert status == 400
            assert "error" in data
        finally:
            api.stop()

    def test_versions_diff_bad_params_400(self, orch_mock):
        base, api = _start("", orch=orch_mock)
        try:
            status, data = _get(base + "/api/versions/diff?file=x.md&v1=a&v2=b")
            assert status == 400
            assert "error" in data
        finally:
            api.stop()


class TaskNotFoundSemantics:

    def test_cancel_missing_task_404(self, orch_mock):
        """任务不存在 → 404（此前返回 200 + cancelled:false 无法区分）"""
        base, api = _start("", orch=orch_mock)
        try:
            req = urllib.request.Request(base + "/tasks/no-such-task/cancel", data=b"")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status, data = resp.status, json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                status, data = e.code, json.loads(e.read().decode())
            assert status == 404
            assert data == {"error": "task not found"}
        finally:
            api.stop()


class TestOpenAPISecurityContract:

    def test_global_security_declared(self):
        from pipeline_core.openapi_spec import generate_spec
        spec = generate_spec()
        assert spec.get("security") == [{"BearerAuth": []}]

    def test_stream_no_longer_marked_public(self):
        """/stream 需鉴权，spec 不得再标 security: []（此前与实现相反）"""
        from pipeline_core.openapi_spec import generate_spec
        spec = generate_spec()
        assert "security" not in spec["paths"]["/stream"]["get"]

    def test_health_marked_public(self):
        from pipeline_core.openapi_spec import generate_spec
        spec = generate_spec()
        assert spec["paths"]["/health"]["get"].get("security") == []

    def test_version_matches_package(self):
        import pipeline_core
        from pipeline_core.openapi_spec import generate_spec
        assert generate_spec()["info"]["version"] == pipeline_core.__version__

    def test_new_endpoints_documented(self):
        from pipeline_core.openapi_spec import generate_spec
        paths = generate_spec()["paths"]
        for p in ("/api/dashboard", "/api/pipeline", "/stream/metrics",
                  "/api/versions", "/api/versions/diff",
                  "/api/versions/stats", "/api/versions/rollback"):
            assert p in paths, f"{p} 缺少 OpenAPI 定义"


class TestMCPGetPipelineInfo:

    def test_get_pipeline_info_succeeds(self):
        """此前读 plan.execution_order/dag_nodes 必抛 AttributeError → -32603；
        改为读 plan.levels 后必须成功返回"""
        from pipeline_core.mcp_server import MCPServer
        server = MCPServer(orch=None)
        resp = server._tool_get_pipeline_info(req_id=1, args={"name": "docgen"})
        assert "error" not in resp, resp
        result = resp["result"]
        # _tool_result 把业务数据包在 content[].text 里
        text = result["content"][0]["text"]
        payload = json.loads(text)
        assert payload["name"] == "docgen"
        assert payload["node_count"] >= 7
        assert len(payload["agents"]) == payload["node_count"]
