"""MCPServer — JSON-RPC 协议测试"""
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_initialize_echoes_client_protocol_version(self, server):
        """MCP 规范：result.protocolVersion 回显客户端请求的版本"""
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 10, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        })
        assert resp["result"]["protocolVersion"] == "2025-06-18"

    def test_generate_document_uses_new_task_id(self):
        """task_id 统一由 pipeline_core.ids.new_task_id 生成（16 位 hex）"""
        from types import SimpleNamespace

        from pipeline_core.ids import new_task_id
        task = SimpleNamespace(id="tid", status=SimpleNamespace(value="running"),
                               pipeline_name="docgen", result={}, error=None)
        orch = MagicMock()
        orch.resolve_pipeline_name.return_value = ("docgen", "")
        orch.run_plan.return_value = task
        s = MCPServer(orch=orch)
        with patch("pipeline_core.mcp_server.new_task_id",
                   side_effect=new_task_id) as mock_gen:
            s._tool_generate_document(11, {"query": "t", "pipeline": "docgen"})
            mock_gen.assert_called_once()
        input_arg = orch.run_plan.call_args.kwargs["input_file"]
        stem = Path(input_arg).stem
        tid = stem.removeprefix("mcp_")
        assert re.fullmatch(r"[0-9a-f]{16}", tid)

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


class TestMCPOverRealStdio:
    """真实 stdio 往返：上面 12 条用例都在进程内调 _handle_request，
    于是"banner 打到 stdout 污染 JSON-RPC 通道"和"serverInfo.version 恒为
    unknown"这两件事没有任何测试能发现（2026-10-06 实测复现）。
    """

    REPO = Path(__file__).parent.parent

    def _roundtrip(self, tmp_path):
        import subprocess
        import sys
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        payload = "\n".join(json.dumps(r) for r in requests) + "\n"
        env = dict(__import__("os").environ,
                   # 状态目录隔离：幂等库按检出绝对路径共享，跑在真 bus_data/ 上会串台
                   DOC_PIPELINE_STATE_DIR=str(tmp_path))
        proc = subprocess.run([sys.executable, "run.py", "--mcp"], input=payload,
                              capture_output=True, text=True, timeout=120,
                              cwd=str(self.REPO), env=env)
        assert proc.returncode == 0, proc.stderr[-2000:]
        frames = [line for line in proc.stdout.splitlines() if line.strip()]
        return frames

    def test_stdout_carries_only_jsonrpc_frames(self, tmp_path):
        frames = self._roundtrip(tmp_path)
        assert len(frames) == 2, f"每个请求应回一帧，实得: {frames}"
        for line in frames:
            parsed = json.loads(line)          # 非 JSON 行（banner 等）在这里炸
            assert parsed.get("jsonrpc") == "2.0", parsed
        assert "Doc-Pipeline" not in "\n".join(frames), "banner 又回到 stdout 了"

    def test_server_info_reports_real_version(self, tmp_path):
        from pipeline_core import __version__
        frames = self._roundtrip(tmp_path)
        info = json.loads(frames[0])["result"]["serverInfo"]
        assert info["name"] == SERVER_NAME
        assert info["version"] == __version__, \
            f"版本回显必须是真实值，历史上当成 'unknown' 出厂过: {info}"
        assert json.loads(frames[1])["result"]["tools"], "tools/list 必须给出工具表"
