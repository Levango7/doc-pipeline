"""OpenAPI Spec — 结构有效性测试"""
import pytest
from pipeline_core.openapi_spec import generate_spec


@pytest.fixture
def spec():
    return generate_spec()


class TestOpenAPISpec:
    def test_version(self, spec):
        assert spec["openapi"] == "3.0.3"

    def test_info(self, spec):
        assert spec["info"]["title"] == "Doc-Pipeline Admin API"
        assert "version" in spec["info"]

    def test_has_servers(self, spec):
        assert len(spec["servers"]) >= 1

    def test_security_scheme(self, spec):
        assert "BearerAuth" in spec["components"]["securitySchemes"]
        assert spec["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"

    def test_has_schemas(self, spec):
        schemas = spec["components"]["schemas"]
        assert "TaskSubmit" in schemas
        assert "TaskInfo" in schemas
        assert "CostSummary" in schemas
        assert "Error" in schemas

    def test_task_submit_schema(self, spec):
        schema = spec["components"]["schemas"]["TaskSubmit"]
        assert "query" in schema["properties"]
        assert "query" in schema["required"]

    def test_paths_not_empty(self, spec):
        assert len(spec["paths"]) >= 15

    def test_key_endpoints_exist(self, spec):
        paths = spec["paths"]
        assert "/health" in paths
        assert "/tasks" in paths
        assert "/api/tasks" in paths
        assert "/api/cost" in paths
        assert "/api/cost/budget" in paths
        assert "/api/alerts" in paths
        assert "/api/logs" in paths
        assert "/api/openapi.json" not in paths  # 自身不需要
        assert "/stream" in paths

    def test_post_endpoints_have_request_body(self, spec):
        for path, methods in spec["paths"].items():
            for method, detail in methods.items():
                if method == "post":
                    if path in ("/api/tasks", "/api/config", "/api/cost/budget", "/api/events/hooks"):
                        assert "requestBody" in detail, f"POST {path} 应有 requestBody"

    def test_task_id_path_param(self, spec):
        path = spec["paths"]["/tasks/{task_id}"]["get"]
        params = path["parameters"]
        assert any(p["name"] == "task_id" and p["in"] == "path" for p in params)

    def test_stream_has_query_params(self, spec):
        path = spec["paths"]["/stream"]["get"]
        params = path["parameters"]
        assert any(p["name"] == "query" and p["in"] == "query" for p in params)

    def test_responses_exist(self, spec):
        for path, methods in spec["paths"].items():
            for method, detail in methods.items():
                assert "responses" in detail, f"{method.upper()} {path} 缺少 responses"
                assert "200" in detail["responses"] or "200" in str(detail["responses"].keys())