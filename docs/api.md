# Admin REST API 参考

启动方式：`python run.py <input> --admin`（或 `--dashboard`，隐含 `--admin`）。
默认监听 `http://127.0.0.1:8910`。在线规范：`GET /api/openapi.json`
（定义见 `pipeline_core/openapi_spec.py`，与本文同步）。

## 鉴权

- 设置环境变量 `ADMIN_API_KEY` 后启用；请求携带 `Authorization: Bearer <key>` 或 `?token=<key>`
  （仅 SSE 场景才建议 query token——`EventSource` 无法自定义请求头；常规请求请使用 `Authorization` 头，
  避免凭证落入访问日志）
- **未配置 `ADMIN_API_KEY` 时进入本机信任模式**：绑定回环地址（`127.0.0.1` 等）时免鉴权访问；
  绑定非回环地址（如 `0.0.0.0`）则**拒绝启动**（安全门，防止无凭证暴露公网）
- `/health` 与静态资源（Dashboard）始终免鉴权
- Dashboard 前端在收到 401 时会弹出 Token 输入框，保存于浏览器 sessionStorage（会话级，关闭标签页即失效）后自动重试

## 危险操作二次确认（X-Confirm）

以下四类危险操作除鉴权外还必须携带请求头 `X-Confirm: yes`，缺失返回
**428 Precondition Required**（body：`{"error": "missing X-Confirm header"}`）；
确认放行时会输出结构化审计日志（时间/key 身份/操作/参数摘要）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/config` | 更新运行时配置 |
| POST | `/api/cache/clear` | 清空所有缓存 |
| POST | `/api/versions/rollback` | 回滚文件到指定版本 |
| POST | `/dlq/{dlq_id}/replay` | 重放死信消息（GET 形式同样要求） |

示例：

```bash
curl -X POST http://127.0.0.1:8910/api/cache/clear \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "X-Confirm: yes"
```

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
| POST | `/api/config` | 更新单项配置（`{"key": ..., "value": ...}`），需 `X-Confirm: yes` |
| POST | `/api/config/reload` | 热重载并通知所有 Agent 的 `on_config_update` |
| GET | `/api/cache` | 缓存统计 |
| POST | `/api/cache/clear` | 清空缓存，需 `X-Confirm: yes` |

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

### 死信队列 / 流式 / 版本管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/dlq` | 死信列表 |
| POST | `/dlq/{dlq_id}/replay` | 重放死信消息，需 `X-Confirm: yes`（GET 形式同样要求） |
| GET | `/stream/metrics` | 流式推送指标快照（连接数/事件数等） |
| GET | `/stream` | SSE 流式生成，query 参数：`query`（必填）、`title`、`task_id`；
支持 `Last-Event-ID` 断线重连。返回 `text/event-stream`（需鉴权） |
| GET | `/api/versions?file=<path>` | 文件版本历史 |
| GET | `/api/versions/diff?file=<path>&v1=N&v2=M` | 对比两个版本 |
| GET | `/api/versions/stats` | 版本管理统计 |
| POST | `/api/versions/rollback` | 回滚到指定版本，JSON 体：`{"file": <path>, "version": <int>}`，需 `X-Confirm: yes` |

注：`cancel/pause/resume/rerun` 类操作对不存在的任务返回 **404**；任务存在但状态不符时
仍为 200 + `<action>: false`。

---

实现位置：全部端点（含 `/api/*` 扩展）在 `pipeline_core/admin_api.py`（`AdminHandler` 路由分发）。
