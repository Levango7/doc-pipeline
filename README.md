# Doc-Pipeline

> **主打：多 Agent 协作的文档生成流水线** — 输入主题，自动检索/抓取/写作/质检/排版/落盘

基于消息总线的多智能体文档生成系统。输入一个主题，自动完成**检索 → 抓取 → 写作 → 质量门控 → 检查 → 排版 → 安全落盘**全流程，输出结构化 Markdown 文档。

## 场景分级

| 级别 | 场景 | 入口 | 说明 |
|---|---|---|---|
| **主打** | 文档生成 | `python run.py <input> --pipeline docgen` | 完整 7 Agent 流水线，生产可用 |
| **主打** | 文档生成 + 事实核查 | `--pipeline docgen-verified` | 增加 fact_checker 节点，数字类声明交叉验证 |
| **省钱** | 文档生成 + **条件升级核查** | `--pipeline docgen-lean` | fact_checker 挂 `when`：质量分达标才付核查成本，不达标时该节点被跳过而下游照常跑 |
| **主打** | **文档生成 + 多格式渲染** | `--pipeline docgen-render` | 追加 renderer 节点，产出 **docx / pdf**（可编辑 Word / 可打印归档） |
| **主打** | **本地资料 → 知识库接地文档** | `--pipeline kb-docgen` | 摄入自己的 PDF/图片/文本 → 切块向量入库 → 检索命中驱动写作；离线可跑（无 LLM 时如实产出抽取式草稿） |
| **实验性** | 需求分析 | `--pipeline docreq` | requirements_analyzer 输出结构化 DocumentSpec |
| **通用** | 任意 JSON 接口 → 报告 | `--pipeline api-report` | 无领域节点：`http_request` 取接口 → `transform` 挑字段渲染 → 落盘 |
| **通用** | 接口列表 → **逐项摘要** | `--pipeline api-digest` | 同一个 Agent 按 `foreach` 逐项跑 N 次，聚合产物再落盘（列表长度超 `max_items` 直接报错） |
| **实验性** | 文档增强 | `--enhance <input>` | 逐章节 LLM 深化 + 搜索补充 |
| **实验性** | MCP Server | `--mcp` | JSON-RPC 2.0 over stdio，供外部 Agent 调度 |

### 输出格式

| 格式 | 技术路线 | 适用 | 说明 |
|---|---|---|---|
| `.md` | — | 中间态 / 二次编辑 | 管线唯一中间产物，**始终保留** |
| `.docx` | python-docx（OOXML） | 交付 / 二次编辑 | 落**真正的 Word 标题样式**（Title / Heading / List Bullet），Word 能自动生成目录、导航窗格可跳转 |
| `.pdf` | ReportLab | 打印 / 归档 / 送审 | A4，中文字符 100% 保留，代码缩进保真 |

> docx 与 pdf **互补而非替代**：docx 是"活文档"（可编辑、可协作），
> pdf 是"定版归档"（可打印、可送审）。两者后端均为可选依赖，
> 缺失时 renderer 如实回报跳过原因，**不影响 Markdown 主产物**。

### 输入来源

除了输入主题去公网检索，系统还能直接吃下**你手上的已有资料**：

| 来源 | 支持 | 说明 |
|---|---|---|
| `.pdf` | ✅ | 数字版 PDF 提取，**按字号推断标题层级**（22pt→h1 / 16pt→h2），中文无损 |
| `.png` `.jpg` 等图片 | 需 OCR 后端 | 扫描件/图片需 `paddleocr` 或 `mineru`，未安装时明确告知而非静默失败 |
| `.md` `.txt` | ✅ | 编码嗅探（UTF-8 / GB18030 / GBK），中文不乱码 |

资料进入**知识库**后持久化，可反复按语义检索：

```bash
# 一条命令跑完：摄入 → 建库检索 → 接地写作 → 质检 → 排版 → 落盘
# 输入 Markdown 里第一行写主题，其余行写资料路径（相对/绝对皆可）
python run.py input.md --pipeline kb-docgen -o output/kb_doc.md
```

也可以只用库和检索这两层的能力：

```python
from artesian.knowledge_base import KnowledgeBase

kb = KnowledgeBase("knowledge_base.db")        # 默认 hash 嵌入（离线可用）
kb.add_file("财报.pdf")                        # 自动经摄入层转 Markdown
kb.search("本季度营收增长多少", top_k=3)        # 向量检索
```

**诚实边界**：内置 `hash` 嵌入是**特征哈希（词法相似）**，能找到"用词相近"
的内容，**不能**理解同义词与语义改写（"营收" vs "收入" 匹配不上）。
需要真语义请装 `sentence-transformers`（`local` 后端）或配 `EMBEDDING_API_KEY`
（`api` 后端）；两者不可用时 `auto` 会自动回落到 hash 并记录原因，不会失败。

---

## 特性

| 类别 | 能力 |
|------|------|
| **编排** | DAG 并行执行、断点续传、可视化执行计划、SQLite 任务队列恢复 |
| **组合** | `when` 条件节点（不成立就跳过、下游照跑）、`call` 子流水线内联（同一 Agent 可在一张图里出现多次）、`foreach` 逐项展开（按上游列表把同一个 Agent 跑 N 次：幂等键按项分裂、产物按声明聚合、**任一项失败即节点失败**） |
| **通用工作流** | 领域无关的 `http_request`（出网走 SSRF 校验、超限不截断、重定向不跟随）+ `transform`（声明式取数/过滤/模板，不执行表达式），出厂流水线 `api-report` 就是它们的消费者：换一种任务类型仍然成立 |
| **需求** | **requirements_analyzer 需求分析器**（输入 → 结构化 DocumentSpec：类型/范围/读者/深度，置信度评分 + 追问建议，`--pipeline docreq`） |
| **检索** | Bocha + Tavily + Serper + Metaso + Bing + Sogou + 360 等 10 引擎、LRU+TTL 跨任务缓存 |
| **抓取** | Async I/O（aiohttp 并发）/ 同步线程池降级、内容质量识别 |
| **写作** | TF-IDF 向量语义匹配、骨架生成、LLM 润色、质量反馈闭环 |
| **质量** | QualityGate v2（Profile 模板）、**产出保真底线**（空/占位文档直接判失败，不再 done+exit 0）、Style Enforcer、Citation Verifier、评分历史学习、**fact_checker 事实核查**（数字类声明 vs 检索源一致性，`--pipeline docgen-verified`） |
| **弹性** | 熔断器、限流器、Agent Pool、背压、自动重生成、告警机制 |
| **可观测** | 结构化日志（轮转）、Prometheus Metrics、Admin REST API、Dashboard、日志查询 |
| **成本** | LLM 调用成本追踪（16 供应商定价表）、预算熔断、`GET /api/cost` |
| **安全** | Agent 沙箱（AST 安全检查 + 白名单）、.env 明文密钥检测 |
| **运维** | 版本锁定（`--write-lock` 生成/刷新 pipelines/*.lock；运行时自动比对 version + config_hash，配置漂移即拒绝执行）、Schema 校验、基础鉴权、Docker 化、配置热更新、MCP Server |

---

## 快速开始

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

输入文件是一行主题描述，输出是结构化 Markdown 文档。

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

# 启动 Dashboard（默认 http://127.0.0.1:8910，入口 /index.html）
python run.py test_input.md --dashboard
```

### CLI 参数

| 参数 | 说明 |
|------|------|
| `input` | 输入文件（Markdown/文本） |
| `--pipeline, -p` | 流水线名称（默认 `docgen`） |
| `--queries, -q` | 检索词（可多个） |
| `--output, -o` | 输出文件路径 |
| `--resume` | 从断点续传（声明式与 legacy 两条路径都生效），配 `--task-id` 指定要续的任务；断点属另一条流水线时会被拒收并告警，而不是合错状态 |
| `--plan / --dry-run` | 仅预览计划，不执行 |
| `--admin / --dashboard` | 启动管理 API / 仪表盘 |
| `--daemon` | 执行完后保持 API 常驻 |
| `--mcp` | 启动 MCP Server（JSON-RPC over stdio，供外部 Agent 调度） |
| `--recover` | 启动时恢复中断的任务队列 |
| `--worker` | 常驻 worker：消费 `task_queue` 里的 pending 任务（claim → 执行 → 落状态）；配 `--once` 只消费一条，`--poll-interval` / `--idle-timeout` / `--lease-stale` 控制节奏与崩溃租约回收 |
| `--config, -c` | 自定义配置文件 |
| `--json-output` | 输出 JSON 结果（供 wrapper 解析） |
| `--legacy` | （已冻结，仅兜底）按 Agent 注册元数据执行，不经 Scheduler/YAML；生产请用默认 DAG 模式 |
| `--write-lock` | 为当前流水线生成/刷新 `pipelines/*.lock`（配置变更需显式重写锁，运行时比对 config_hash + 拓扑指纹） |
| `--check` | 启动自检后退出：如实报告 HTML 解析内核、LLM 供应商、依赖与目录结构。退出码只反映**结构性故障**——缺 LLM 凭据记 WARN 不记 ERROR，因此 `rc` 可被 CI 直接采信（历史上 CI 从不读这个 rc，只 grep 一行文案） |

> **执行路径说明**：默认走声明式 DAG（`pipelines/*.yaml` + Scheduler，含 lockfile 校验与 per-node 配置）。
> `--legacy` 是历史兜底路径，已冻结不再演进，两条路径的一致性由 `tests/test_dual_path_parity.py` 护栏。

---

## 架构

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
                 │   Checker   │  结构完整性分级质检（P0-P3）
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
| `pipeline_core/cache_manager.py` | 统一缓存（memory/file/multi 三级） |
| `pipeline_core/llm_router.py` | 多供应商 LLM 路由器（16 供应商定义，按 .env 启用） |
| `pipeline_core/cost_tracker.py` | LLM 成本追踪（16 供应商定价表 + 预算熔断） |
| `pipeline_core/alert_manager.py` | 告警机制（熔断/DLQ/限流/预算超限通知） |
| `pipeline_core/quality_feedback.py` | 质量评分历史 + 弱项模式分析 + 写作建议 |
| `pipeline_core/task_queue.py` | SQLite 持久化任务队列（原子 claim、租约回收、终态只写一次） |
| `pipeline_core/worker.py` | 常驻 worker：消费队列里的 pending 任务（`--worker`），多 worker 互斥、崩溃租约回收 |
| `pipeline_core/mcp_server.py` | MCP Server（JSON-RPC 2.0 over stdio，5 tools） |
| `pipeline_core/openapi_spec.py` | OpenAPI 3.0 规范生成 |
| `pipeline_core/agent_loader.py` | Agent 安全加载（AST 检查 + 白名单沙箱） |
| `docpipeline/renderer.py` | 渲染层：Markdown → docx（OOXML）/ pdf（ReportLab），双后端可选依赖 |
| `artesian`（独立库，自本仓 `pipeline_core/` 迁出） | `fast_json`（orjson 优先、stdlib 回退）+ HTML 解析内核兼容层（modest/lexbor 按可用性选，降级显式记录）+ 嵌入层（hash 内置 / local 模型 / API，auto 自动回落）+ 知识库（切块 → 向量化 → SQLite 持久化 → 向量检索）+ 统一搜索引擎接口（10 引擎 + mock 测试桩 + 自带 LRU+TTL 缓存） |
| `pipeline_core/state_paths.py` | 运行态路径解析（`DOC_PIPELINE_STATE_DIR` / `DOC_PIPELINE_VERSIONS_DIR`），测试与真实运行不再共用一份幂等键历史 |
| `docpipeline/ingest.py` | 摄入层：PDF/图片/文本 → 结构化 Markdown（PDF 按字号推断标题层级） |
| `docpipeline/document_enhancer.py` | 文档增强：对已有 Markdown 逐章节 LLM 深化 + ASCII 图修复 + 导出 |
| `agents/` | 14 个 Agent 实现（researcher/fetcher/writer/quality_gate/checker/fact_checker/layout/safe_writer/requirements_analyzer/renderer/ingest/knowledge_base/http_request/transform） |

---

## 配置

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
    - [safe_writer]
  edges:
    - [researcher, fetcher]
    - [fetcher, writer]
    # ...
```

### 条件节点（`when`）

节点可以声明执行条件；不成立时该节点被跳过，**下游照常执行**（这正是"可选分支"的含义）：

```yaml
agents:
  - name: renderer
    when:
      path: config.format            # 见下方可用路径
      op: "in"
      value: [docx, pdf]

  - name: fact_checker
    when:
      all:                           # 只允许一层组合
        - {path: upstream.quality_gate.overall_score, op: ">=", value: 60}
        - {path: artifacts.results, op: truthy}
```

求值上下文（`pipeline_core/conditions.py`，纯声明、不 eval）：

| 前缀 | 内容 | 例子 |
|------|------|------|
| `artifacts.*` | 上游按 `PRODUCES` 合并后的产物 | `artifacts.content` |
| `upstream.*` | 某个具体上游节点的原始结果 | `upstream.quality_gate.overall_score` |
| `config.*` | 本节点的节点级配置 | `config.format` |
| `inputs.*` | 调用方传给本子流水线的参数 | `inputs.min_score` |
| `pipeline` / `task.*` | 流水线名 / 任务 id 与输入文件 | `task.input_file` |

算子：`==` `!=` `<` `<=` `>` `>=` `in` `not in` `exists` `truthy` `falsy`。三条硬规矩：

- 写法非法（未知算子、缺 `value`、嵌套 `all/any`、单条件还套组合）在**解析期**报错；
- 数值比较要求两侧同型，`bool` 不与数字比较（Python 里 `True == 1` 会静默成立）；
- **路径取不到就抛错**，整条 run 失败——把它当"条件不成立"就会静默跳过分支却照报 done。
  只有 `exists` / `truthy` / `falsy` 允许路径缺失。

`when` 参与 `topology_hash`：给节点加条件会让既有 lockfile 报拓扑漂移，必须 `--write-lock`。
不写 `when` 的节点行为与之前完全一致（各内置流水线的 lockfile 指纹实测未变，`tests/test_condition_nodes.py` 逐条校验）。

右值也能从上下文取，这是子流水线参数化的前提：

```yaml
when:
  path: upstream.quality_gate.overall_score
  op: ">="
  value_from: inputs.min_score        # 阈值由调用方传，不抄死在片段里
```

`value` 与 `value_from` 必须恰好给一个。`inputs.*` 若没人提供，解析期就报错而不是等运行期：
`all:` 会短路，缺的参数可能还没被求值到就已经绕过了。

### 子流水线（`call`）

一个节点可以引用另一条流水线，解析期把它**内联展开**成普通节点，因此幂等键、检查点、
重试、熔断、`when`、产物契约一律照旧生效：

```yaml
agents:
  - name: writer
    dependencies: []
  - name: review                 # 节点身份，不是 Agent 名
    call: docgen-verified        # 被引用的流水线（pipelines/docgen-verified.yaml）
    dependencies: [writer]
    inputs:                      # 传给子流水线的参数，片段用 when 的 inputs.* 读
      fact_check: true
      min_score: 70
  - name: publish
    dependencies: [review]       # 自动接到子流水线的出口节点
```

- 内联进来的节点带别名：`checker` → `checker__review`，所以**同一个 Agent 可以在一张
  图里出现多次**而互不覆盖；`agent_of()` 仍还原成 `checker` 去查注册表。
- 环与深度在解析期拒绝：`a → b → a` 报"循环引用"，超过 3 层（含叶子）报"嵌套超过上限"。
- `call` 节点不接受 `config` / `when` / `pool_size` / `rate_limit` / `foreach` —— 配置属于
  子流水线，父子各写一份会变成两处真相；`foreach` 更是没有对象（展开后"本节点"不存在）。
- **被引子流水线改了，调用方的 lockfile 会报拓扑漂移**（展开后的图算指纹，
  `inputs` 的实参也算），必须 `--write-lock`。

参数（`inputs`）的作用域：

| 写在哪 | 含义 |
| --- | --- |
| `call` 节点 | 这次调用传给子流水线的实参 |
| 普通节点 | 自己的默认值，仅当它的 `when` 读到 `inputs.*` 才允许写 |

内联时按"节点默认值 ← 调用方实参"合并，且只把该节点确实会读的那几个键落到节点上；
更深一层的 `call` 有自己的作用域，外层参数不会灌进去。参数双向都在解析期查：
引用了没人传 → 报错（`all:` 会短路，缺参到运行期可能被静默绕过）；传了没人读 → 报错
（`min_scor: 70` 这类拼写错误原本会静默走默认值）。

出厂片段 `pipelines/_quality-tail.yaml`（质量尾：quality_gate → checker → 可选
fact_checker → layout → safe_writer）被 docgen / docgen-render / docgen-verified /
docgen-lean / docreq / kb-docgen 六条流水线引用；抽取前逐字节比对过这六份尾巴，
`quality_gate/checker/layout/safe_writer` 的 version/timeout/config 完全一致，
差异只有 fact_checker 的有无与门槛，于是收敛成两个参数：`fact_check: false`（docgen /
docgen-render / docreq / kb-docgen）、`{true, 0}`（docgen-verified，无条件核查）、
`{true, 70}`（docgen-lean，达标才核查）。

`_` 前缀 = 内部片段：不进 `--pipeline` 候选清单（`installed_pipelines` /
`list_pipelines` / run.py 三处口径一致），但仍能被 `call` 加载；片段必须与引用它的
YAML 同目录。它自带默认值，所以也能单独解析与加锁。版本锁定靠调用方的 lockfile：
片段的节点、连线、配置哈希与实参都进了父图的 `topology_hash` / `config_hash`。

### 逐项展开（`foreach`）

`when` 决定"这个节点跑不跑"，`foreach` 决定"这个节点跑几次"——按上游列表**逐项**调用
同一个 Agent，再把 N 份结果聚合成一份产物：

```yaml
agents:
  - name: http_request
    config:
      url: https://api.github.com/repos/apache/kafka/issues?state=open
      expect: json
  - name: digest
    dependencies: [http_request]
    foreach:
      over: artifacts.response    # 与 when 同一套点号路径；取不到值就报错
      max_items: 5                # 默认 64；超限报错而不是截断
    config:
      template: "- [#{{item.number}}] {{item.title}}（第 {{index}}/{{count}} 条）"
```

- 展开发生在**一次节点执行内部**：DAG 里仍是一个节点（不新增节点身份、不改层级），
  lockfile、检查点、重试、熔断都按节点粒度照旧。
- 每一项拿到三个引擎自有键：`item`（当前项）、`index`（序号）、`count`（总项数）。
  它们是引擎自有键，谁声明成同名产物都顶不掉——被顶掉之后每一项看到的是同一份数据，
  渲染出 N 份相同内容却一声不响。
- 每一项一个独立幂等键（后缀 `#{i}`）。共用前缀的话第 2 项起会命中第 1 项的缓存 ——
  表现是"展开了 N 次、只执行了 1 次"，而产物看起来是完整的。
- 限流按项领令牌：一次节点执行现在等于 N 次对外调用，只领一次等于把作者设的速率整倍放大。
- 聚合结果 `{status, count, items: [逐项原始结果], ...按 PRODUCES 聚合的产物}`。
  str 产物按行拼接、list 产物首尾相接、其余类型收成列表 —— 不给"取最后一项"这种
  覆盖语义，覆盖在逐项展开里等于"只剩第 N 项"。
- **任一项失败 ⇒ 整个节点失败**（含订阅者返回 None 的超时项）；`hard_floor` 逐项累积。
  零项展开、非列表、`over` 取不到、超过 `max_items` 都是显式错误：静默少跑比失败难查。
- 不与 `pool_size>1` / `call` / 质量重做（`REGENERATION_*`）/ 落盘（`WRITES_OUTPUT`）同用。
  前两者的展开分配语义没有唯一答案，后两者的操作对象是"整份产物"，逐项展开里没这个东西；
  要落盘就把落盘节点排在 foreach 之后，让它拿聚合产物。
- 展开契约（`over` 与 `max_items`）进 `topology_hash`：改了必须 `--write-lock`，否则运行时
  报拓扑漂移。锁的是**声明**而不是运行期解析出的项数——项数来自数据，每次跑都可能不同。

出厂消费者 `pipelines/api-digest.yaml`（HTTP 列表接口 → `transform` 逐项渲染 → `safe_writer`
落盘），判据与回归见 `tests/test_foreach.py`（含离线跑通整条 Scheduler → DAGExecutor 链的
真跑用例）。

### 通用 Agent（`http_request` / `transform`）

引擎不止能写文档：这两个 Agent 不带任何领域语义，配起来就是一条"取数据 → 变形 → 落盘"
的通用工作流，出厂流水线 `pipelines/api-report.yaml` 就是它们的真实消费者（也是接线判据，
`tests/test_generic_agents.py` 离线跑通整条 Scheduler → DAGExecutor → safe_writer 链并断言落盘产物）。

`http_request`：出网取一个接口。

| 配置 | 默认 | 说明 |
|------|------|------|
| `url` | 必填 | 也可留空、由上游产物给（`payload["url"]`）；只接受 `http/https`，指向内网/回环/链路本地地址默认**拒绝** |
| `method` | `GET` | `GET/POST/PUT/PATCH/DELETE/HEAD`，其余该节点直接失败 |
| `headers` | `{}` | 原样发出；进产物的是**响应**头，且 `authorization`/`token`/`secret`/`cookie`/`api-key` 一类值恒为 `***` |
| `body_artifact` | `""` | 用哪个上游产物当 JSON 请求体；点名了却不在上游产物里 → 节点失败 |
| `expect` | `json` | `json` 解析失败即节点失败（错误里带响应前 500 字符）；`text` 原样传下游 |
| `timeout_s` | `15` | 单次请求超时 |
| `max_bytes` | `200000` | 超限**中止并报错**，不截断——残缺 JSON 会让下游拿到看似合法的坏数据 |
| `allow_hosts` | `[]` | 内网目标的显式白名单（比对 hostname **全等**，不做前缀匹配）；命中时产物带 `guard: "allowlist"`，不静默放行 |

重定向恒不跟随（`allow_redirects=False`，不是配置项）：3xx 只回 `redirect_to` 就返回，
且不进解析——重定向响应体常为空，让"不是合法 JSON"抢先判死，作者就看不到 Location 了。

`transform`：声明式的胶水，替代"为每个接口写一个 Agent"。

```yaml
- name: transform
  dependencies: [http_request]
  config:
    items: artifacts.response.items       # 给了就按列表逐项处理；不给只渲染一次
    fields: [name, id]                    # 每项挑哪些字段（缺字段 → 节点失败，不静默丢）
    where: {path: item.enabled, op: truthy}  # 逐项过滤；与 when 同一套受限语言
    set:                                  # 派生值：{name, from}
      - {name: total, from: "len(items)"}
    template: |                           # 渲染一次，不是逐 item 渲染
      # 共 {{len(items)}} 条
      - 首条：{{items.0.name}}（{{items.0.id}}）
```

路径与 `when` 同源（`conditions.resolve`），因此支持列表下标：`artifacts.items.0.id`。
`where` 与 `fields` 都要求给 `items`——没有列表时它们无物可作用，而这属于"写了没人读"，
一律报错而不是静默出厂全量。
模板只认 `{{路径}}` 与 `{{len(路径)}}`（`len` 是唯一被放行的函数：想要别的运算就该用条件
或换 Agent，在配置里塞表达式求值器等于把数据通道变成代码通道），取不到值就判失败而不是
渲染成空字符串——空产物比报错难查得多。产物键：`data`（结构化，逐项处理时是列表）、
`text` / `content`（渲染结果，同一份，方便直接接 `safe_writer`/质检那批按 `content` 取正文的节点）。

### Quality Profile（`pipelines/quality/`）

```yaml
# technical-doc.yaml
name: technical-doc
threshold: 70
# 权重维度与 quality_gate._score_all 的产出一一对应（总和 = 1.0）
weights:
  completeness: 0.18
  structure: 0.12
  readability: 0.10
  citation: 0.12
  depth: 0.10
  substance: 0.18
  topic_relevance: 0.20
style_rules:
  - id: broken_links
    pattern: '\[([^\]]*)\]\(\s*\)'
    penalty: 15
    message: 空链接/危险协议
citation:
  enabled: true
  penalty_per_issue: 5
  check_url_format: true
```

权重只在 Quality Profile 一处定义;style 不作为评分维度——风格问题按
`style_rules` 以扣分形式计入（与 citation 扣分共享 `max_penalty` 上限）。

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

## 管理 API

启动：`python run.py <input> --admin` （默认 `http://127.0.0.1:8910`）

| 端点 | 方法 | 说明 |
|------|------|------|
| **任务管理** | | |
| `/tasks` | GET | 任务列表 |
| `/tasks/<id>` | GET | 单任务详情（含 result/output_content/output_path） |
| `/tasks/<id>/cancel` | POST | 取消任务 |
| `/tasks/<id>/rerun` | POST | 重跑流水线（复用 last plan） |
| `/api/tasks` | POST | 提交新任务（同步/异步，外部 Agent 调度入口） |
| **Agent 管理** | | |
| `/agents` | GET | 已注册 Agent 列表 |
| `/api/agents/` | GET | Agent 详情（含 stats/meta/熔断器状态） |
| **配置与成本** | | |
| `/api/config` | GET | 运行时配置快照 |
| `/api/config/reload` | POST | 配置热更新（通知所有 Agent on_config_update） |
| `/api/cost` | GET | LLM 成本统计（按供应商/Agent/时间维度） |
| `/api/cost/budget` | POST | 设置预算上限（超限自动熔断） |
| **质量与日志** | | |
| `/api/quality/feedback` | GET | 质量评分历史 + 弱项模式分析 |
| `/api/logs` | GET | 结构化日志查询（level/agent/since/limit 过滤） |
| `/api/alerts` | GET | 告警历史查询（level/category/limit 过滤） |
| **版本管理** | | |
| `/api/versions` | GET | 文件版本历史 |
| `/api/versions/diff` | GET | 对比两版本差异 |
| `/api/versions/rollback` | POST | 回滚到指定版本 |
| `/api/versions/stats` | GET | 版本管理统计 |
| **死信队列** | | |
| `/dlq` | GET | 死信队列列表 |
| `/dlq/<id>/replay` | POST | 重放死信 |
| **事件钩子** | | |
| `/api/events/hooks` | GET | 列出已注册事件钩子 |
| `/api/events/hooks` | POST | 注册事件钩子（SSRF 防护，拒绝私有 IP） |
| `/api/events/hooks/<id>` | DELETE | 注销事件钩子 |
| **缓存** | | |
| `/api/cache` | GET | 缓存统计 |
| `/api/cache/clear` | POST | 清空所有缓存 |
| **快捷操作** | | |
| `/cancel` | POST | 取消任务（body 传 task_id） |
| `/pause` | POST | 暂停任务 |
| `/resume` | POST | 恢复暂停的任务 |
| `/replay` | POST | 重放死信（body 传 dlq_id） |
| `/rerun` | POST | 重跑流水线（body 传 task_id） |
| **健康与指标** | | |
| `/health` | GET | 总线 + Registry 健康状态（免鉴权） |
| `/api/health/deep` | GET | 全组件深度健康检查 |
| `/metrics` | GET | Prometheus 格式指标 |
| `/api/pipeline` | GET | 流水线配置信息 |
| **流式与规范** | | |
| `/stream` | GET | SSE 流式推送文档生成进度（支持 Last-Event-ID 重连） |
| `/stream/metrics` | GET | 流式指标快照（JSON） |
| `/api/openapi.json` | GET | OpenAPI 3.0 规范 |
| `/api/dashboard` | GET | Dashboard 数据 |
| **静态资源** | | |
| `/` | GET | API 纯文本索引（**非仪表盘**） |
| `/index.html` | GET | 静态仪表盘入口（若 `--dashboard` 启动） |

### 鉴权

通过环境变量 `ADMIN_API_KEY` 或 `AdminAPI(api_key=...)` 启用：

```bash
export ADMIN_API_KEY="your-secret-key"
python run.py input.md --admin
```

客户端请求携带 `Authorization: Bearer <key>` 或 `?token=<key>`（仅 SSE 场景才建议 query token，
常规请用 Authorization 头，避免凭证落入访问日志）。
未配置时禁用鉴权（仅本机访问）。`/health` 与静态资源始终免鉴权。

### MCP Server

通过 `--mcp` 启动 JSON-RPC 2.0 over stdio，供外部 Agent（如 Claude Desktop）调度：

```bash
python run.py --mcp
```

提供 5 个 tools：`generate_document`、`get_task`、`list_tasks`、`list_pipelines`、`get_pipeline_info`。

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

## Docker

> **当前前置条件（2026-10-07 实测）**：`docker build` 依赖 `pip install -r requirements.txt`
> 能装到独立库 `artesian`（见「核心模块」表末行）。它尚未发布 PyPI、也还没推远端，
> 因此现在构建出的镜像会在容器启动时 `ModuleNotFoundError: artesian`——
> `pipeline_core/__init__.py` 是**导入期**就要它（挡住 artesian 后 `import pipeline_core`
> 立刻报 `No module named 'artesian.fast_json'`，本机实测）。requirements.txt 里补上
> 可安装的 pin（`artesian @ git+https://…@<sha>` 或发布后的版本号）之后，本节命令才成立。

```bash
# 构建并启动常驻 Admin API 服务（生产配置，绑定 0.0.0.0:8910；
# 非回环绑定强制要求 ADMIN_API_KEY，缺失时容器会拒绝启动并提示）
docker build -t doc-pipeline .
docker run -d -p 8910:8910 -e ADMIN_API_KEY=change-me \
  -v $(pwd)/checkpoints:/app/checkpoints doc-pipeline

# 提交任务：POST /api/tasks（鉴权头 Authorization: Bearer <ADMIN_API_KEY>）
# 一次性生成文档（覆盖默认 CMD）：
docker run --rm -v $(pwd)/output:/app/output doc-pipeline \
  test_input.md -o output/doc.md
```

非 root 用户运行，暴露 8910 端口，`/app/checkpoints` 与 `/app/logs` 为持久化卷；
HEALTHCHECK 直接探测容器内 `/health`（免鉴权）。

---

## 测试

```bash
python -m pytest tests/ -v
```

**2104 个测试本机全绿**（`2104 passed, 1 skipped, 6 deselected`，2026-10-07 本机全量实测；
工具层迁出 artesian 后 19 条 fast_json 用例随库走，等价判据在新库加强至 35 条；
嵌入层与知识库迁出后 74 条用例随库走，等价判据在新库加强至 91 条；
搜索引擎迁出后 91 条用例随库走，等价判据在新库加强至 92 条，另配 30 条缓存/env 底座判据；
现测命令 `python -m pytest tests/ -q`；coverage 门禁 83%，
本轮实测 87.90%）。CI 的通过数可能与本机略有
出入——渲染层/OCR/嵌入类用例带 `skipif`，取决于该 job 装了哪些可选依赖。
数字由 `tests/test_doc_consistency.py` 与实际收集数比对把关，落后于代码即红（此前这里
长期写 1854 而无人能证明它对不对，因为 `tests/` 里没有一条测试引用 README）。覆盖：Scheduler 解析、
Schema 校验、Lockfile 与 edges 一致性、消息总线（含幂等去重的显式回报）、
熔断器、限流器（含集成）、QualityGate（含产出保真底线）、Agent 集成、
容错注入、断点续传、管理 API、并发压力、SSE 流式、执行器工厂、任务队列、
成本追踪、告警机制、质量闭环、MCP Server、OpenAPI Spec、Agent 沙箱、
配置热更新、kb-docgen 离线端到端、运行态路径隔离。

> `python -m pytest tests/ -m e2e` 需要真实网络与 LLM Key；CI 未配 Secret 时
> 这些用例会**全部 skip**（历史上 E2E Nightly 因此"绿而未跑"），
> 离线端到端能力由 `tests/test_kb_pipeline_wiring.py` 与
> `tests/test_e2e_mock.py` 承担。

```bash
# 运行真实端到端测试（需要网络 + LLM API Key）
python -m pytest tests/ -m e2e -v
```

---

## 质量门控机制

`QualityGate v2` 按 Profile 权重评分：

```
产出保真底线（先于评分）：内容非空 且 ≥ min_output_chars（默认 120）
                     且 不含已知占位语（"未采集到可整合的搜索结果"等）
                     且 占位章节占比 ≤ max_placeholder_section_ratio（默认 0.34）
                     且 正文不含抓取层原始素材签名（"下载时间:" 行 / 60 连等号分隔线；
                         确需附原文用 allow_raw_fetch_blocks: true 显式放行）
                     → 违反即 status=fail + hard_floor=true，
                       不重做、不因 pipeline.fail_fast=false 而放行，整条流水线 exit 1

总分 = Σ(维度得分 × 权重) − 风格扣分 − 引用扣分
阈值 = profile.threshold (默认 70)

不达标 → 自动重生成 (最多 max_regenerations=3 次)
重做后仍不达标 → accepted_with_warnings（放行但如实标注）
```

底线与评分的分工：评分量的是"写得好不好"，可以被扣分拉低后仍放行；
底线量的是"到底有没有内容"。**历史上没有底线**——无检索结果时 writer
会写一份 99 字节的占位文档，一路 9 步全绿、`done` + `exit 0` 并落盘
docx/pdf，把失败伪装成成功（`tests/test_fidelity_gate.py` 锁住该回归）。

### 维度

评分共 7 个维度（`technical-doc` 权重）：主题相关度 topic_relevance 0.20、
完整性 completeness 0.18、内容实质度 substance 0.18（信息密度/重复率/实质信号）、
结构 structure 0.12、引用 citation 0.12、可读性 readability 0.10、深度 depth 0.10。
风格问题不计分，按 profile 的 `style_rules` 扣分（与引用扣分共享 `max_penalty=40` 上限）。

> 诚实边界：以上维度均为规则/启发式度量（结构、字数、关键词命中、URL 格式等），
> 不构成事实正确性验证——事实核查请使用 `--pipeline docgen-verified` 的 fact_checker
> （其启发式边界见 `agents/fact_checker.py` 模块注释）。

---

## 目录结构

```
doc-pipeline/
├── agents/              # 14 个 Agent 实现
├── pipeline_core/       # 引擎层：领域无关的编排框架（不 import 文档层）
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
│   ├── cache_manager.py # 统一缓存
│   ├── llm_router.py    # LLM 多供应商路由
│   ├── cost_tracker.py  # LLM 成本追踪 + 预算熔断
│   ├── alert_manager.py # 告警机制
│   ├── quality_feedback.py # 质量闭环学习
│   ├── task_queue.py    # SQLite 任务队列
│   ├── worker.py        # 常驻 worker（--worker）：消费队列、租约回收
│   ├── mcp_server.py    # MCP Server (JSON-RPC)
│   ├── openapi_spec.py  # OpenAPI 3.0 规范
│   ├── agent_loader.py  # Agent 安全加载（沙箱）
│   ├── streaming.py     # SSE 流式输出
│   └── ...              # 更多模块
├── docpipeline/         # 文档领域层（只依赖 pipeline_core，方向单向）
│   ├── renderer.py      # Markdown → docx / pdf
│   ├── ingest.py        # PDF / 图片 / 文本 → 结构化 Markdown
│   └── document_enhancer.py # 已有文档逐章节 LLM 增强
├── pipelines/           # Pipeline 定义 + Quality Profile
│   ├── _quality-tail.yaml # 内部片段：质量尾（六条流水线共用，不进 --pipeline 候选）
│   ├── docgen.yaml      # 默认文档生成流水线（尾巴 call _quality-tail，不核查）
│   ├── docgen-render.yaml # 追加 renderer 节点，产出 docx/pdf
│   ├── docgen-verified.yaml
│   ├── docgen-lean.yaml # 条件升级核查：质量达标才跑 fact_checker
│   ├── docreq.yaml      # 需求分析增强（requirements_analyzer 开头）
│   ├── kb-docgen.yaml   # 本地资料 → 知识库接地（ingest/kb/writer 已接线）
│   ├── api-report.yaml  # 通用件示例：JSON API → transform 挑字段 → 落盘（无文档领域节点）
│   ├── api-digest.yaml  # foreach 消费者：列表接口 → 同一个 Agent 逐项渲染 → 聚合落盘
│   ├── three_pass.yaml  # 三阶段流水线（尾巴阈值/重试与片段不同，故不引用片段）
│   ├── test_pipeline.yaml
│   └── *.lock           # 版本锁定：config_hash + 拓扑指纹，漂移即拒绝执行
│   └── quality/
│       ├── technical-doc.yaml
│       └── tutorial.yaml
├── dashboard/           # 前端仪表盘
├── spike/               # 渲染层可行性验证脚本与结论（见 spike/README.md）
├── tests/               # 测试套件（数量见上文「测试」一节；另有 e2e 标记用例）
├── checkpoints/         # 断点 + 日志（自动轮转）
├── versions/            # 文档版本存储
├── run.py               # CLI 入口
├── Dockerfile
├── requirements.txt
└── .github/workflows/ci.yml
```

---

## 性能

> **注意**：以下数据来自 `benchmark.py` 的 mock 基准（模拟引擎，无网络 I/O，质量门控跳过 LLM），
> 反映框架本身的开销。默认 `config.json` 已启用 Bing/Sogou/360 免费 HTML 兜底引擎（无需 API Key，
> 结果质量低于付费 API 引擎）；完整多引擎检索 + LLM 润色请使用：
> `python run.py input.md -c config.production.json -o out.md`

| 指标 | 数值 | 模式 |
|------|------|------|
| Fetcher 并发 | 20 页 / 3s (aiohttp) | 真实网络 |
| LLM 额度消耗 | 0（质量门控跳过，规则兜底） | mock |
| 消息总线吞吐 | 批量 drain 50 条/轮 | — |
| 缓存命中 | 74 万 ops/s (get_hit) | 基线（ubuntu/3.12） |
| 测试覆盖 | 见「测试」一节的现测数字（另有 e2e 标记用例） | — |

### 生产模式预期耗时（config.production.json）

| 阶段 | 预期耗时 | 说明 |
|------|----------|------|
| Researcher（真实搜索） | 3-8s | 取决于引擎响应速度 |
| Fetcher（20 页下载） | 2-4s | aiohttp 并发 |
| Writer（TF-IDF + LLM 润色） | 5-30s | LLM 调用为主要耗时 |
| QualityGate + Checker + Layout | <1s | 纯规则计算 |
| **总计** | **10-45s** | 视 LLM 可用性和网络状况 |

---

## 文档

| 文档 | 内容 |
|------|------|
| [部署指南](docs/deployment.md) | 生产环境配置、API Key、监控、备份策略 |
| [API 参考](docs/api.md) | Admin REST API 全端点说明与鉴权 |
| [架构说明](docs/architecture.md) | 分层结构、线程模型、数据流 |
| [Agent 开发指南](docs/agents.md) | 自定义 Agent 契约、沙箱规则、最小示例 |

---

## License

MIT
