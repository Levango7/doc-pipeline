# 架构说明

本文描述 doc-pipeline 的分层结构、线程模型与一次任务的完整数据流。
模块职责速查表见 README「核心模块」章节。

---

## 1. 分层总览

```
┌─────────────────────────── 入口层 ───────────────────────────┐
│  run.py (CLI)   --mcp→ mcp_server.py (JSON-RPC/stdio)        │
│  --admin/--dashboard→ admin_api.py (ThreadingHTTPServer)      │
│  外部系统 → POST /api/tasks / SSE /stream                     │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌─────────────────────────── 编排层 ───────────────────────────┐
│  scheduler.py     pipeline YAML → ExecutionPlan（Schema+锁） │
│  pipeline.py      PipelineOrchestrator：run_plan 层级循环、   │
│                   checkpoint、recover_tasks、池化结果合并      │
│  dag_executor.py  DAGExecutor：节点调度（同步线程池/async）、  │
│                   重试退避、熔断、限流、步骤审计                │
│  executor_factory.py  thread/process 执行器工厂               │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌─────────────────────────── 传输层 ───────────────────────────┐
│  message_bus_v3.py  MessageBus：发布/订阅 + 请求响应 + 背压    │
│  message_store.py   PersistentStore：SQLite(WAL) 持久化 + DLQ │
│  task_queue.py      SQLite 任务队列（中断恢复）                │
└──────────────────────────┬────────────────────────────────────┘
                           ▼
┌─────────────────────────── Agent 层 ─────────────────────────┐
│  registry.py       注册表：元信息/健康检查/重生/热插拔          │
│  agent_loader.py   发现 + AST 安全沙箱加载                    │
│  base_agent.py     BaseAgent 契约（handle + 生命周期钩子）     │
│  agents/*.py       researcher → fetcher → writer →            │
│                    quality_gate → checker → layout → safe_writer │
└───────────────────────────────────────────────────────────────┘

横切组件：circuit_breaker.py / rate_limiter.py / cache_manager.py /
         observability.py(日志+Metrics) / event_hook.py(webhook) /
         llm_router.py / search_engines.py / cost_tracker.py /
         alert_manager.py / quality_feedback.py / version_manager.py
```

## 2. 核心概念

| 概念 | 定义位置 | 说明 |
|------|---------|------|
| `ExecutionPlan` | `scheduler.py` | YAML 解析产物：`levels`（拓扑层级）、`raw` 原始配置、`checkpoint` 策略 |
| `PipelineTask` | `pipeline.py` | 运行时任务状态：status/dag_nodes/steps/result/stop_event |
| `TaskNode` | `pipeline.py` | 统一节点模型：静态配置（timeout/retry/backoff）+ 运行时状态（attempts/status/result） |
| `AgentMeta` | `registry.py` | Agent 注册元信息 |
| 熔断器 | `circuit_breaker.py` | CLOSED/OPEN/HALF_OPEN 三态，per-agent 隔离，打开时快速失败 |
| 重生成循环 | `agents/quality_gate.py` | 评分不达标 → 触发 `REGENERATION_TARGET`（writer）重做 → 复检 |

## 3. 线程模型

| 线程 | 创建处 | 职责 | 关闭方式 |
|------|--------|------|---------|
| Bus worker | `MessageBus.__init__` | 消费异步队列、批量投递订阅者 | `bus.shutdown()` |
| Webhook 事件循环 | `event_hook.py` | aiohttp 异步投递 webhook | orchestrator shutdown 时通知 |
| Admin HTTP | `ThreadingHTTPServer`（daemon_threads=True） | 每连接一线程处理 REST/SSE | 进程退出 |
| 执行器池 | `executor_factory.create_executor` | 节点执行工作线程 | with 上下文自动关闭 |

关闭入口统一为 `orch.shutdown()`（`run.py` 各分支末尾调用），负责停总线、
清理 agent（含 `cleanup_stale_temp`）。SIGTERM 经 `_sigterm_handler` 转为
KeyboardInterrupt 走同一收尾路径。

## 4. 一次任务的数据流

以默认 `docgen` 流水线为例：

1. **提交**：CLI / `POST /api/tasks` → `orch.run_plan(plan, input_file, task_id)`
2. **建任务**：创建 `PipelineTask`（RUNNING）、注册到 `_running_tasks`、
   写入 SQLite `task_queue`、发 `task.created/started` 事件与 `pipeline.started` 消息
3. **层级循环**：按 `plan.levels` 逐层执行；每层前检查 cancel/pause、快照状态；
   `DAGExecutor.execute_level` 将层内节点提交线程池并发执行
4. **节点执行**：`execute_node_from_scheduler` → 组装 Message → `agent.handle(msg)`，
   结果写入任务输出；失败走指数退避重试（可被 stop_event 中断），最终失败触发
   熔断计数或 fail_fast 软中断；每步记录 StepResult + 审计 + Metrics
5. **质量闭环**：quality_gate 评分不达标时按重生成配置回退 writer 重做
6. **池化合并**：层内多实例（`*_pool_*`）结果按 `results_merge` 策略聚合
7. **收尾**：`_finalize_plan_task` 更新进度/时间戳 → task_queue 状态 → 事件钩子
   （completed/failed/cancelled）→ 报告生成 → 临时文件清理 → checkpoint 处理
8. **落盘**：safe_writer 原子写入输出文档并备份

断点续传：任务中断后 checkpoint 保留各 agent `on_snapshot()` 状态；
`--resume` 或 `--recover` 从 checkpoint/task_queue 恢复。

## 5. 设计取舍备注

- **进程模式**（`executor_type: process`）当前不可用：子进程内 registry/bus 为 None，
  无生产代码路径重建上下文（`dag_executor._execute_node_worker` 会显式报错）。
  生产使用线程模式即可。
- `MessageBus` 在构造时即启动 worker 线程——每个实例必须配套 `shutdown()`
  （orchestrator 与测试 fixture 已保证）。
