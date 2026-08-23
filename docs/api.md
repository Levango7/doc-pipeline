# Admin REST API 参考

启动方式：`python run.py <input> --admin`（或 `--dashboard`，隐含 `--admin`）。
默认监听 `http://127.0.0.1:8910`。在线规范：`GET /api/openapi.json`
（定义见 `pipeline_core/openapi_spec.py`，与本文同步）。

## 鉴权

- 设置环境变量 `ADMIN_API_KEY` 后启用；未配置时仅本机访问、无鉴权
- 请求携带 `Authorization: Bearer <key>` 或 `?token=<key>`
- `/health` 与静态资源（Dashboard）始终免鉴权

## 端点总览

### 系统

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| GET | `/health` | 总线 + Registry 健康状态 | 免 |
| GET | `/api/health/deep` | 深度健康检查（LLM/搜索/缓存） | 是 |
| GET | `/metrics` | Prometheus 格式指标 | 是 |
| GET | `/api/dashboard` | Dashboard 聚合数据 | 是 |

### 任务

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/tasks` | 任务列表 |
| POST | `/api/tasks` | 提交新任务（外部调度入口，支持同步/异步） |
| GET | `/tasks/{task_id}` | 单任务详情（含 result / output_content / output_path） |
| POST | `/tasks/{task_id}/cancel` | 取消任务 |
| POST | `/tasks/{task_id}/rerun` | 重跑流水线（复用 last plan） |
| POST | `/tasks/{task_id}/pause` | 暂停任务（断点保留） |
| POST | `/tasks/{task_id}/resume` | 恢复任务 |

提交任务请求体（schema `TaskSubmit`）：

```json
{
  "query": "文档主题/查询（必填）",
  "title": "可选标题",
  "pipeline": "docgen",
  "wait": false
}
```

任务详情响应核心字段（schema `TaskInfo`）：`id`、`status`
（`pending/running/done/failed/cancelled`）、`pipeline`、`result`、
`output_content`、`error`。

### Agent 与配置

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/agents` | 已注册 Agent 列表 |
| GET | `/api/agents/{name}` | Agent 详情 |
| GET | `/api/config` | 配置快照 |
| POST | `/api/config` | 更新单项配置（`{"key": ..., "value": ...}`） |
| POST | `/api/config/reload` | 热重载并通知所有 Agent 的 `on_config_update` |
| GET | `/api/cache` | 缓存统计 |
| POST | `/api/cache/clear` | 清空缓存 |

### 成本 / 告警 / 质量 / 日志

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/cost` | LLM 成本汇总（按供应商/Agent/时间维度） |
| POST | `/api/cost/budget` | 设置预算上限（超限熔断 LLM 调用），体：`{"max_cost": 数值}` |
| GET | `/api/alerts` | 告警历史，query 参数：`level`、`category`、`limit` |
| GET | `/api/quality/feedback` | 质量评分历史 + 弱项模式改进建议 |
| GET | `/api/logs` | 结构化日志查询，query 参数：`level`、`agent`、`since`（最近 N 秒）、`limit` |

### 事件钩子

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/events/hooks` | 已注册钩子列表 |
| POST | `/api/events/hooks` | 注册 webhook，体：`{"event": "...", "url": "..."}` |
| DELETE | `/api/events/hooks/{hook_id}` | 注销钩子 |

可用事件与行为见 README「事件钩子」章节。

### 死信队列 / 流式

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/dlq` | 死信列表 |
| POST | `/dlq/{dlq_id}/replay` | 重放死信消息 |
| GET | `/stream/metrics` | 流式推送指标快照（连接数/事件数等） |
| GET | `/stream` | SSE 流式生成，query 参数：`query`（必填）、`title`、`task_id`；
支持 `Last-Event-ID` 断线重连。返回 `text/event-stream`，免鉴权 |

---

实现位置：全部端点（含 `/api/*` 扩展）在 `pipeline_core/admin_api.py`（`AdminHandler` 路由分发）。
