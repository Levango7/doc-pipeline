"""OpenAPI 3.0 Spec — 机器可读 API 定义

让 AI agent 和前端自动发现接口。
访问: GET /api/openapi.json
"""
from __future__ import annotations


def _schemas() -> dict:
    """components.schemas 定义"""
    return {
        "TaskSubmit": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "文档主题/查询"},
                "title": {"type": "string", "description": "文档标题"},
                "pipeline": {"type": "string", "default": "docgen"},
                "wait": {"type": "boolean", "default": False},
                "output": {"type": "string", "description": "输出文件路径（白名单目录内）"},
            },
        },
        "TaskInfo": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "running", "done", "failed", "cancelled"]},
                "pipeline": {"type": "string"},
                "result": {"type": "object"},
                "output_content": {"type": "string", "nullable": True},
                "error": {"type": "string"},
            },
        },
        "CostSummary": {
            "type": "object",
            "properties": {
                "total_cost": {"type": "number"},
                "budget": {"type": "number"},
                "budget_remaining": {"type": "number", "nullable": True},
                "budget_exceeded": {"type": "boolean"},
                "by_provider": {"type": "object"},
            },
        },
        "Error": {
            "type": "object",
            "properties": {
                "error": {"type": "string"},
            },
        },
    }


def _system_paths() -> dict:
    """系统级端点：健康检查 / 指标"""
    return {
        "/health": {
            "get": {
                "summary": "健康检查",
                "security": [],
                "responses": {"200": {"description": "健康状态"}},
            },
        },
        "/api/health/deep": {
            "get": {
                "summary": "深度健康检查（LLM/搜索/缓存）",
                "responses": {"200": {"description": "组件健康状态"}},
            },
        },
        "/metrics": {
            "get": {
                "summary": "Prometheus 指标",
                "responses": {"200": {"description": "Prometheus 格式指标"}},
            },
        },
    }


def _task_paths() -> dict:
    """任务生命周期端点：列表 / 详情 / 提交 / 取消 / 重跑 / 暂停 / 恢复"""
    return {
        "/tasks": {
            "get": {
                "summary": "任务列表",
                "responses": {"200": {"description": "任务列表"}},
            },
        },
        "/tasks/{task_id}": {
            "get": {
                "summary": "查询单任务（含结果和输出内容）",
                "parameters": [
                    {"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "任务详情", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TaskInfo"}}}},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/api/tasks": {
            "post": {
                "summary": "提交新文档生成任务",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TaskSubmit"}}},
                },
                "responses": {
                    "200": {"description": "任务已提交/完成"},
                    "400": {"description": "参数错误", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/cancel": {
            "post": {
                "summary": "取消任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "取消结果"},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/rerun": {
            "post": {
                "summary": "重跑任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "重跑已启动"},
                    "404": {"description": "原任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/pause": {
            "post": {
                "summary": "暂停任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "暂停结果"},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/tasks/{task_id}/resume": {
            "post": {
                "summary": "恢复任务",
                "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "恢复结果"},
                    "404": {"description": "任务不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
    }


def _resource_paths() -> dict:
    """资源查询端点：Agent / 配置 / 缓存"""
    return {
        "/agents": {
            "get": {
                "summary": "Agent 列表",
                "responses": {"200": {"description": "已注册 Agent"}},
            },
        },
        "/api/agents/{name}": {
            "get": {
                "summary": "Agent 详情",
                "parameters": [{"name": "name", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Agent 详情"}},
            },
        },
        "/api/config": {
            "get": {
                "summary": "读取配置",
                "responses": {"200": {"description": "配置快照"}},
            },
            "post": {
                "summary": "更新配置",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}}}}},
                "responses": {"200": {"description": "更新成功"}},
            },
        },
        "/api/cache": {
            "get": {
                "summary": "缓存统计",
                "responses": {"200": {"description": "缓存统计"}},
            },
        },
        "/api/cache/clear": {
            "post": {
                "summary": "清空缓存",
                "responses": {"200": {"description": "已清空"}},
            },
        },
    }


def _ops_paths() -> dict:
    """运维端点：成本 / 告警 / 质量 / 日志 / 事件钩子 / 死信 / SSE 流"""
    return {
        "/api/cost": {
            "get": {
                "summary": "LLM 成本统计",
                "responses": {"200": {"description": "成本汇总", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CostSummary"}}}}},
            },
        },
        "/api/cost/budget": {
            "post": {
                "summary": "设置预算上限",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"max_cost": {"type": "number"}}}}}},
                "responses": {"200": {"description": "预算已设置"}},
            },
        },
        "/api/alerts": {
            "get": {
                "summary": "告警列表",
                "parameters": [
                    {"name": "level", "in": "query", "schema": {"type": "string"}},
                    {"name": "category", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "告警列表"}},
            },
        },
        "/api/quality/feedback": {
            "get": {
                "summary": "质量闭环反馈统计",
                "responses": {"200": {"description": "质量评分历史和改进建议"}},
            },
        },
        "/api/logs": {
            "get": {
                "summary": "结构化日志查询",
                "parameters": [
                    {"name": "level", "in": "query", "schema": {"type": "string"}},
                    {"name": "agent", "in": "query", "schema": {"type": "string"}},
                    {"name": "since", "in": "query", "schema": {"type": "integer"}, "description": "最近 N 秒"},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "日志条目列表"}},
            },
        },
        "/api/config/reload": {
            "post": {
                "summary": "热重载配置并通知所有 Agent",
                "responses": {"200": {"description": "重载结果"}},
            },
        },
        "/api/events/hooks": {
            "get": {
                "summary": "事件钩子列表",
                "responses": {"200": {"description": "钩子列表"}},
            },
            "post": {
                "summary": "注册事件钩子",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"event": {"type": "string"}, "url": {"type": "string"}}}}}},
                "responses": {"200": {"description": "已注册"}},
            },
        },
        "/api/events/hooks/{hook_id}": {
            "delete": {
                "summary": "注销事件钩子",
                "parameters": [{"name": "hook_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "已注销"}},
            },
        },
        "/dlq": {
            "get": {
                "summary": "死信队列",
                "responses": {"200": {"description": "死信列表"}},
            },
        },
        "/dlq/{dlq_id}/replay": {
            "post": {
                "summary": "重放死信",
                "parameters": [{"name": "dlq_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "重放结果"}},
            },
        },
        "/api/dashboard": {
            "get": {
                "summary": "Dashboard 聚合数据（任务含 progress/steps + Agent 摘要）",
                "responses": {"200": {"description": "聚合状态"}},
            },
        },
        "/api/pipeline": {
            "get": {
                "summary": "流水线配置与版本信息",
                "responses": {"200": {"description": "流水线信息"}},
            },
        },
        "/api/versions": {
            "get": {
                "summary": "文件版本历史",
                "parameters": [{"name": "file", "in": "query", "required": True, "schema": {"type": "string"}}],
                "responses": {
                    "200": {"description": "版本列表"},
                    "400": {"description": "参数/路径校验失败", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/api/versions/diff": {
            "get": {
                "summary": "对比两个版本",
                "parameters": [
                    {"name": "file", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "v1", "in": "query", "required": True, "schema": {"type": "integer"}},
                    {"name": "v2", "in": "query", "required": True, "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {"description": "差异文本"},
                    "400": {"description": "参数错误", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/api/versions/stats": {
            "get": {
                "summary": "版本管理统计",
                "responses": {"200": {"description": "统计信息"}},
            },
        },
        "/api/versions/rollback": {
            "post": {
                "summary": "回滚到指定版本",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["file", "version"],
                    "properties": {"file": {"type": "string"}, "version": {"type": "integer"}},
                }}}},
                "responses": {
                    "200": {"description": "回滚结果"},
                    "404": {"description": "版本不存在", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}},
                },
            },
        },
        "/stream/metrics": {
            "get": {
                "summary": "流式推送指标快照（连接数/事件数等）",
                "responses": {"200": {"description": "text/event-stream"}},
            },
        },
        "/stream": {
            "get": {
                "summary": "SSE 流式文档生成",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "title", "in": "query", "schema": {"type": "string"}},
                    {"name": "task_id", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "text/event-stream"}},
            },
        },
    }


def generate_spec() -> dict:
    """生成 OpenAPI 3.0 spec"""
    try:
        from . import __version__
    except ImportError:  # pragma: no cover
        __version__ = "unknown"
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Doc-Pipeline Admin API",
            "description": "多 Agent 文档生成流水线管理 API",
            "version": __version__,
        },
        "servers": [
            {"url": "http://localhost:8910", "description": "本地开发"},
        ],
        # 全局安全要求：除显式标注 security: [] 的端点（/health）外均需 Bearer 凭证，
        # 与 AdminHandler._check_auth 实现一致
        "security": [{"BearerAuth": []}],
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                }
            },
            "schemas": _schemas(),
        },
        "paths": {
            **_system_paths(),
            **_task_paths(),
            **_resource_paths(),
            **_ops_paths(),
        },
    }
