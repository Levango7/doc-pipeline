"""
Admin API v1 - 轻量级 REST API（零外部依赖）
=============================================
端点：
  GET  /health              → 总线 + Registry 健康状态
  GET  /metrics             → Prometheus 格式指标
  GET  /tasks               → 所有任务列表
  GET  /tasks/<id>          → 单任务详情
  POST /tasks/<id>/cancel   → 取消任务
  POST /tasks/<id>/rerun    → 重跑流水线（复用上一次 plan + 输入）
  GET  /agents              → 已注册 agent 列表
  GET  /dlq                 → 死信队列
  POST /dlq/<id>/replay     → 重放死信

鉴权：
  - 通过环境变量 ADMIN_API_KEY 启用（非空时开启）
  - 客户端需在 Header 携带 `Authorization: Bearer <key>`
  - /health 和静态资源免鉴权
"""
from __future__ import annotations

import json
import logging
import os
import mimetypes
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger(__name__)


class AdminHandler(BaseHTTPRequestHandler):
    """REST API 请求处理"""

    orch: Any = None  # 由 AdminAPI 注入
    dashboard_dir: Optional[str] = None  # 静态文件目录
    api_key: Optional[str] = None  # 由 AdminAPI 注入

    def _json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def _prometheus(self, text: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(text.encode())

    def _text(self, text: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text.encode())

    def _serve_static(self, path: str) -> bool:
        """尝试从 dashboard_dir 提供静态文件"""
        if not self.dashboard_dir:
            return False
        # 只处理 .html/.js/.css 文件
        if not any(path.endswith(ext) for ext in (".html", ".js", ".css", ".png", ".svg", ".ico", ".json")):
            return False
        # 安全处理：防止路径遍历
        clean_path = path.lstrip("/")
        file_path = Path(self.dashboard_dir) / clean_path
        if not file_path.exists() or not file_path.is_file():
            return False
        # 确保文件在 dashboard_dir 内
        try:
            file_path.relative_to(Path(self.dashboard_dir).resolve())
        except ValueError:
            return False
        try:
            content = file_path.read_bytes()
            ctype, _ = mimetypes.guess_type(str(file_path))
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return True
        except Exception:
            return False

    def _check_auth(self) -> bool:
        """校验 API key（未配置则不校验）"""
        if not self.api_key:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
            return token == self.api_key
        # 兼容 ?token=xxx
        if "?" in self.path:
            q = self.path.split("?", 1)[1]
            for kv in q.split("&"):
                if kv.startswith("token="):
                    return kv[6:] == self.api_key
        return False

    def do_GET(self):
        try:
            # 静态资源免鉴权
            if self._serve_static(self.path):
                return
            # 健康端点免鉴权
            if self.path == "/health":
                self._handle_health()
                return
            # 其余端点需要鉴权
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return

            if self.path == "/metrics":
                self._handle_metrics()
            elif self.path == "/tasks":
                self._handle_list_tasks()
            elif self.path.startswith("/tasks/"):
                task_id = self.path.split("/tasks/")[1].split("/")[0]
                if self.path.endswith("/cancel"):
                    self._handle_cancel_task(task_id)
                elif self.path.endswith("/rerun"):
                    self._handle_rerun_task(task_id)
                elif self.path.endswith("/pause"):
                    self._handle_pause_task(task_id)
                elif self.path.endswith("/resume"):
                    self._handle_resume_task(task_id)
                else:
                    self._handle_get_task(task_id)
            elif self.path == "/agents":
                self._handle_list_agents()
            elif self.path == "/dlq":
                self._handle_list_dlq()
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                dlq_id = int(self.path.split("/dlq/")[1].split("/")[0])
                self._handle_replay_dlq(dlq_id)
            elif self.path == "/api/dashboard":
                self._handle_dashboard()
            elif self.path == "/api/pipeline":
                self._handle_pipeline()
            elif self.path == "/":
                self._text("Doc-Pipeline Admin API v1\n"
                           "Endpoints: /health /metrics /tasks /agents /dlq /api/dashboard /api/pipeline")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            # 读取请求体
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""

            # 静态资源/健康免鉴权
            if self._serve_static(self.path):
                return
            if self.path == "/health":
                self._handle_health()
                return
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return

            # 处理 POST 专属路由
            if self.path.startswith("/tasks/"):
                task_id = self.path.split("/tasks/")[1].split("/")[0]
                if self.path.endswith("/cancel"):
                    self._handle_cancel_task(task_id)
                    return
                elif self.path.endswith("/rerun"):
                    self._handle_rerun_task(task_id)
                    return
                elif self.path.endswith("/pause"):
                    self._handle_pause_task(task_id)
                    return
                elif self.path.endswith("/resume"):
                    self._handle_resume_task(task_id)
                    return
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                dlq_id = int(self.path.split("/dlq/")[1].split("/")[0])
                self._handle_replay_dlq(dlq_id)
                return

            self._json({"error": "method not allowed"}, 405)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def _handle_health(self):
        if not self.orch:
            return self._json({"status": "error", "message": "orchestrator not set"})
        h = self.orch.bus.health()
        h["registry_agents"] = len(self.orch.registry.list())
        h["tasks_running"] = len(self.orch.list_tasks())
        self._json(h)

    def _handle_metrics(self):
        if not self.orch:
            return self._text("orchestrator not set\n")
        metrics = getattr(self.orch, "_metrics", None)
        if metrics and hasattr(metrics, "to_prometheus"):
            self._prometheus(metrics.to_prometheus())
        else:
            self._text("# no metrics available\n")

    def _handle_list_tasks(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        tasks = self.orch.list_tasks()
        tasks_data = [
            {
                "id": t.id,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "pipeline": t.pipeline_name,
                "created_at": getattr(t, "created_at", None),
                "finished_at": getattr(t, "finished_at", None),
                "error": getattr(t, "error", None),
            }
            for t in tasks
        ]
        self._json({"tasks": tasks_data, "count": len(tasks_data)})

    def _handle_get_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        task = self.orch.get_task(task_id)
        if not task:
            return self._json({"error": "task not found"}, 404)
        self._json({
            "id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            "input_file": getattr(task, "input_file", ""),
            "config": getattr(task, "config", {}),
            "created_at": getattr(task, "created_at", None),
            "finished_at": getattr(task, "finished_at", None),
            "error": getattr(task, "error", None),
        })

    def _handle_cancel_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        ok = self.orch.cancel(task_id)
        self._json({"cancelled": ok, "task_id": task_id})

    def _handle_rerun_task(self, task_id: str):
        """重跑流水线（复用上一次 plan + 输入）"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            new_task = self.orch.rerun(task_id=task_id)
            self._json({
                "status": "rerun_started",
                "new_task_id": new_task.id,
                "pipeline": new_task.pipeline_name,
            })
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_pause_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        ok = self.orch.pause(task_id)
        self._json({"paused": ok, "task_id": task_id})

    def _handle_resume_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        ok = self.orch.resume(task_id)
        self._json({"resumed": ok, "task_id": task_id})

    def _handle_list_agents(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        agents = self.orch.registry.list()
        self._json({"agents": agents, "count": len(agents)})

    def _handle_list_dlq(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        dlq = self.orch.bus.list_dlq()
        self._json({"dlq": dlq, "count": len(dlq)})

    def _handle_replay_dlq(self, dlq_id: int):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        # 交由 orchestrator 真正重放（重新执行故障 node 并回填）
        result = self.orch.replay_dlq(dlq_id)
        self._json({"replayed": result is not None, "dlq_id": dlq_id,
                     "result": result})

    def _handle_dashboard(self):
        if not self.orch:
            return self._json({"status": "error", "message": "orchestrator not set"})
        orch = self.orch
        tasks = orch.list_tasks()
        tasks_data = [
            {
                "id": t.id,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "pipeline": t.pipeline_name,
                "progress": getattr(t, "progress", 0),
                "steps": len(getattr(t, "steps", [])),
                "created_at": getattr(t, "created_at", None),
                "finished_at": getattr(t, "finished_at", None),
                "error": getattr(t, "error", None),
            }
            for t in tasks
        ]
        agents_list = orch.registry.list()
        agents = []
        for a in agents_list:
            agents.append({
                "name": a.get("name", ""),
                "version": a.get("version", ""),
                "status": a.get("status", "unknown"),
            })
        self._json({
            "status": "ok",
            "tasks": tasks_data,
            "agents": agents,
            "task_count": len(tasks_data),
        })

    def _handle_pipeline(self):
        """返回流水线配置信息"""
        if not self.orch:
            return self._json({"status": "error", "message": "orchestrator not set"})
        orch = self.orch
        try:
            from pipeline_core import __version__ as _v
        except ImportError:
            _v = "unknown"
        pipeline_info = {
            "agents_dir": str(orch.agents_dir),
            "checkpoint_dir": str(orch.checkpoint_dir),
            "registered_agents": orch.registry.list(),
            "active_tasks": len(orch.list_tasks()),
            "version": _v,
        }
        from pathlib import Path
        pipelines_dir = Path(orch.agents_dir).parent / "pipelines"
        if pipelines_dir.exists():
            pipeline_info["pipeline_files"] = [p.name for p in sorted(pipelines_dir.glob("*.yaml"))]
        self._json(pipeline_info)


class AdminAPI:
    """管理 API 服务器"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8910,
                 serve_static: bool = False, dashboard_dir: Optional[str] = None,
                 api_key: Optional[str] = None):
        self.host = host
        self.port = port
        self.serve_static = serve_static
        self.dashboard_dir = dashboard_dir
        # 从环境变量或参数获取 API key
        self.api_key = api_key or os.environ.get("ADMIN_API_KEY", "")
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, orch) -> bool:
        """在后台线程启动 API 服务器"""
        try:
            AdminHandler.orch = orch
            AdminHandler.api_key = self.api_key
            if self.serve_static and self.dashboard_dir:
                AdminHandler.dashboard_dir = self.dashboard_dir
                _logger.info(f"[AdminAPI] 静态文件目录: {self.dashboard_dir}")
            if self.api_key:
                _logger.info("[AdminAPI] 已启用 API Key 鉴权")
            self._server = HTTPServer((self.host, self.port), AdminHandler)
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True,
                name="admin-api",
            )
            self._thread.start()
            _logger.info(f"[AdminAPI] 启动 http://{self.host}:{self.port}")
            return True
        except Exception as e:
            _logger.error(f"[AdminAPI] 启动失败: {e}")
            return False

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            if self._thread:
                self._thread.join(timeout=2)
            self._server = None
            self._thread = None
