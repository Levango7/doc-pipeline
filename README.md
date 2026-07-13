# Doc-Pipeline

> 多 Agent 协作的文档生成流水线 — DAG 编排 / 质量门控 / 弹性容错 / 可观测性

基于消息总线的多智能体文档生成系统。输入一个主题，自动完成**检索 → 抓取 → 写作 → 质量门控 → 检查 → 排版 → 安全落盘**全流程，输出结构化 Markdown 文档。

---

## ✨ 特性

| 类别 | 能力 |
|------|------|
| **编排** | DAG 并行执行、断点续传、可视化执行计划 |
| **检索** | Bing + Sogou + 360 三引擎（中国网络优化）、HTML 正文提取 |
| **抓取** | Async I/O（aiohttp 并发）/ 同步线程池降级、内容质量识别 |
| **写作** | TF-IDF 向量语义匹配、骨架生成、LLM 润色（可配置） |
| **质量** | QualityGate v2（Profile 模板机制）、Style Enforcer、Citation Verifier |
| **弹性** | 熔断器、限流器、Agent Pool、背压、自动重生成 |
| **可观测** | 结构化日志（轮转）、Prometheus Metrics、Admin REST API、Dashboard |
| **运维** | 版本锁定（Lockfile）、Schema 校验、基础鉴权、Docker 化 |

---

## 🚀 快速开始

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
| `pipeline_core/pipeline.py` | Orchestrator：DAG 执行、断点续传、rerun、日志轮转 |
| `pipeline_core/message_bus_v3.py` | SQLite 持久化消息总线 |
| `pipeline_core/circuit_breaker.py` | 熔断器（CLOSED/OPEN/HALF_OPEN） |
| `pipeline_core/rate_limiter.py` | 限流器 |
| `pipeline_core/registry.py` | Agent 注册表 |
| `pipeline_core/observability.py` | 结构化日志 + Metrics |
| `pipeline_core/admin_api.py` | REST API（健康/指标/任务/Rerun/鉴权） |
| `agents/` | 7 个 Agent 实现 |

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
      search_engines: [bing, sogou, 360]
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

---

## 🔌 管理 API

启动：`python run.py <input> --admin` （默认 `http://127.0.0.1:8910`）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 总线 + Registry 健康状态（免鉴权） |
| `/metrics` | GET | Prometheus 格式指标 |
| `/tasks` | GET | 任务列表 |
| `/tasks/<id>` | GET | 单任务详情 |
| `/tasks/<id>/cancel` | POST | 取消任务 |
| `/tasks/<id>/rerun` | POST | 重跑流水线（复用 last plan） |
| `/agents` | GET | 已注册 Agent |
| `/dlq` | GET | 死信队列 |
| `/dlq/<id>/replay` | POST | 重放死信 |
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

**89 个测试全部通过**，覆盖：Scheduler 解析、Schema 校验、Lockfile、消息总线、熔断器、限流器（含集成）、QualityGate、Agent 集成、容错注入、断点续传、管理 API。

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
├── pipeline_core/       # 核心编排框架
│   ├── scheduler.py     # DAG 计划 + Schema + Lockfile
│   ├── pipeline.py      # Orchestrator
│   ├── admin_api.py     # REST API
│   ├── message_bus_v3.py
│   ├── circuit_breaker.py
│   ├── rate_limiter.py
│   ├── registry.py
│   └── observability.py
├── pipelines/           # Pipeline 定义 + Quality Profile
│   ├── docgen.yaml
│   ├── test_pipeline.yaml
│   └── quality/
│       ├── technical-doc.yaml
│       └── tutorial.yaml
├── dashboard/           # 前端仪表盘
├── tests/               # 50 个测试
├── checkpoints/         # 断点 + 日志（自动轮转）
├── run.py               # CLI 入口
├── Dockerfile
├── requirements.txt
└── .github/workflows/ci.yml
```

---

## 📈 性能

| 指标 | 数值 |
|------|------|
| 端到端（docgen, 20 页抓取） | ~7s |
| Fetcher 并发 | 20 页 / 3s (aiohttp) |
| LLM 额度消耗 | 0（质量门控跳过，规则兜底） |
| 测试覆盖 | 50 tests |

---

## 📝 License

MIT
