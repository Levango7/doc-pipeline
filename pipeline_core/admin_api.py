"""
Admin API v1 - 轻量级 REST API（零外部依赖）
=============================================
端点：
  GET  /health              → 总线 + Registry 健康状态
  GET  /api/health/deep     → 全组件深度健康检查（LLM/搜索/缓存/检查点）
  GET  /metrics             → Prometheus 格式指标
  GET  /tasks               → 所有任务列表
  GET  /tasks/<id>          → 单任务详情（含 result/output_content）
  POST /api/tasks           → 提交新文档生成任务（body: query/title/pipeline/wait/output）
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

危险操作二次确认：
  - POST /api/config、/api/cache/clear、/api/versions/rollback、/dlq/<id>/replay
    （GET 同）除鉴权外必须携带请求头 `X-Confirm: yes`，缺失返回
    428 Precondition Required；执行时输出结构化审计日志
    （时间/key 身份/操作/参数摘要）
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import urlparse

from .fast_json import dumps as _fast_dumps
from .fast_json import loads as _fast_loads
from .ids import new_task_id

_logger = logging.getLogger(__name__)


# ─── 安全防护辅助 ─────────────────────────────
# 安全修复 (P0): 集中定义 SSRF 防护与路径白名单校验，供各端点复用

# task_id 仅允许字母、数字、下划线、连字符（防止路径遍历 ../../）
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# 访问日志脱敏：?token= 查询串凭证打码为 ***（SSE 场景才建议 query token）
_QS_TOKEN_RE = re.compile(r"(token=)[^&\s]+")


def mask_token_query(text: str) -> str:
    return _QS_TOKEN_RE.sub(r"\1***", text)


def _is_private_ip(host: str) -> bool:
    """判断 host 是否为私有/保留 IP 地址（SSRF 防护）。

    覆盖：私有段（10/8、172.16/12、192.168/16）、环回（127/8、::1）、
    链路本地（169.254/16、fe80::/10）、元数据端点（169.254.169.254）、
    未分配/组播/保留段、IPv6 私有（fc00::/7）。
    """
    try:
        # 解析为 IP 地址对象（自动区分 v4/v6）
        addr = ipaddress.ip_address(host)
    except ValueError:
        # 非 IP 字面量（可能是域名），交由调用方决定是否放行
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _validate_webhook_url(url: str) -> tuple[bool, str]:
    """校验 webhook URL 安全性（SSRF 防护）。

    Returns:
        (ok, reason): ok=True 表示通过；ok=False 时 reason 为拒绝原因。
    """
    if not url or not isinstance(url, str):
        return False, "url 为空"
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"url 解析失败: {e}"
    # 仅允许 http/https 协议（拒绝 file://、gopher://、ftp:// 等）
    if parsed.scheme not in ("http", "https"):
        return False, f"协议 {parsed.scheme!r} 不允许，仅支持 http/https"
    host = parsed.hostname or ""
    if not host:
        return False, "url 缺少 host"
    # 拒绝裸 IP 形式的私有/保留地址（域名解析后的 SSRF 由出网层兜底，此处先拦裸 IP）
    if _is_private_ip(host):
        return False, f"host {host!r} 为私有/保留 IP，已拒绝（SSRF 防护）"
    return True, ""


def _validate_output_path(path_str: str, base_dir: str | None = None) -> tuple[bool, str]:
    """校验输出路径必须落在 base_dir（默认 cwd）范围内（防止任意文件写入）。

    Returns:
        (ok, reason): ok=True 表示通过；ok=False 时 reason 为拒绝原因。
    """
    if not path_str:
        return True, ""  # 空路径不校验（交由默认逻辑）
    base = Path(base_dir or os.getcwd()).resolve()
    try:
        target = Path(path_str).resolve()
    except (OSError, ValueError) as e:
        return False, f"路径解析失败: {e}"
    try:
        target.relative_to(base)
    except ValueError:
        return False, f"路径 {path_str!r} 不在允许目录 {base!s} 内"
    return True, ""


def _validate_task_id(task_id: str) -> bool:
    """校验 task_id 仅含字母/数字/下划线/连字符（防止路径遍历）。"""
    if not task_id or len(task_id) > 128:
        return False
    return bool(_TASK_ID_RE.match(task_id))


# ─── 流式指标聚合 ─────────────────────────────
# streaming.py 仅提供单回调粒度的 StreamMetrics，未提供全局聚合接口；
# 此处在 admin_api 侧维护 task_id -> callback 注册表并汇总快照。

class _StreamMetricsHub:
    """聚合所有活跃 StreamCallback 的流式指标（线程安全）"""

    _COUNTER_KEYS = ("events_emitted", "events_dropped", "chunks_emitted", "sections_emitted")

    def __init__(self):
        self._lock = threading.Lock()
        self._callbacks: dict[str, Any] = {}
        self._started_at = time.time()

    def track(self, task_id: str, callback: Any) -> None:
        with self._lock:
            self._callbacks[task_id] = callback

    def untrack(self, task_id: str) -> None:
        with self._lock:
            self._callbacks.pop(task_id, None)

    @staticmethod
    def _cb_snapshot(cb: Any) -> dict:
        try:
            return dict(cb.metrics.snapshot())
        except Exception:
            return {}

    def snapshot(self) -> dict:
        with self._lock:
            callbacks = list(self._callbacks.values())
        totals = {k: 0 for k in self._COUNTER_KEYS}
        latency_weighted = 0.0
        for cb in callbacks:
            snap = self._cb_snapshot(cb)
            if not snap:
                continue
            for k in self._COUNTER_KEYS:
                totals[k] += int(snap.get(k, 0) or 0)
            emitted = int(snap.get("events_emitted", 0) or 0)
            if emitted > 0:
                latency_weighted += float(snap.get("avg_latency", 0.0) or 0.0) * emitted
        elapsed = time.time() - self._started_at
        total_events = totals["events_emitted"]
        return {
            **totals,
            "active_streams": len(callbacks),
            "elapsed": elapsed,
            "events_per_sec": (total_events / elapsed) if elapsed > 0 else 0.0,
            "avg_latency": (latency_weighted / total_events) if total_events > 0 else 0.0,
        }


_STREAM_METRICS_HUB = _StreamMetricsHub()

_EXTRA_CORS_ORIGINS = tuple(
    o.strip() for o in os.environ.get("ADMIN_CORS_ORIGINS", "").split(",") if o.strip()
)


def track_stream_callback(task_id: str, callback: Any) -> None:
    """登记活跃流回调（供 /stream/metrics 聚合）"""
    _STREAM_METRICS_HUB.track(task_id, callback)


def untrack_stream_callback(task_id: str) -> None:
    _STREAM_METRICS_HUB.untrack(task_id)


def stream_metrics_snapshot() -> dict:
    """全部活跃流的指标快照"""
    return _STREAM_METRICS_HUB.snapshot()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器 — 替代单线程 HTTPServer，避免慢请求阻塞所有连接"""
    daemon_threads = True
    allow_reuse_address = True


class AdminHandler(BaseHTTPRequestHandler):
    """REST API 请求处理"""

    orch: Any = None  # 由 AdminAPI 注入
    dashboard_dir: str | None = None  # 静态文件目录
    api_key: str | None = None  # 由 AdminAPI 注入
    server_host: str = "127.0.0.1"  # 由 AdminAPI 注入（本机信任模式判定）

    def _parse_query(self) -> dict:
        """解析 URL 查询串为 dict（不含 URL 解码以外的处理）"""
        if "?" not in self.path:
            return {}
        qs = self.path.split("?", 1)[1]
        return dict(
            kv.split("=", 1) for kv in qs.split("&") if "=" in kv
        )

    def _allowed_origin(self, origin: str | None) -> str | None:
        """CORS 白名单判定。

        仅放行：
          - ADMIN_CORS_ORIGINS 环境变量显式配置的 Origin
          - 与 Host 头完全一致的同源请求
          - 回环地址（127.0.0.1/localhost/::1）且端口与本服务一致
        其余一律拒绝（修复：原先无条件回 ACAO:* 导致任意网页可跨站调用本机 API）。
        """
        if not origin:
            return None
        origin = origin.strip()
        if origin in _EXTRA_CORS_ORIGINS:
            return origin
        try:
            parsed = urlparse(origin)
        except Exception:
            return None
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        host_header = (self.headers.get("Host") or "").strip()
        if host_header and origin == f"{parsed.scheme}://{host_header}":
            return origin
        try:
            server_port = self.server.server_address[1]  # type: ignore[index]
        except Exception:
            server_port = None
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        hostname = parsed.hostname.lower()
        if hostname in ("127.0.0.1", "localhost", "::1") \
                and server_port is not None and origin_port == server_port:
            return origin
        return None

    def _apply_cors_headers(self):
        """按白名单回显 Origin；非白名单请求不带 ACAO 头（浏览器侧拦截响应）"""
        allowed = self._allowed_origin(self.headers.get("Origin"))
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")

    def _json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._apply_cors_headers()
        self.end_headers()
        self.wfile.write(_fast_dumps(data, default=str).encode())

    def _prometheus(self, text: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(text.encode())

    def _text(self, text: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self._apply_cors_headers()
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
            self._apply_cors_headers()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return True
        except Exception:
            return False


    # ─── 版本管理端点 ─────────────────────────────

    def _handle_versions_list(self, file_path: str):
        """GET /api/versions?file=<path> — 获取文件版本历史"""
        try:
            # 安全修复 (P0): file_path 路径白名单校验，防止任意文件读取
            ok, reason = _validate_output_path(file_path)
            if not ok:
                return self._json({"error": f"file_path 校验失败: {reason}"}, 400)
            from .version_manager import get_version_manager
            vm = get_version_manager()
            history = vm.history(file_path, limit=50)
            self._json({"status": "ok", "file": file_path, "versions": history, "count": len(history)})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_versions_diff(self, file_path: str, v1: int, v2: int):
        """GET /api/versions/diff?file=<path>&v1=N&v2=M — 对比两版本"""
        try:
            # 安全修复 (P0): file_path 路径白名单校验，防止任意文件读取
            ok, reason = _validate_output_path(file_path)
            if not ok:
                return self._json({"error": f"file_path 校验失败: {reason}"}, 400)
            from .version_manager import get_version_manager
            vm = get_version_manager()
            diff_text = vm.diff(file_path, v1, v2)
            self._json({"status": "ok", "file": file_path, "v1": v1, "v2": v2, "diff": diff_text})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_versions_rollback(self, file_path: str, version: int):
        """POST /api/versions/rollback — 回滚到指定版本"""
        try:
            # 安全修复 (P0): file_path 路径白名单校验，防止任意文件写入
            ok, reason = _validate_output_path(file_path)
            if not ok:
                return self._json({"error": f"file_path 校验失败: {reason}"}, 400)
            from .version_manager import get_version_manager
            vm = get_version_manager()
            result = vm.rollback(file_path, version)
            status_code = 200 if result["status"] == "ok" else 404
            self._json(result, status_code)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_versions_stats(self):
        """GET /api/versions/stats — 版本管理统计"""
        try:
            from .version_manager import get_version_manager
            vm = get_version_manager()
            self._json({"status": "ok", **vm.stats()})
        except Exception as e:
            self._json({"error": str(e)}, 500)


    # 回环地址集合：未配置 API key 时仅对这些绑定地址放行（本机信任模式）
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}

    def _check_auth(self) -> bool:
        """校验 API key。

        - 配置了 api_key：所有受保护端点必须携带有效凭证（Bearer 头或 ?token=）
        - 未配置 api_key 且服务绑定回环地址：放行（本机信任模式，与 docs/api.md 一致）
        - 未配置 api_key 且绑定非回环地址：拒绝（AdminAPI.start 已在此组合下拒绝启动，
          此处为兜底防线）
        /health 与静态资源已在路由层豁免。
        """
        if not self.api_key:
            return getattr(self, "server_host", "127.0.0.1") in self._LOOPBACK_HOSTS
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

    def log_message(self, fmt, *args):
        """访问日志拼装点：requestline 中的 ?token= 凭证打码后再输出"""
        masked = tuple(mask_token_query(a) if isinstance(a, str) else a for a in args)
        sys.stderr.write(
            f"{self.address_string()} - - "
            f"[{self.log_date_time_string()}] {fmt % masked}\n")

    def _require_confirm(self, action: str, params: dict | None = None) -> bool:
        """危险操作二次确认门：X-Confirm: yes 放行并落审计日志，缺失返回 428"""
        if (self.headers.get("X-Confirm") or "").strip().lower() != "yes":
            self._json({"error": "missing X-Confirm header"}, 428)
            return False
        self._audit_dangerous_op(action, params or {})
        return True

    def _audit_dangerous_op(self, action: str, params: dict) -> None:
        with contextlib.suppress(Exception):
            from .observability import get_logger
            get_logger().info(
                f"[AdminAPI] 危险操作审计: {action}",
                action=action,
                key_id=self._key_identity(),
                client=self.client_address[0],
                params=_fast_dumps(params, default=str),
            )

    def _key_identity(self) -> str:
        """凭证指纹（sha256 前 12 位），避免审计日志落明文 key"""
        candidates: list[str] = []
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[len("Bearer "):].strip())
        if "?" in getattr(self, "path", ""):
            for kv in self.path.split("?", 1)[1].split("&"):
                if kv.startswith("token="):
                    candidates.append(kv[len("token="):])
        for tok in candidates:
            if tok:
                return "sha256:" + hashlib.sha256(tok.encode()).hexdigest()[:12]
        return "trust" if not self.api_key else "anonymous"

    def do_GET(self):
        try:
            # 静态资源免鉴权
            if self._serve_static(self.path):
                return
            # 健康端点免鉴权（只读，供监控探针使用）
            if self.path == "/health":
                self._handle_health()
                return
            # 安全修复 (P0): /stream 触发文档生成会消耗 LLM 配额并执行流水线，
            # 必须鉴权；原先置于 _check_auth() 之前导致任意未授权客户端可滥用
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return
            if self.path.startswith("/stream"):
                self._handle_stream()
                return

            if self.path == "/metrics":
                self._handle_metrics()
            elif self.path == "/tasks":
                self._handle_list_tasks()
            elif self.path.startswith("/tasks/"):
                task_id = self.path.split("/tasks/")[1].split("/")[0]
                if not _validate_task_id(task_id):
                    return self._json({"error": "invalid task id"}, 400)
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
            elif self.path == "/api/cost":
                self._handle_cost_stats()
            elif self.path == "/api/events/hooks":
                self._handle_list_hooks()
            elif self.path == "/dlq":
                self._handle_list_dlq()
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                raw_id = self.path.split("/dlq/")[1].split("/")[0]
                try:
                    dlq_id = int(raw_id)
                except ValueError:
                    return self._json({"error": f"invalid dlq id: {raw_id!r}"}, 400)
                if not self._require_confirm("dlq_replay", {"dlq_id": dlq_id}):
                    return
                self._handle_replay_dlq(dlq_id)
            elif self.path.split("?", 1)[0] == "/api/versions/stats":
                self._handle_versions_stats()
            elif self.path.split("?", 1)[0] == "/api/versions/diff":
                qs = self._parse_query()
                file_path = qs.get("file", "")
                try:
                    v1, v2 = int(qs.get("v1", "")), int(qs.get("v2", ""))
                except ValueError:
                    return self._json({"error": "v1/v2 必须为整数"}, 400)
                self._handle_versions_diff(file_path, v1, v2)
            elif self.path.split("?", 1)[0] == "/api/versions":
                qs = self._parse_query()
                file_path = qs.get("file", "")
                if not file_path:
                    return self._json({"error": "缺少 file 参数"}, 400)
                self._handle_versions_list(file_path)
            elif self.path == "/api/dashboard":
                self._handle_dashboard()
            elif self.path == "/api/pipeline":
                self._handle_pipeline()
            elif self.path == "/api/openapi.json":
                from .openapi_spec import generate_spec
                self._json(generate_spec())
            elif self.path == "/api/alerts":
                self._handle_alerts()
            elif self.path == "/api/quality/feedback":
                from .quality_feedback import get_quality_feedback
                self._json(get_quality_feedback().stats())
            elif self.path.startswith("/api/logs"):
                self._handle_logs()
            elif self.path == "/":
                self._text("Doc-Pipeline Admin API v1\n"
                           "Endpoints: /health /metrics /tasks /agents /dlq /api/dashboard /api/pipeline /stream")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            _logger.exception("[AdminAPI] GET %s 处理异常: %s", self.path, e)
            self._json({"error": "internal server error"}, 500)

    def do_POST(self):
        try:
            # 静态资源/健康免鉴权
            if self._serve_static(self.path):
                return
            if self.path == "/health":
                self._handle_health()
                return
            # 鉴权前置：未通过前不读取请求体（HTTP/1.0 无 keep-alive，可安全直接响应）
            if not self._check_auth():
                self._json({"error": "unauthorized"}, 401)
                return

            # 读取请求体（已通过鉴权）
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""

            # 处理 POST 专属路由
            if self.path == "/api/tasks":
                self._handle_submit_task(body)
                return
            if self.path.startswith("/tasks/"):
                task_id = self.path.split("/tasks/")[1].split("/")[0]
                if not _validate_task_id(task_id):
                    return self._json({"error": "invalid task id"}, 400)
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
            elif self.path == "/api/versions/rollback":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (ValueError, UnicodeDecodeError):
                    return self._json({"error": "请求体不是合法 JSON"}, 400)
                file_path = payload.get("file", "")
                version = payload.get("version")
                if not file_path or not isinstance(version, int):
                    return self._json(
                        {"error": '需要 JSON 体 {"file": <path>, "version": <int>}'}, 400)
                if not self._require_confirm(
                        "versions_rollback", {"file": file_path, "version": version}):
                    return
                self._handle_versions_rollback(file_path, version)
                return
            elif self.path.startswith("/dlq/") and self.path.endswith("/replay"):
                raw_id = self.path.split("/dlq/")[1].split("/")[0]
                try:
                    dlq_id = int(raw_id)
                except ValueError:
                    return self._json({"error": f"invalid dlq id: {raw_id!r}"}, 400)
                if not self._require_confirm("dlq_replay", {"dlq_id": dlq_id}):
                    return
                self._handle_replay_dlq(dlq_id)
                return
            elif self.path == "/api/config":
                summary: dict = {}
                with contextlib.suppress(Exception):
                    data = _fast_loads(body) if body else {}
                    if isinstance(data, dict):
                        summary = {"key": data.get("key")}
                if not self._require_confirm("config_set", summary):
                    return
                self._handle_config_set(body)
                return
            elif self.path == "/api/config/reload":
                self._handle_config_reload()
                return
            elif self.path == "/api/cache/clear":
                if not self._require_confirm("cache_clear"):
                    return
                self._handle_cache_clear()
                return
            elif self.path == "/api/cost/budget":
                self._handle_set_budget(body)
                return
            elif self.path == "/api/events/hooks":
                self._handle_register_hook(body)
                return

            self._json({"error": "method not allowed"}, 405)
        except Exception as e:
            _logger.exception("[AdminAPI] POST %s 处理异常: %s", self.path, e)
            self._json({"error": "internal server error"}, 500)

    def do_OPTIONS(self):
        """CORS 预飞：仅白名单 Origin 放行（同源 / 回环同端口 / ADMIN_CORS_ORIGINS）"""
        origin = self.headers.get("Origin")
        allowed = self._allowed_origin(origin)
        if origin and not allowed:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "origin not allowed"}')
            return
        self.send_response(204)
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
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
            _logger.exception("[AdminAPI] DELETE %s 处理异常: %s", self.path, e)
            self._json({"error": "internal server error"}, 500)

    def _handle_health(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 503)
        h = self.orch.bus.health()
        h["registry_agents"] = len(self.orch.registry.list())
        h["tasks_running"] = len(self.orch.list_tasks())
        self._json(h)

    def _handle_metrics(self):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 503)
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
        result = dict(getattr(task, "result", {}))
        output_content = None
        output_path_found = None
        _MAX_OUTPUT = 512 * 1024
        for key in ("safe_writer", "safewriter", "layout", "checker"):
            val = result.get(key)
            path = None
            if isinstance(val, dict):
                path = val.get("output_path") or val.get("path") or val.get("file")
            elif isinstance(val, str) and val.endswith(".md"):
                path = val
            if path and Path(path).exists():
                output_path_found = str(path)
                with contextlib.suppress(Exception):
                    size = Path(path).stat().st_size
                    if size <= _MAX_OUTPUT:
                        output_content = Path(path).read_text(encoding="utf-8")
                    else:
                        output_content = f"[文件过大 {size} bytes，已省略，路径: {path}]"
                break
        self._json({
            "id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            "input_file": getattr(task, "input_file", ""),
            "config": getattr(task, "config", {}),
            "result": result,
            "output_path": output_path_found,
            "output_content": output_content,
            "created_at": getattr(task, "created_at", None),
            "finished_at": getattr(task, "finished_at", None),
            "error": getattr(task, "error", None),
        })

    def _attach_stream_callback(self, task_id: str):
        """为 API 提交的任务挂接流式回调（供 GET /stream?task_id= 订阅实时进度）"""
        try:
            from .streaming import StreamCallback, register_callback
            writer_agent = self._find_streaming_agent()
            if writer_agent is None:
                return None
            callback = StreamCallback()
            # 按 task_id 注册到 writer 的回调注册表（并发任务互不串扰；
            # 旧单槽 _active_stream_callback 在多任务并发时会互相覆盖）
            writer_agent._register_stream_callback(task_id, callback)
            register_callback(task_id, callback)
            track_stream_callback(task_id, callback)
            return callback
        except Exception:
            return None

    def _detach_stream_callback(self, task_id: str, callback) -> None:
        if callback is None:
            return
        with contextlib.suppress(Exception):
            from .streaming import unregister_callback
            unregister_callback(task_id)
        untrack_stream_callback(task_id)
        with contextlib.suppress(Exception):
            writer_agent = self._find_streaming_agent()
            if writer_agent is not None \
                    and writer_agent._get_stream_callback(task_id) is callback:
                writer_agent._unregister_stream_callback(task_id)
        with contextlib.suppress(Exception):
            callback.close()

    def _schedule_input_cleanup(self, task_id: str, input_file: Path,
                                callback=None, max_wait_seconds: int = 1800):
        """wait=false 时后台线程仍会读取输入文件，待任务终态后再清理临时文件。"""
        orch = self.orch

        def _reap():
            deadline = time.time() + max_wait_seconds
            active = True
            while time.time() < deadline:
                task = None
                with contextlib.suppress(Exception):
                    task = orch.get_task(task_id) if orch else None
                if task is None:
                    active = False
                    break
                status = str(getattr(task.status, "value", task.status))
                if status not in ("pending", "running", "paused"):
                    active = False
                    break
                time.sleep(2.0)
            self._detach_stream_callback(task_id, callback)
            if not active:
                with contextlib.suppress(OSError):
                    input_file.unlink()

        threading.Thread(target=_reap, daemon=True,
                         name=f"tmp-clean-{task_id}").start()

    def _handle_submit_task(self, body: bytes):
        """提交新文档生成任务

        body JSON:
          query (str, 必填) — 文档主题/查询
          title (str, 可选) — 文档标题，默认同 query
          pipeline (str, 可选) — 流水线名，默认 docgen
          wait (bool, 可选) — 是否同步等待完成，默认 false
          output (str, 可选) — 输出文件路径
        """
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            data = _fast_loads(body) if body else {}
        except Exception:
            return self._json({"error": "invalid JSON body"}, 400)

        query = data.get("query", "").strip()
        if not query:
            return self._json({"error": "missing 'query' field"}, 400)

        title = data.get("title", query)
        pipeline_name = data.get("pipeline", "docgen")
        wait = bool(data.get("wait", False))
        output_path = data.get("output", "")
        # 安全修复 (P0): output 路径白名单校验，防止任意文件写入
        if output_path:
            ok, reason = _validate_output_path(output_path)
            if not ok:
                return self._json({"error": f"output 路径校验失败: {reason}"}, 400)

        import tempfile
        task_id = new_task_id()

        try:
            from .scheduler import Scheduler
            sched = Scheduler()
            plan = sched.parse(pipeline_name)
        except Exception as e:
            return self._json({"error": f"failed to parse pipeline '{pipeline_name}': {e}"}, 400)

        input_file = Path(tempfile.gettempdir()) / f"task_{task_id}.md"
        input_file.write_text(f"# {title}\n\n## 查询\n\n{query}\n", encoding="utf-8")

        callback = self._attach_stream_callback(task_id)

        try:
            task = self.orch.run_plan(plan, input_file=str(input_file),
                                      task_id=task_id, wait=wait)
        except Exception as e:
            self._detach_stream_callback(task_id, callback)
            with contextlib.suppress(OSError):
                input_file.unlink()
            return self._json({"error": f"pipeline execution failed: {e}"}, 500)

        response = {
            "task_id": task.id,
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "pipeline": task.pipeline_name,
            "query": query,
            "title": title,
        }

        if wait:
            result = dict(getattr(task, "result", {}))
            response["result"] = result
            response["error"] = getattr(task, "error", None)
            for key in ("safe_writer", "safewriter", "layout", "checker"):
                val = result.get(key)
                if isinstance(val, dict):
                    path = val.get("output_path") or val.get("path") or val.get("file")
                    if path and Path(path).exists():
                        response["output_path"] = path
                        with contextlib.suppress(Exception):
                            response["output_content"] = Path(path).read_text(encoding="utf-8")
                        break
                elif isinstance(val, str) and val.endswith(".md") and Path(val).exists():
                    response["output_path"] = val
                    with contextlib.suppress(Exception):
                        response["output_content"] = Path(val).read_text(encoding="utf-8")
                    break

            self._detach_stream_callback(task_id, callback)
            with contextlib.suppress(OSError):
                input_file.unlink()
        else:
            response["message"] = "task started, poll GET /tasks/{task_id} for status"
            # 后台线程仍会读取输入文件 → 任务结束后由 reaper 清理
            self._schedule_input_cleanup(task_id, input_file, callback)

        self._json(response)

    def _handle_cancel_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if self.orch.get_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
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
        if self.orch.get_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
        ok = self.orch.pause(task_id)
        self._json({"paused": ok, "task_id": task_id})

    def _handle_resume_task(self, task_id: str):
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        if self.orch.get_task(task_id) is None:
            return self._json({"error": "task not found"}, 404)
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
            return self._json({"error": "orchestrator not set"}, 500)
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
            return self._json({"error": "orchestrator not set"}, 500)
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
        detail = {  # type: ignore[var-annotated]
            "name": agent_name,
            "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
            "meta": {},
        }
        meta = getattr(agent, "meta", None)
        if meta:
            for attr in ("version", "description", "priority", "input_topics",
                         "output_topics", "dependencies", "cache_ttl", "respawn",
                         "health_check_interval", "tags"):
                detail["meta"][attr] = getattr(meta, attr, None)  # type: ignore[index]
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
            data = _fast_loads(body) if body else {}
        except (json.JSONDecodeError, Exception):
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

    def _handle_cost_stats(self):
        """LLM 成本统计"""
        try:
            from pipeline_core.cost_tracker import get_cost_tracker
            self._json(get_cost_tracker().summary())
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_set_budget(self, body: bytes):
        """设置预算上限"""
        try:
            data = _fast_loads(body) if body else {}
            max_cost = float(data.get("max_cost", 0))
            from pipeline_core.cost_tracker import get_cost_tracker
            get_cost_tracker().set_budget(max_cost)
            self._json({"budget_set": True, "max_cost": max_cost})
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def _handle_config_reload(self):
        """热重载配置并通知所有 Agent"""
        if not self.orch:
            return self._json({"error": "orchestrator not set"}, 500)
        try:
            self.orch.config.reload()
            notified = 0
            for agent_info in self.orch.registry.list():
                name = agent_info.get("name", "")
                instance = self.orch.registry.get_instance(name)
                if instance and hasattr(instance, "on_config_update"):
                    with contextlib.suppress(Exception):
                        instance.on_config_update(changed_keys=["*"])
                        notified += 1
            self._json({"reloaded": True, "agents_notified": notified})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_alerts(self):
        """告警列表"""
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)
        level = params.get("level", [None])[0]
        category = params.get("category", [None])[0]
        try:
            limit = int(params.get("limit", ["50"])[0])
        except ValueError:
            return self._json({"error": "limit 必须为整数"}, 400)
        from .alert_manager import get_alerts
        self._json({"alerts": get_alerts(level=level, category=category, limit=limit)})

    def _handle_logs(self):
        """结构化日志查询

        ?level=error&agent=writer&since=3600&limit=50
        """
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)
        level_filter = params.get("level", [None])[0]
        agent_filter = params.get("agent", [None])[0]
        try:
            since_seconds = int(params.get("since", ["3600"])[0])
            limit = int(params.get("limit", ["100"])[0])
        except ValueError:
            return self._json({"error": "since/limit 必须为整数"}, 400)

        log_dir = Path("logs")
        if not log_dir.exists():
            self._json({"logs": [], "count": 0})
            return

        cutoff = time.time() - since_seconds
        results = []
        for log_file in sorted(log_dir.glob("*.jsonl"), reverse=True):
            try:
                for line in log_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = _fast_loads(line)
                    except Exception:
                        continue
                    ts = entry.get("timestamp", 0)
                    if ts < cutoff:
                        continue
                    if level_filter and entry.get("level", "").lower() != level_filter.lower():
                        continue
                    if agent_filter and entry.get("agent", "") != agent_filter:
                        continue
                    results.append(entry)
                    if len(results) >= limit:
                        break
            except Exception:
                continue
            if len(results) >= limit:
                break
        self._json({"logs": results, "count": len(results)})

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
            data = _fast_loads(body) if body else {}
        except (json.JSONDecodeError, Exception):
            return self._json({"error": "invalid JSON body"}, 400)
        event = data.get("event")
        url = data.get("url")
        if not event or not url:
            return self._json({"error": "missing 'event' or 'url' field"}, 400)
        # 安全修复 (P0): webhook URL SSRF 防护，拒绝私有/保留 IP 与非 http(s) 协议
        ok, reason = _validate_webhook_url(url)
        if not ok:
            return self._json({"error": f"webhook url 校验失败: {reason}"}, 400)
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

    _SSE_HEARTBEAT_SECONDS = 15.0
    _BROKEN_PIPE_CANCEL_THRESHOLD = 3

    def _send_sse(self, event) -> bool:
        """写出一条 SSE 帧（统一走 StreamEvent.to_dict() 序列化，含 ts/section/total）。

        返回 False 表示客户端连接已断开（BrokenPipe/ConnectionReset）。
        """
        payload = _fast_dumps(event.to_dict(), default=str)
        frame = f"id: {event.event_id}\ndata: {payload}\n\n"
        try:
            self.wfile.write(frame.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _send_heartbeat(self) -> bool:
        """SSE 注释帧心跳（迭代空转时保活连接）"""
        try:
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _cancel_for_broken_pipe(self, task_id: str):
        """客户端连续断管 → 取消任务止损 LLM 配额"""
        _logger.warning("[AdminAPI] SSE 客户端连续断开，取消任务 %s 止损", task_id)
        cancel = getattr(self.orch, "cancel", None) if self.orch else None
        if callable(cancel):
            with contextlib.suppress(Exception):
                cancel(task_id)

    def _pump_sse(self, callback, task_id: str, start_cursor: int = 0) -> None:
        """推送回调事件流（游标式只读，多客户端安全）。

        每个客户端持独立 event_id 游标，从回调事件历史读取而非破坏性出队——
        多个 SSE 客户端并发订阅同一任务时各自拿到完整事件流，互不瓜分
        （修复：原 get_events() 出队语义下第二个客户端会抢走第一个的事件）。
        空闲 15s 发送心跳帧；连续 BrokenPipe 达阈值则取消任务并停止。
        """
        broken = 0
        last_frame = time.time()
        cursor = start_cursor
        while True:
            events = callback.get_events_since(cursor)
            for ev in events:
                cursor = ev.event_id
                if self._send_sse(ev):
                    broken = 0
                    last_frame = time.time()
                else:
                    broken += 1
                if ev.event_type in ("complete", "error"):
                    return
                if broken >= self._BROKEN_PIPE_CANCEL_THRESHOLD:
                    self._cancel_for_broken_pipe(task_id)
                    return
            if callback.is_closed():
                return
            if time.time() - last_frame >= self._SSE_HEARTBEAT_SECONDS:
                if self._send_heartbeat():
                    last_frame = time.time()
                else:
                    broken += 1
                    if broken >= self._BROKEN_PIPE_CANCEL_THRESHOLD:
                        self._cancel_for_broken_pipe(task_id)
                        return
                    last_frame = time.time()
                continue
            time.sleep(0.2)

    def _stream_replay(self, task_id: str, query: str,
                       existing_callback, last_event_id: int) -> None:
        """SSE 重连：从 Last-Event-ID 游标继续推送（历史只读，无需二次入队）"""
        from .streaming import StreamEvent
        self._send_sse(StreamEvent("connected", {"task_id": task_id, "query": query,
                                                 "resumed_from": last_event_id,
                                                 "reconnect": True}))
        replayed = len(existing_callback.get_events_since(last_event_id))
        self._send_sse(StreamEvent("resumed", {"replayed": replayed}))
        self._pump_sse(existing_callback, task_id, start_cursor=last_event_id)

    def _find_streaming_agent(self):
        """查找具备 handle_streaming 能力的 writer agent 实例"""
        for a in self.orch.registry._agents.values():
            if hasattr(a, "handle_streaming"):
                return a
        return None

    def _start_stream_worker(self, writer_agent, task_id: str,
                             input_file: Path, callback) -> threading.Thread:
        """后台线程执行完整流水线（fetcher/researcher/writer 走 run_plan_async 真异步，
        替代旧的 writer_agent.handle_streaming 单 Agent 方式），完成后回调 complete/error"""
        orch = self.orch

        def _run():
            import asyncio as _aio

            from pipeline_core.scheduler import Scheduler
            try:
                sched = Scheduler()
                plan = sched.parse("docgen")
                # 按 task_id 注册回调，writer 的 _restructure_document 会按 task_id 拾取
                writer_agent._register_stream_callback(task_id, callback)
                task = _aio.run(orch.run_plan_async(
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
                writer_agent._unregister_stream_callback(task_id)
                with contextlib.suppress(OSError):
                    input_file.unlink()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        return worker

    def _handle_stream(self):
        """SSE 流式端点 —— 实时推送文档生成进度。

        用法: GET /stream?task_id=<id>&query=<topic>&title=<title>
        重连: 客户端断线后携带 Last-Event-ID header 重连，从断点继续推送。
        指标: GET /stream/metrics 获取流式指标快照。
        返回: text/event-stream 格式，每个事件含 id/type/data/section/total 字段。
        """
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # 指标查询端点（聚合所有活跃流的 StreamMetrics，而非新建实例恒报零）
        if parsed.path == "/stream/metrics":
            self._json(stream_metrics_snapshot())
            return

        task_id = params.get("task_id", ["stream"])[0]
        # task_id 会拼进临时输入文件名（stream_{task_id}.md），必须做字符白名单校验，
        # 防止 ../ 路径遍历把攻击者控制的内容写到任意路径（与 /tasks 路由同一防线）
        if task_id != "stream" and not _validate_task_id(task_id):
            self._json({"error": "invalid task id"}, 400)
            return
        query = params.get("query", [""])[0]
        title = params.get("title", ["自动生成文档"])[0]

        # SSE 重连: 检查 Last-Event-ID header
        last_event_id = 0
        last_event_id_header = self.headers.get("Last-Event-ID")
        if last_event_id_header:
            with contextlib.suppress(ValueError):
                last_event_id = int(last_event_id_header)

        from pipeline_core.streaming import (
            StreamCallback,
            StreamEvent,
            get_callback,
            register_callback,
            unregister_callback,
        )

        # 已有活跃流：直接挂接订阅（replay 历史后续推），不再重复起流水线
        existing_callback = get_callback(task_id) if task_id != "stream" else None

        if not query and existing_callback is None:
            self._json({"error": "missing 'query' parameter"}, 400)
            return

        # SSE headers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._apply_cors_headers()
        self.end_headers()

        if existing_callback:
            self._stream_replay(task_id, query, existing_callback, last_event_id)
            return

        # 首次连接
        self._send_sse(StreamEvent("connected", {"task_id": task_id, "query": query,
                                                 "resumed_from": 0, "reconnect": False}))

        if not self.orch:
            self._send_sse(StreamEvent("error", {"error": "orchestrator not set"}))
            return

        try:
            # 获取 writer agent 实例
            writer_agent = self._find_streaming_agent()
            if not writer_agent:
                self._send_sse(StreamEvent("error", {"error": "writer agent not found"}))
                return

            callback = StreamCallback()
            register_callback(task_id, callback)
            track_stream_callback(task_id, callback)

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

            worker = self._start_stream_worker(
                writer_agent, task_id, input_file, callback)

            # 流式推送事件（心跳 + 断管止损），直到 complete/error
            self._pump_sse(callback, task_id)

            # 等待后台生成线程完整结束，确保 complete/error 事件已推送给客户端；
            # timeout=120s 防止流水线挂死时 HTTP 线程永久阻塞（daemon 线程，
            # 超时后仅放弃等待并注销回调，不影响后台线程自行退出）。
            worker.join(timeout=120)
            if worker.is_alive():
                _logger.warning("[AdminAPI] 流式任务 %s 后台线程 %ss 未结束，提前断开",
                                task_id, 120)

        except Exception as e:
            self._send_sse(StreamEvent("error", {"error": "internal server error"}))
            _logger.exception("[AdminAPI] 流式任务 %s 异常: %s", task_id, e)
        finally:
            unregister_callback(task_id)
            untrack_stream_callback(task_id)


class AdminAPI:
    """管理 API 服务器"""

    def __init__(self, host: str = "127.0.0.1", port: int = 8910,
                 serve_static: bool = False, dashboard_dir: str | None = None,
                 api_key: str | None = None):
        self.host = host
        self.port = port
        self.serve_static = serve_static
        self.dashboard_dir = dashboard_dir
        # 从环境变量或参数获取 API key
        self.api_key = api_key or os.environ.get("ADMIN_API_KEY", "")
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, orch) -> bool:
        """在后台线程启动 API 服务器"""
        try:
            # 安全门：非回环绑定必须配置 API key（本机信任模式仅限回环地址）
            if not self.api_key and self.host not in AdminHandler._LOOPBACK_HOSTS:
                _logger.error(
                    f"[AdminAPI] 拒绝启动：绑定 {self.host} 但未设置 ADMIN_API_KEY。"
                    "公网/容器部署必须设置 ADMIN_API_KEY，或将 host 改为 127.0.0.1（仅本机）。"
                )
                return False
            AdminHandler.orch = orch
            AdminHandler.api_key = self.api_key
            AdminHandler.server_host = self.host
            if self.serve_static and self.dashboard_dir:
                AdminHandler.dashboard_dir = self.dashboard_dir
                _logger.info(f"[AdminAPI] 静态文件目录: {self.dashboard_dir}")
            if self.api_key:
                _logger.info("[AdminAPI] 已启用 API Key 鉴权")
            else:
                _logger.warning(
                    "[AdminAPI] 未配置 ADMIN_API_KEY：本机信任模式（仅回环访问免鉴权），"
                    "受保护端点对远程来源返回 401。"
                )
            self._server = ThreadingHTTPServer((self.host, self.port), AdminHandler)
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
