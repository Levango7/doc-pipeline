"""MCPServer — JSON-RPC 协议测试"""
import json
from unittest.mock import MagicMock

import pytest

from pipeline_core.mcp_server import PROTOCOL_VERSION, SERVER_NAME, MCPServer


@pytest.fixture
def server():
    s = MCPServer(orch=MagicMock())
    return s


class TestMCPServer:
    def test_initialize(self, server):
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert resp["result"]["serverInfo"]["name"] == SERVER_NAME

    def test_tools_list(self, server):
        resp = server._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        assert len(tools) == 5
        names = [t["name"] for t in tools]
        assert "generate_document" in names
        assert "get_task" in names
        assert "list_tasks" in names
        assert "list_pipelines" in names
        assert "get_pipeline_info" in names

    def test_tool_has_input_schema(self, server):
        resp = server._handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        for tool in resp["result"]["tools"]:
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_generate_document_missing_query(self, server):
        """业务失败（缺 query）→ MCP 规范的 result.content + isError:true，非 JSON-RPC error"""
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "generate_document", "arguments": {}},
        })
        assert "error" not in resp
        assert resp["result"]["isError"] is True
        assert "query" in resp["result"]["content"][0]["text"]

    def test_unknown_method(self, server):
        resp = server._handle_request({"jsonrpc": "2.0", "id": 5, "method": "unknown"})
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_unknown_tool(self, server):
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "nonexistent", "arguments": {}},
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32602

    def test_ping(self, server):
        resp = server._handle_request({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        assert resp["result"] == {}

    def test_initialized_notification(self, server):
        resp = server._handle_request({"jsonrpc": "2.0", "method": "initialized"})
        assert resp is None

    def test_list_pipelines(self, server):
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "list_pipelines", "arguments": {}},
        })
        assert "result" in resp
        assert "content" in resp["result"]
        data = json.loads(resp["result"]["content"][0]["text"])
        assert "pipelines" in data

    def test_tool_result_format(self, server):
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "list_pipelines", "arguments": {}},
        })
        content = resp["result"]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert isinstance(content[0]["text"], str)
