# Doc-Pipeline

> 多 Agent 协作的文档生成流水线 — DAG 编排 / 质量门控 / 弹性容错 / 可观测性

基于消息总线的多智能体文档生成系统。输入一个主题，自动完成**检索 → 抓取 → 写作 → 质量门控 → 检查 → 排版 → 安全落盘**全流程，输出结构化 Markdown 文档。

---

## ✨ 特性

| 类别 | 能力 |
|------|------|
| **编排** | DAG 并行执行、断点续传、可视化执行计划、SQLite 任务队列恢复 |
| **需求** | **requirements_analyzer 需求分析器**（输入 → 结构化 DocumentSpec：类型/范围/读者/深度，置信度评分 + 追问建议，`--pipeline docreq`） |
| **检索** | Bocha + Tavily + Serper + Bing + Sogou + 360 六引擎、LRU+TTL 跨任务缓存 |
| **抓取** | Async I/O（aiohttp 并发）/ 同步线程池降级、内容质量识别 |
| **写作** | TF-IDF 向量语义匹配、骨架生成、LLM 润色、质量反馈闭环 |
| **质量** | QualityGate v2（Profile 模板）、Style Enforcer、Citation Verifier、评分历史学习、**fact_checker 事实核查**（数字类声明 vs 检索源一致性，`--pipeline docgen-verified`） |
| **弹性** | 熔断器、限流器、Agent Pool、背压、自动重生成、告警机制 |
| **可观测** | 结构化日志（轮转）、Prometheus Metrics、Admin REST API、Dashboard、日志查询 |
| **成本** | LLM 调用成本追踪（12 供应商定价）、预算熔断、`GET /api/cost` |
| **安全** | Agent 沙箱（AST 安全检查 + 白名单）、.env 明文密钥检测 |
| **运维** | 版本锁定、Schema 校验、基础鉴权、Docker 化、配置热更新、MCP Server |

---

## 🚀 快速开始

### 前置条件

- Python 3.11+
- 网络连接（用于检索引擎抓取内容）
- 搜索引擎 API Key（可选）— 复制 `.env.example` 为 `.env` 并填入 Bocha/Tavily/Serper 等 Key；无 Key 时默认使用 Bing/Sogou/360 免费引擎

### 3 步快速体验

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 生成文档
python run.py test_input.md -o output/my_doc.md

# 3. 查看结果
type output\my_doc.md
```

完成！输入文件是一行主题描述，输出是结构化 Markdown 文档。

### 安装

```bash
pip install -r requirements.txt
```

### 基本用法

```bash
# 生成文档（默认 docgen 流水线）
python run.py test_input.md --pipeline docgen --output out.md

# 指定检索词
python run.py topic.md --queries "RAG 架构" "检索增强生成" --pipeline docgen

# 仅预览执行计划（不实际运行）
python run.py test_input.md --plan

# 从断点恢复
python run.py test_input.md --resume --task-id <id>

# 启动 Dashboard（默认 http://127.0.0.1:8910）
python run.py test_input.md --dashboard
```

### CLI 参数

| 参数 | 说明 |
|------|------|
| `input` | 输入文件（Markdown/文本） |
| `--pipeline, -p` | 流水线名称（默认 `docgen`） |
| `--queries, -q` | 检索词（可多个） |
| `--output, -o` | 输出文件路径 |
| `--resume` | 从断点续传 |
| `--plan / --dry-run` | 仅预览计划，不执行 |
| `--admin / --dashboard` | 启动管理 API / 仪表盘 |
| `--daemon` | 执行完后保持 API 常驻 |
| `--mcp` | 启动 MCP Server（JSON-RPC over stdio，供外部 Agent 调度） |
| `--recover` | 启动时恢复中断的任务队列 |
| `--config, -c` | 自定义配置文件 |
| `--json-output` | 输出 JSON 结果（供 wrapper 解析） |

---

## 🏗️ 架构

```
                    ┌─────────────┐
   input.md  ──────▶ │  Researcher │  多引擎搜索 + 过滤
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │   Fetcher   │  并发下载 + 正文提取 + 质量识别
                    └──────┬──────┘
                           ▼
                    ┌─────────────┐
                    │   Writer    │  骨架 + TF-IDF 匹配 + LLM 润色
                    └──────┬──────┘
                           ▼
                 ┌───────────────────┐
                 │   QualityGate     │  Profile 评分 + Style/Citation 扣分
                 │  (不达标自动重生成) │
                 └──────┬────────────┘
                        ▼
                 ┌─────────────┐
                 │   Checker   │  死链/空链接/完整性检查
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │   Layout    │  标题层级/目录/格式优化
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ SafeWriter  │  原子写入 + 备份
                 └─────────────┘
```

### 核心模块

| 模块 | 职责 |
|------|------|
| `pipeline_core/scheduler.py` | 读取 pipeline YAML → ExecutionPlan（含 Schema 校验、Lockfile） |
| `pipeline_core/pipeline.py` | Orchestrator：DAG 执行、断点续传、rerun、统一节点模型 |
| `pipeline_core/dag_executor.py` | DAG 构建、节点调度、熔断/限流/重做循环 |
| `pipeline_core/message_bus_v3.py` | SQLite 持久化消息总线（WAL + 批量投递 + 背压） |
| `pipeline_core/circuit_breaker.py` | 熔断器（CLOSED/OPEN/HALF_OPEN） |
| `pipeline_core/rate_limiter.py` | 令牌桶限流器 |
| `pipeline_core/registry.py` | Agent 注册表（健康检查 + 热插拔） |
| `pipeline_core/observability.py` | 结构化日志（异步批量写入）+ Prometheus Metrics |
| `pipeline_core/admin_api.py` | REST API（多线程，健康/指标/任务/成本/告警/日志/配置管理） |
| `pipeline_core/version_manager.py` | 文档版本管理（自动版本号/diff/回滚） |
| `pipeline_core/batch_queue.py` | 批量文档生成队列 |
| `pipeline_core/cache_manager.py` | 统一缓存（memory/file/multi 三级） |
| `pipeline_core/llm_router.py` | 多供应商 LLM 路由器（10 供应商 fallback） |
| `pipeline_core/search_engines.py` | 统一搜索引擎接口（10 引擎 + LRU+TTL 缓存） |
| `pipeline_core/cost_tracker.py` | LLM 成本追踪（12 供应商定价表 + 预算熔断） |
| `pipeline_core/alert_manager.py` | 告警机制（熔断/DLQ/限流/预算超限通知） |
| `pipeline_core/quality_feedback.py` | 质量评分历史 + 弱项模式分析 + 写作建议 |
| `pipeline_core/task_queue.py` | SQLite 持久化任务队列（中断恢复） |
| `pipeline_core/mcp_server.py` | MCP Server（JSON-RPC 2.0 over stdio，5 tools） |
| `pipeline_core/openapi_spec.py` | OpenAPI 3.0 规范生成 |
| `pipeline_core/agent_loader.py` | Agent 安全加载（AST 检查 + 白名单沙箱） |
| `agents/` | 7 个 Agent 实现（researcher/fetcher/writer/quality_gate/checker/layout/safe_writer） |

---

## ⚙️ 配置

### Pipeline 定义（`pipelines/docgen.yaml`）

```yaml
defaults:
  timeout: 300
  retry:
    max_attempts: 3
    backoff: exponential
    initial_delay: 1.0

agents:
  - name: researcher
    version: "2.0"
    config:
      search_engines: [bocha, tavily, serper, bing, sogou, 360]
      max_results: 20

  - name: writer
    version: "2.0"
    config:
      template: default
      polish_cache_ttl: 3600

  # ... 其余 Agent

topology:
  type: dag
  levels:
    - [researcher]
    - [fetcher]
    - [writer]
    - [quality_gate]
    - [checker]
    - [layout]
    - [safewriter]
  edges:
    - [researcher, fetcher]
    - [fetcher, writer]
    # ...
```

### Quality Profile（`pipelines/quality/`）

```yaml
# technical-doc.yaml
name: technical-doc
threshold: 70
weights:
  completeness: 0.35
  accuracy: 0.25
  readability: 0.20
  style: 0.10
  citation: 0.10
style_rules:
  - id: bad_link
    pattern: '\]\((#[^)]*|javascript:|data:)'
    penalty: 15
    message: 空链接/危险协议
citation:
  enabled: true
  penalty_per_issue: 5
  check_url_format: true
```

运行时通过 pipeline YAML 的 `quality_profile` 切换。

### 生产环境配置

项目提供 `config.production.json` 模板，与默认 `config.json` 的区别：

| 配置项 | 默认 (config.json) | 生产 (config.production.json) |
|--------|-------------------|------------------------------|
| `researcher.search_engines` | `["bing", "sogou", "360"]`（免费 HTML 兜底引擎） | `["bocha", "tavily", "serper", "bing", "sogou", "360"]`（含付费 API 引擎） |
| `fail_fast` | `true` | `false`（单 Agent 失败不中断流水线） |
| `researcher.max_workers` | 3 | 5 |
| `researcher.cache_size` | 1000 | 2000 |
| `admin_api.host` / `admin_api.port` | `127.0.0.1` / `8910`（由 CLI 启停） | `0.0.0.0` / `8910`（**非回环绑定必须设置 `ADMIN_API_KEY`，否则拒绝启动**） |

```bash
# 使用生产配置
python run.py input.md -c config.production.json -o output/doc.md
```

---

## 🔌 管理 API

启动：`python run.py <input> --admin` （默认 `http://127.0.0.1:8910`）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 总线 + Registry 健康状态（免鉴权） |
| `/metrics` | GET | Prometheus 格式指标 |
| `/tasks` | GET | 任务列表 |
| `/tasks` | POST | 提交新任务（同步/异步，外部 Agent 调度入口） |
| `/tasks/<id>` | GET | 单任务详情（含 result/output_content/output_path） |
| `/tasks/<id>/cancel` | POST | 取消任务 |
| `/tasks/<id>/rerun` | POST | 重跑流水线（复用 last plan） |
| `/agents` | GET | 已注册 Agent |
| `/dlq` | GET | 死信队列 |
| `/dlq/<id>/replay` | POST | 重放死信 |
| `/api/cost` | GET | LLM 成本统计（按供应商/Agent/时间维度） |
| `/api/cost/budget` | POST | 设置预算上限（超限自动熔断） |
| `/api/alerts` | GET | 告警历史查询（level/category/since 过滤） |
| `/api/logs` | GET | 结构化日志查询（level/agent/since/keyword 过滤） |
| `/api/quality/feedback` | GET | 质量评分历史 + 弱项模式分析 |
| `/api/config/reload` | POST | 配置热更新（通知所有 Agent on_config_update） |
| `/api/openapi.json` | GET | OpenAPI 3.0 规范 |
| `/api/dashboard` | GET | Dashboard 数据 |
| `/` | GET | 静态仪表盘（若 `--dashboard`） |

### 鉴权

通过环境变量 `ADMIN_API_KEY` 或 `AdminAPI(api_key=...)` 启用：

```bash
export ADMIN_API_KEY="your-secret-key"
python run.py input.md --admin
```

客户端请求携带 `Authorization: Bearer <key>` 或 `?token=<key>`。
未配置时禁用鉴权（仅本机访问）。`/health` 与静态资源始终免鉴权。

### MCP Server

通过 `--mcp` 启动 JSON-RPC 2.0 over stdio，供外部 Agent（如 Claude Desktop）调度：

```bash
python run.py --mcp
```

提供 5 个 tools：`submit_task`、`get_task_status`、`get_task_result`、`list_tasks`、`get_system_health`。

### 事件钩子（Event Hooks）

通过 `POST /api/events/hooks` 注册 HTTP 回调，流水线事件触发时异步 POST JSON 到指定 URL：

| 事件 | 说明 |
|------|------|
| `task.created` | 任务创建 |
| `task.started` | 任务开始执行 |
| `task.completed` | 任务完成 |
| `task.failed` | 任务失败 |
| `task.cancelled` | 任务取消 |
| `agent.started` | Agent 启动 |
| `agent.error` | Agent 异常 |
| `quality_gate.evaluated` | 质量门控评分完成 |
| `circuit_breaker.open` | 熔断器打开 |
| `circuit_breaker.close` | 熔断器恢复 |

```bash
# 注册 webhook
curl -X POST http://127.0.0.1:8910/api/events/hooks \
  -H "Content-Type: application/json" \
  -d '{"event": "task.completed", "url": "https://your-webhook-handler.com/notify"}'

# 列出已注册钩子
curl http://127.0.0.1:8910/api/events/hooks

# 注销钩子
curl -X DELETE http://127.0.0.1:8910/api/events/hooks/<id>
```

Webhook 使用独立事件循环异步发送（aiohttp 连接池），不阻塞流水线主线程。

### Agent 安全沙箱

第三方 Agent 加载时自动执行 AST 安全检查（禁止 `os.system`/`subprocess`/`eval`/`exec`/`open` 等），
内置 Agent 白名单跳过检查。默认 `strict_safety=True`，检测到危险调用直接阻断加载。

---

## 🐳 Docker

```bash
docker build -t doc-pipeline .
docker run -p 8910:8910 -v $(pwd)/checkpoints:/app/checkpoints doc-pipeline
```

非 root 用户运行，暴露 8910 端口，`/app/checkpoints` 与 `/app/logs` 为持久化卷。

---

## 🧪 测试

```bash
python -m pytest tests/ -v
```

**588 个测试全部通过**（另有 6 个 e2e 测试默认跳过），覆盖：Scheduler 解析、Schema 校验、Lockfile、消息总线、熔断器、限流器（含集成）、QualityGate、Agent 集成、容错注入、断点续传、管理 API、并发压力、SSE 流式、执行器工厂、任务队列、成本追踪、告警机制、质量闭环、MCP Server、OpenAPI Spec、Agent 沙箱 + 配置热更新。

```bash
# 运行真实端到端测试（需要网络 + LLM API Key）
python -m pytest tests/ -m e2e -v
```

---

## 📊 质量门控机制

`QualityGate v2` 按 Profile 权重评分：

```
总分 = Σ(维度得分 × 权重) − 风格扣分 − 引用扣分
阈值 = profile.threshold (默认 70)

不达标 → 自动重生成 (最多 max_regenerations=3 次)
```

### 维度

| 维度 | 权重 (technical-doc) | 说明 |
|------|---------------------|------|
| 完整性 | 0.35 | 章节覆盖、引用数量 |
| 准确性 | 0.25 | 事实一致性 |
| 可读性 | 0.20 | 段落长度、术语密度 |
| 风格 | 0.10 | 标题规范、空链接检测 |
| 引用 | 0.10 | URL 格式、死链检测 |

---

## 📁 目录结构

```
doc-pipeline/
├── agents/              # 7 个 Agent 实现
├── pipeline_core/       # 核心编排框架（35 个模块）
│   ├── pipeline.py      # Orchestrator（统一节点模型）
│   ├── dag_executor.py  # DAG 构建 + 节点调度
│   ├── scheduler.py     # YAML → ExecutionPlan + Schema + Lockfile
│   ├── message_bus_v3.py # 消息总线（WAL + 批量投递）
│   ├── message_store.py  # SQLite 持久化层
│   ├── circuit_breaker.py # 熔断器
│   ├── rate_limiter.py  # 令牌桶限流
│   ├── registry.py      # Agent 注册表
│   ├── observability.py # 结构化日志（异步）+ Metrics
│   ├── admin_api.py     # REST API（多线程）
│   ├── version_manager.py # 文档版本管理
│   ├── batch_queue.py   # 批量文档队列
│   ├── cache_manager.py # 统一缓存
│   ├── llm_router.py    # LLM 多供应商路由
│   ├── search_engines.py # 10 引擎统一接口 + 缓存
│   ├── cost_tracker.py  # LLM 成本追踪 + 预算熔断
│   ├── alert_manager.py # 告警机制
│   ├── quality_feedback.py # 质量闭环学习
│   ├── task_queue.py    # SQLite 任务队列
│   ├── mcp_server.py    # MCP Server (JSON-RPC)
│   ├── openapi_spec.py  # OpenAPI 3.0 规范
│   ├── agent_loader.py  # Agent 安全加载（沙箱）
│   ├── streaming.py     # SSE 流式输出
│   └── ...              # 更多模块
├── pipelines/           # Pipeline 定义 + Quality Profile
│   ├── docgen.yaml      # 默认文档生成流水线
│   ├── three_pass.yaml  # 三阶段流水线（DAG 版）
│   ├── test_pipeline.yaml
│   └── quality/
│       ├── technical-doc.yaml
│       └── tutorial.yaml
├── dashboard/           # 前端仪表盘
├── tests/               # 588 个测试（+ 6 个 e2e）
├── checkpoints/         # 断点 + 日志（自动轮转）
├── versions/            # 文档版本存储
├── run.py               # CLI 入口
├── Dockerfile
├── requirements.txt
└── .github/workflows/ci.yml
```

---

## 📈 性能

> **注意**：以下数据来自 `benchmark.py` 的 mock 基准（模拟引擎，无网络 I/O，质量门控跳过 LLM），
> 反映框架本身的开销。默认 `config.json` 已启用 Bing/Sogou/360 免费 HTML 兜底引擎（无需 API Key，
> 结果质量低于付费 API 引擎）；完整多引擎检索 + LLM 润色请使用：
> `python run.py input.md -c config.production.json -o out.md`

| 指标 | 数值 | 模式 |
|------|------|------|
| 端到端（docgen, 20 页抓取） | ~7s | mock（无网络 I/O） |
| Fetcher 并发 | 20 页 / 3s (aiohttp) | 真实网络 |
| LLM 额度消耗 | 0（质量门控跳过，规则兜底） | mock |
| 消息总线吞吐 | 批量 drain 50 条/轮 | — |
| 缓存命中 | 125 万 ops/s | — |
| 测试覆盖 | 588 tests (+ 6 e2e) | — |

### 生产模式预期耗时（config.production.json）

| 阶段 | 预期耗时 | 说明 |
|------|----------|------|
| Researcher（真实搜索） | 3-8s | 取决于引擎响应速度 |
| Fetcher（20 页下载） | 2-4s | aiohttp 并发 |
| Writer（TF-IDF + LLM 润色） | 5-30s | LLM 调用为主要耗时 |
| QualityGate + Checker + Layout | <1s | 纯规则计算 |
| **总计** | **10-45s** | 视 LLM 可用性和网络状况 |

---

## 📚 文档

| 文档 | 内容 |
|------|------|
| [部署指南](docs/deployment.md) | 生产环境配置、API Key、监控、备份策略 |
| [API 参考](docs/api.md) | Admin REST API 全端点说明与鉴权 |
| [架构说明](docs/architecture.md) | 分层结构、线程模型、数据流 |
| [Agent 开发指南](docs/agents.md) | 自定义 Agent 契约、沙箱规则、最小示例 |

---

## 📝 License

MIT
