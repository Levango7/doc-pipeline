"""
Admin API v1 - 轻量级 REST API（零外部依赖）
=============================================
端点：
  GET  /health              → 总线 + Registry 健康状态
  GET  /api/health/deep     → 全组件深度健康检查（LLM/搜索/缓存/检查点）
  GET  /metrics             → Prometheus 格式指标
  GET  /tasks               → 所有任务列表
  GET  /tasks/<id>          → 单任务详情
  POST /tasks/<id>/cancel   → 取消任务
  POST /tasks/<id>/rerun    → 重跑流水线（复用上一次 plan + 输入）
  GET  /agents              → 已注册 agent 列表
  GET  /api/agents/<name>   → 单个 agent 详情（含 stats/meta）
  GET  /api/config          → 运行时配置快照
  POST /api/config          → 运行时配置更新（body: {"key": "llm.model", "value": "xxx"}）
  GET  /api/cache           → 缓存统计
  POST /api/cache/clear     → 清空所有缓存
  GET  /api/events/hooks    → 列出已注册事件钩子
  POST /api/events/hooks    → 注册事件钩子（body: {"event": "task.completed", "url": "https://..."}）
  DELETE /api/events/hooks/<id> → 注销事件钩子
  GET  /dlq                 → 死信队列
  POST /dlq/<id>/replay     → 重放死信
  GET  /stream?query=...    → SSE 流式推送文档生成进度

鉴权：
  - 通过环境变量 ADMIN_API_KEY 启用（非空时开启）
  - 客户端需在 Header 携带 `Authorization: Bearer <key>`
  - /health 和静态资源免鉴权
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import time
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
        """校验 API key（未配置则不校验）。使用恒定时间比较防止时序攻击。"""
        if not self.api_key:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
            return hmac.compare_digest(token, self.api_key)
        # 兼容 ?token=xxx
        if "?" in self.path:
            q = self.path.split("?", 1)[1]
            for kv in q.split("&"):
                if kv.startswith("token="):
                    return hmac.compare_digest(kv[6:], self.api_key)
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
            elif self.path.startswith("/api/agents/"):
                agent_name = self.path.split("/api/agents/")[1].split("/")[0]
                self._handle_agent_detail(agent_name)
            elif self.path == "/api/config":
                self._handle_config_get()
            elif self.path == "/api/health/deep":
                self._handle_health_deep()
            elif self.path == "/api/cache":
                self._handle_cache_stats()
            elif self.path == "/api/events/hooks":
                self._handle_list_hooks()
            elif self.path == "/dlq":
                self._handle_list_dlq()
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                dlq_id = int(self.path.split("/dlq/")[1].split("/")[0])
                self._handle_replay_dlq(dlq_id)
            elif self.path == "/api/dashboard":
                self._handle_dashboard()
            elif self.path == "/api/pipeline":
                self._handle_pipeline()
            elif self.path.startswith("/stream"):
                self._handle_stream()
            elif self.path == "/":
                self._text("Doc-Pipeline Admin API v1\n"
                           "Endpoints: /health /metrics /tasks /agents /dlq /api/dashboard /api/pipeline /stream")
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
            elif self.path == "/api/config":
                self._handle_config_set(body)
                return
            elif self.path == "/api/cache/clear":
                self._handle_cache_clear()
                return
            elif self.path == "/api/events/hooks":
                self._handle_register_hook(body)
                return

            self._json({"error": "method not allowed"}, 405)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_DELETE(self):
        try:
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return
            if self.path.startswith("/api/events/hooks/"):
                hook_id = self.path.split("/api/events/hooks/")[1]
                self._handle_unregister_hook(hook_id)
                return
            self._json({"error": "method not allowed"}, 405)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

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

    def _handle_agent_detail(self, agent_name: str):
        """单个 agent 详情（含 stats/meta）"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        agent = self.orch.registry._agents.get(agent_name)
        if not agent:
            return self._json({"error": f"agent '{agent_name}' not found"}, 404)
        detail = {
            "name": agent_name,
            "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
            "meta": {},
        }
        meta = getattr(agent, "meta", None)
        if meta:
            for attr in ("version", "description", "priority", "input_topics",
                         "output_topics", "dependencies", "cache_ttl", "respawn",
                         "health_check_interval", "tags"):
                detail["meta"][attr] = getattr(meta, attr, None)
        stats = getattr(agent, "stats", None)
        if stats:
            detail["stats"] = {
                "start_count": getattr(stats, "start_count", 0),
                "error_count": getattr(stats, "error_count", 0),
                "respawn_count": getattr(stats, "respawn_count", 0),
                "total_runtime_ms": getattr(stats, "total_runtime_ms", 0),
                "avg_processing_time_ms": getattr(stats, "avg_processing_time_ms", 0),
            }
        # 熔断器状态
        cb = self.orch._cb_registry.get(agent_name) if hasattr(self.orch, "_cb_registry") else None
        if cb:
            detail["circuit_breaker"] = {
                "state": cb.state.name if hasattr(cb.state, "name") else str(cb.state),
                "failure_count": cb.failure_count,
            }
        self._json(detail)

    def _handle_config_get(self):
        """运行时配置快照"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        self._json(self.orch.config.to_dict())

    def _handle_config_set(self, body: bytes):
        """运行时配置更新。body: {"key": "llm.model", "value": "xxx"}"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON body"}, 400)
        key = data.get("key")
        value = data.get("value")
        if not key:
            return self._json({"error": "missing 'key' field"}, 400)
        old_value = self.orch.config.get(key)
        self.orch.config.set(key, value)
        _logger.info(f"[AdminAPI] 配置更新: {key} = {value!r} (旧值: {old_value!r})")
        self._json({"key": key, "old_value": old_value, "new_value": value, "applied": True})

    def _handle_health_deep(self):
        """全组件深度健康检查"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        orch = self.orch
        result = {"timestamp": time.time(), "components": {}}

        # 1. 消息总线
        bus_h = orch.bus.health()
        result["components"]["message_bus"] = {
            "status": "healthy" if bus_h.get("running") else "unhealthy",
            "details": bus_h,
        }

        # 2. Registry
        agents = orch.registry.list()
        result["components"]["registry"] = {
            "status": "healthy" if len(agents) > 0 else "degraded",
            "agent_count": len(agents),
        }

        # 3. LLM Router
        try:
            from pipeline_core.llm_router import get_router
            router = get_router()
            providers = router.list_providers() if hasattr(router, "list_providers") else []
            active = router.get_active() if hasattr(router, "get_active") else None
            result["components"]["llm_router"] = {
                "status": "healthy" if active else "degraded",
                "providers": providers,
                "active": active,
            }
        except Exception as e:
            result["components"]["llm_router"] = {"status": "unknown", "error": str(e)}

        # 4. 搜索引擎
        try:
            from pipeline_core.search_engines import SearchEngineManager
            mgr = SearchEngineManager.from_env()
            engines = mgr.list_engines() if hasattr(mgr, "list_engines") else []
            result["components"]["search_engines"] = {
                "status": "healthy" if engines else "degraded",
                "engines": engines,
            }
        except Exception as e:
            result["components"]["search_engines"] = {"status": "unknown", "error": str(e)}

        # 5. 缓存
        try:
            from pipeline_core.cache_manager import all_stats
            stats = all_stats()
            result["components"]["cache"] = {
                "status": "healthy",
                "stats": stats,
            }
        except Exception as e:
            result["components"]["cache"] = {"status": "unknown", "error": str(e)}

        # 6. 检查点目录
        ckpt_dir = orch.checkpoint_dir
        result["components"]["checkpoint"] = {
            "status": "healthy" if ckpt_dir.exists() else "degraded",
            "dir": str(ckpt_dir),
        }

        # 7. 熔断器
        cb_list = orch._cb_registry.list() if hasattr(orch, "_cb_registry") else []
        open_cbs = [c for c in cb_list if getattr(c, "state", None) and c.state.name == "OPEN"]
        result["components"]["circuit_breakers"] = {
            "status": "healthy" if not open_cbs else "degraded",
            "total": len(cb_list),
            "open": len(open_cbs),
        }

        # 总体状态
        unhealthy = [k for k, v in result["components"].items()
                     if isinstance(v, dict) and v.get("status") not in ("healthy",)]
        result["overall"] = "healthy" if not unhealthy else "degraded"
        result["unhealthy_components"] = unhealthy
        self._json(result)

    def _handle_cache_stats(self):
        """缓存统计"""
        try:
            from pipeline_core.cache_manager import all_stats
            self._json({"caches": all_stats()})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_cache_clear(self):
        """清空所有缓存"""
        try:
            from pipeline_core.cache_manager import clear_all_caches
            clear_all_caches()
            _logger.info("[AdminAPI] 所有缓存已清空")
            self._json({"cleared": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_list_hooks(self):
        """列出已注册事件钩子"""
        from pipeline_core.event_hook import get_hook_manager
        mgr = get_hook_manager()
        self._json({"hooks": mgr.list_hooks()})

    def _handle_register_hook(self, body: bytes):
        """注册事件钩子。body: {"event": "task.completed", "url": "https://..."}"""
        from pipeline_core.event_hook import get_hook_manager
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON body"}, 400)
        event = data.get("event")
        url = data.get("url")
        if not event or not url:
            return self._json({"error": "missing 'event' or 'url' field"}, 400)
        mgr = get_hook_manager()
        hook_id = mgr.register(event, url, data.get("headers", {}))
        _logger.info(f"[AdminAPI] 事件钩子已注册: {event} -> {url} (id={hook_id})")
        self._json({"hook_id": hook_id, "event": event, "url": url, "registered": True})

    def _handle_unregister_hook(self, hook_id: str):
        """注销事件钩子"""
        from pipeline_core.event_hook import get_hook_manager
        mgr = get_hook_manager()
        ok = mgr.unregister(hook_id)
        self._json({"unregistered": ok, "hook_id": hook_id})

    def _handle_stream(self):
        """SSE 流式端点 —— 实时推送文档生成进度。

        用法: GET /stream?task_id=<id>&query=<topic>&title=<title>
        重连: 客户端断线后携带 Last-Event-ID header 重连，从断点继续推送。
        指标: GET /stream/metrics 获取流式指标快照。
        返回: text/event-stream 格式，每个事件含 id/type/data/section/total 字段。
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # 指标查询端点
        if parsed.path == "/stream/metrics":
            from pipeline_core.streaming import StreamMetrics
            metrics = StreamMetrics()
            self._json(metrics.snapshot())
            return

        task_id = params.get("task_id", ["stream"])[0]
        query = params.get("query", [""])[0]
        title = params.get("title", ["自动生成文档"])[0]

        # SSE 重连: 检查 Last-Event-ID header
        last_event_id = 0
        last_event_id_header = self.headers.get("Last-Event-ID")
        if last_event_id_header:
            try:
                last_event_id = int(last_event_id_header)
            except ValueError:
                pass

        if not query:
            self._json({"error": "missing 'query' parameter"}, 400)
            return

        # SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def _send_sse(event_type: str, data: dict, event_id: int = 0):
            payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
            id_line = f"id: {event_id}\n" if event_id else ""
            try:
                self.wfile.write(f"{id_line}data: {payload}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        from pipeline_core.streaming import (
            StreamCallback, register_callback, get_callback, unregister_callback,
        )

        # SSE 重连: 查找已注册的 callback（同一 task_id 的活跃流）
        existing_callback = get_callback(task_id) if last_event_id > 0 else None

        if existing_callback:
            # 重连模式：replay 历史事件后继续监听
            _send_sse("connected", {"task_id": task_id, "query": query,
                                    "resumed_from": last_event_id, "reconnect": True})
            missed_events = existing_callback.get_events_since(last_event_id)
            for event in missed_events:
                _send_sse(event.event_type, event.to_dict().get("data", {}),
                         event.event_id)
            _send_sse("resumed", {"replayed": len(missed_events)})

            # 继续监听新事件
            for event in existing_callback:
                _send_sse(event.event_type, event.to_dict().get("data", {}),
                         event.event_id)
                if event.event_type in ("complete", "error"):
                    break
            return

        # 首次连接
        _send_sse("connected", {"task_id": task_id, "query": query,
                                "resumed_from": 0, "reconnect": False})

        if not self.orch:
            _send_sse("error", {"error": "orchestrator not set"})
            return

        try:
            # 获取 writer agent 实例
            writer_agent = None
            for a in self.orch.registry._agents.values():
                if hasattr(a, "handle_streaming"):
                    writer_agent = a
                    break

            if not writer_agent:
                _send_sse("error", {"error": "writer agent not found"})
                return

            callback = StreamCallback()
            register_callback(task_id, callback)

            # 加载 prompt 模板获取章节数，用于 on_start 通知
            try:
                template = writer_agent._load_prompt_template(
                    writer_agent._prompt_profile)
                sections = template.get("sections", [])
            except Exception:
                sections = []
            callback.on_start(len(sections), title)

            # 创建临时输入文件（供全流水线 fetcher→researcher→writer 使用）
            import tempfile
            input_file = Path(tempfile.gettempdir()) / f"stream_{task_id}.md"
            input_file.write_text(
                f"# {title}\n\n## 查询\n\n{query}\n", encoding="utf-8")

            # 在后台线程用 run_plan_async 执行全流水线（端到端真异步）
            # 替代旧的 writer_agent.handle_streaming(msg, callback) 方式，
            # 使 fetcher/researcher/writer 三阶段均在 async 路径下执行
            def _run():
                import asyncio as _aio
                from pipeline_core.scheduler import Scheduler
                try:
                    sched = Scheduler()
                    plan = sched.parse("docgen")
                    # 设置回调，writer 的 _restructure_document 会自动拾取
                    writer_agent._active_stream_callback = callback
                    task = _aio.run(self.orch.run_plan_async(
                        plan, input_file=str(input_file), task_id=task_id
                    ))
                    # 流水线完成后发送 complete 事件
                    content = ""
                    if task and task.result:
                        writer_result = task.result.get("writer", {})
                        if isinstance(writer_result, dict):
                            content = writer_result.get("content", "")
                    callback.on_complete(content, {
                        "status": task.status.value if task else "unknown",
                        "task_id": task_id,
                    })
                except Exception as e:
                    callback.on_error(str(e))
                finally:
                    writer_agent._active_stream_callback = None
                    try:
                        input_file.unlink()
                    except OSError:
                        pass

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()

            # 流式推送事件（带 event_id）
            for event in callback:
                _send_sse(event.event_type, event.to_dict().get("data", {}),
                         event.event_id)
                if event.event_type in ("complete", "error"):
                    break

            worker.join(timeout=5)
            unregister_callback(task_id)

        except Exception as e:
            _send_sse("error", {"error": str(e)})
            unregister_callback(task_id)


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
