"""MCP Server — Model Context Protocol 标准接口

让 AI agent（Claude / GPT / GLM 等）通过 MCP 协议调度 doc-pipeline。

协议: JSON-RPC 2.0 over stdio（每行一个 JSON 消息）
启动: python run.py --mcp
     或 doc-pipeline mcp

暴露的 MCP Tools:
  - generate_document: 提交文档生成任务
  - get_task:          查询任务状态和结果
  - list_tasks:        列出所有任务
  - list_pipelines:    列出可用流水线
  - get_pipeline_info: 获取流水线详情

示例（AI agent 侧）:
  → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{}}}
  ← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"doc-pipeline","version":"3.2.0"}}}

  → {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  ← {"jsonrpc":"2.0","id":2,"result":{"tools":[...]}}

  → {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"generate_document","arguments":{"query":"Python asyncio"}}}
  ← {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"..."}]}}
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

from .fast_json import dumps as _fast_dumps
from .fast_json import loads as _fast_loads

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "doc-pipeline"
SERVER_VERSION = "3.2.0"

TOOLS = [
    {
        "name": "generate_document",
        "description": "提交文档生成任务。输入一个主题，自动完成检索→抓取→写作→质量门控→检查→排版→安全落盘，输出结构化 Markdown 文档。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "文档主题/查询关键词（必填）",
                },
                "title": {
                    "type": "string",
                    "description": "文档标题（可选，默认同 query）",
                },
                "pipeline": {
                    "type": "string",
                    "description": "流水线名（可选，默认 docgen）",
                    "default": "docgen",
                },
                "wait": {
                    "type": "boolean",
                    "description": "是否同步等待完成（可选，默认 false，建议 false 后用 get_task 轮询）",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_task",
        "description": "查询任务状态和结果（含输出文档内容）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "任务 ID",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "list_tasks",
        "description": "列出所有任务（含状态、进度、结果摘要）。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_pipelines",
        "description": "列出可用的流水线定义。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_pipeline_info",
        "description": "获取指定流水线的 Agent 拓扑和配置详情。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "流水线名（默认 docgen）",
                    "default": "docgen",
                },
            },
        },
    },
]


class MCPServer:
    """MCP Server — JSON-RPC 2.0 over stdio"""

    def __init__(self, orch=None):
        self.orch = orch
        self._initialized = False

    def serve(self):
        """主循环：从 stdin 读 JSON-RPC，写响应到 stdout"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = _fast_loads(line)
            except Exception:
                self._send_error(None, -32700, "Parse error")
                continue

            response = self._handle_request(request)
            if response is not None:
                sys.stdout.write(_fast_dumps(response) + "\n")
                sys.stdout.flush()

    def _handle_request(self, request: dict) -> dict | None:
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            if method == "initialize":
                return self._ok(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
            elif method == "initialized":
                self._initialized = True
                return None
            elif method == "tools/list":
                return self._ok(req_id, {"tools": TOOLS})
            elif method == "tools/call":
                return self._handle_tool_call(req_id, params)
            elif method == "ping":
                return self._ok(req_id, {})
            else:
                return self._error(req_id, -32601, f"Method not found: {method}")
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return self._error(req_id, -32603, f"Internal error: {e}")

    def _handle_tool_call(self, req_id: Any, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments", {})

        if name == "generate_document":
            return self._tool_generate_document(req_id, args)
        elif name == "get_task":
            return self._tool_get_task(req_id, args)
        elif name == "list_tasks":
            return self._tool_list_tasks(req_id, args)
        elif name == "list_pipelines":
            return self._tool_list_pipelines(req_id, args)
        elif name == "get_pipeline_info":
            return self._tool_get_pipeline_info(req_id, args)
        else:
            return self._error(req_id, -32602, f"Unknown tool: {name}")

    def _tool_generate_document(self, req_id: Any, args: dict) -> dict:
        query = args.get("query", "").strip()
        if not query:
            return self._error(req_id, -32602, "Missing 'query' argument")

        title = args.get("title", query)
        pipeline_name = args.get("pipeline", "docgen")
        wait = bool(args.get("wait", False))

        if not self.orch:
            return self._error(req_id, -32603, "Orchestrator not initialized")

        task_id = str(uuid.uuid4())[:8]
        input_file = Path(tempfile.gettempdir()) / f"mcp_{task_id}.md"
        input_file.write_text(f"# {title}\n\n## 查询\n\n{query}\n", encoding="utf-8")

        try:
            from .scheduler import Scheduler
            sched = Scheduler()
            plan = sched.parse(pipeline_name)
        except Exception as e:
            return self._error(req_id, -32603, f"Failed to parse pipeline: {e}")

        try:
            task = self.orch.run_plan(plan, input_file=str(input_file),
                                      task_id=task_id, wait=wait)
        except Exception as e:
            return self._error(req_id, -32603, f"Pipeline failed: {e}")

        result = {
            "task_id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            "query": query,
            "title": title,
        }

        if wait:
            result["result_keys"] = list(getattr(task, "result", {}).keys())
            result["error"] = getattr(task, "error", None)
            for key in ("safe_writer", "safewriter", "layout", "checker"):
                val = getattr(task, "result", {}).get(key)
                path = None
                if isinstance(val, dict):
                    path = val.get("output_path") or val.get("path") or val.get("file")
                elif isinstance(val, str) and val.endswith(".md"):
                    path = val
                if path and Path(path).exists():
                    result["output_path"] = path
                    with contextlib.suppress(Exception):
                        result["output_content"] = Path(path).read_text(encoding="utf-8")
                    break

        return self._tool_result(req_id, result)

    def _tool_get_task(self, req_id: Any, args: dict) -> dict:
        task_id = args.get("task_id", "")
        if not self.orch:
            return self._error(req_id, -32603, "Orchestrator not initialized")

        task = self.orch.get_task(task_id)
        if not task:
            return self._error(req_id, -32602, f"Task not found: {task_id}")

        result = {
            "id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            "result": dict(getattr(task, "result", {})),
            "error": getattr(task, "error", None),
        }
        return self._tool_result(req_id, result)

    def _tool_list_tasks(self, req_id: Any, args: dict) -> dict:
        if not self.orch:
            return self._error(req_id, -32603, "Orchestrator not initialized")

        tasks = self.orch.list_tasks()
        result = {
            "tasks": [
                {
                    "id": t.id,
                    "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                    "pipeline": t.pipeline_name,
                }
                for t in tasks
            ],
            "count": len(tasks),
        }
        return self._tool_result(req_id, result)

    def _tool_list_pipelines(self, req_id: Any, args: dict) -> dict:
        pipelines_dir = Path(__file__).parent.parent / "pipelines"
        pipelines = []
        for f in pipelines_dir.glob("*.yaml"):
            if not f.name.startswith("test_"):
                pipelines.append({"name": f.stem, "file": str(f)})
        return self._tool_result(req_id, {"pipelines": pipelines})

    def _tool_get_pipeline_info(self, req_id: Any, args: dict) -> dict:
        name = args.get("name", "docgen")
        try:
            from .scheduler import Scheduler
            sched = Scheduler()
            plan = sched.parse(name)
            result = {
                "name": plan.pipeline_name,
                "node_count": plan.node_count,
                "levels": plan.execution_order,  # type: ignore[attr-defined]
                "agents": [
                    {
                        "name": n.agent_name,
                        "dependencies": n.dependencies,
                        "timeout": n.timeout,
                        "max_retries": n.max_retries,
                    }
                    for n in plan.dag_nodes.values()  # type: ignore[attr-defined]
                ],
            }
            return self._tool_result(req_id, result)
        except Exception as e:
            return self._error(req_id, -32603, f"Failed to parse pipeline: {e}")

    def _ok(self, req_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _error(self, req_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def _tool_result(self, req_id: Any, data: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": _fast_dumps(data, ensure_ascii=False)}],
            },
        }

    def _send_error(self, req_id: Any, code: int, message: str):
        response = self._error(req_id, code, message)
        sys.stdout.write(_fast_dumps(response) + "\n")
        sys.stdout.flush()


def run_mcp_server(orch=None):
    """启动 MCP server（stdio 模式）"""
    if orch is None:
        from . import PipelineOrchestrator
        project_root = Path(__file__).parent.parent
        orch = PipelineOrchestrator(
            agents_dir=str(project_root / "agents"),
            checkpoint_dir=str(project_root / "checkpoints"),
        )
        orch.register_agents()

    server = MCPServer(orch=orch)
    server.serve()
