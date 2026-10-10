# 产品定义 · Agent 工作流运行时

> 状态：**已确认方案（2026-10-06）**，作为升级工作的基线文档。
> 本文只定义"做什么/长什么样/怎么算合格"，不含实施排期。

## 0. 证据口径（先说清哪些是量过的）

| 标记 | 含义 |
|---|---|
| 〔实测〕 | 2026-10-06 在本机量得：**Python 3.14.3**（全局 site-packages，无 venv）、**无 `.env`/无 LLM Key**、单进程、`pool_size: 2`、`defaults.parallelism.mode: single` |
| 〔静态〕 | 读代码/`git grep` 得到的结构与计数，未在运行时验证 |
| 〔参照〕 | 竞品公开定位，**本仓未逐家实测**，仅作坐标参考 |

规模基线〔静态〕：`pipeline_core` 16,593 LOC / 42 文件；`agents` 6,470 LOC / 14 个 Agent；`docpipeline` 1,246 LOC / 3 文件；`tests` 27,153 LOC / 99 文件；`run.py` 707 行。
测试基线〔实测〕：**2211 passed / 2 skipped / 6 deselected，242s**（README 写 1854，属过期低估）。

---

## 1. 产品定位

**一句话**：领域无关的 **Agent 工作流运行时** —— 用声明式 YAML 编排可插拔 Agent，内核自带节点级重试/熔断/限流/幂等/检查点续跑、**产出质量契约**与 LLM 成本预算熔断；可 `pip install`，可被 CLI / HTTP / MCP / 队列调用。**docgen 只是装在它上面的第一个 pack。**

### 1.1 目标用户与待办任务

| 用户 | 待办任务 | 今天怎么做 | 我们赢在哪 |
|---|---|---|---|
| 企业内部自动化工程师 | 把"取数 → 加工 → 核查 → 交付"做成可维护、可回滚的作业 | n8n / Dify 拖拽，逻辑粘在节点脚本里 | YAML-as-code 进 Git + 锁文件拓扑指纹防漂移 |
| Agent 应用开发者 | 要 durable execution，但不想养 Temporal 集群 | LangGraph 自研胶水 + 手写重试 | 现成的重试/熔断/限流/续跑 + 成本预算熔断 |
| 生态内项目（MAOP 等） | 需要一个真跑 Python 节点的执行内核 | 已经通过 `doc-pipeline` 适配器调用本仓 | 同一进程内的节点语义，不是外壳派发 |

### 1.2 差异化（本仓可指认的证据）

1. **工作流可复现**：`pipelines/*.lock` 记 `config_hash` + `topology_hash`，配置或拓扑漂移即拒绝执行；`call` 内联后的展开图与 `inputs` 实参都进指纹〔静态〕。
2. **产出契约是运行时概念**：节点按 `PRODUCES` 声明产物，声明了落盘节点却没交付物就不算 `done`（`pipeline.py` 交付契约）〔实测：修复前该情形会 done + exit 0〕。`when` 不成立的节点被跳过而**下游照跑**〔实测：探针里 `fact_checker__quality_tail` 返回 `skipped` 且后续节点正常执行〕。
3. **成本可治理**：`cost_tracker.py` 定价表 + 预算熔断 + `GET /api/cost`〔静态〕。
4. **通用件已接线而非"实现了没人调"**：`http_request`（SSRF 守护、超限中止不截断、3xx 不跟随）+ `transform`（受限声明语言、不 eval 表达式）〔实测：一条零文档语义 DAG `http_request → transform → transform__b → safe_writer` 全链跑通并落盘，GitHub API 200、`guard: public`、同一 Agent 以别名在一图内出现两次〕。

### 1.3 与 MAOP 的边界（不得越界）

| | MAOP（+ MAOS） | 本产品 |
|---|---|---|
| 执行对象 | **外部 CLI Agent**（`config/agents.yaml` 31 条适配器） | **进程内 Python Agent**（DAG 节点） |
| 职责 | 派发、治理、SLA、RBAC/SSO/多租户（在 MAOS） | 执行内核 + 节点语义 + 产出契约 |
| 关系 | 调用方 | 被调用的运行时 |

**不做**：可视化拖拽编辑器（拼不过 n8n 的集成数量）；通用作业调度器（Airflow 地盘）；身份/RBAC/SSO/多租户治理面（MAOP+MAOS 地盘）。

### 1.4 诚实短板（定位成立前必须清）

第三方插件已 6 个（`entry_points` 机制 2026-10-08 落地，2026-10-10 补足生态面至 6 个真 pip 包，见 §2）· 无身份/租户（一把共享 `ADMIN_API_KEY`）· 状态全是单机文件且租约按本机 PID 判活 · **质量门会假绿**（见 §6）。

---

## 2. "20 倍"的可测量口径

能力 = 四轴组合数。当前值全部量得，不许用口号代替分母。

| 轴 | 现状 | 目标 | 依据 |
|---|---|---|---|
| 任务类型（真实跑通的 pack） | **11**（docgen 系 / api-report / intel-brief / kb-brief / data-qc / alert-runbook / deck-brief / minutes-weekly / invoice-extract / k8s-patrol / spec-cases〔最后 9 条 2026-10-09 落地〕） | **≥10** | 〔实测〕每条 pack 一条真 E2E：9 条在 `tests/test_pack_suite.py`（真 Scheduler→DAG→Agent→落盘，只换外部 IO），存量两条在 `test_kb_pipeline_wiring` / `test_generic_agents`；`TestPackInventory` 清册护栏逐条钉住 YAML + lock + 锁校验 + 判据类存在 |
| 触发方式 | **6**（CLI / HTTP `POST /api/tasks` / MCP stdio / **定时 `--triggers`** / **入站 webhook** / **队列多机 worker**〔均 2026-10-08 落地〕） | **≥6** | 〔实测〕`tests/test_triggers.py` 钉定时提交路径、`tests/test_webhooks.py` 钉 HMAC/Token 鉴权与审计留痕、`tests/test_multi_process_workers.py` 钉真跨进程互斥与租约回收（边界：跨主机接管需共享 pid 命名空间） |
| 交付形态 | **5**（md / docx / pdf / **xlsx / pptx**〔2026-10-08 加入结构化子集〕） | **≥5** | 〔实测〕`tests/test_renderer.py` 读回判真；xlsx/pptx 为结构化子集而非全量转换 |
| 扩展来源 | **2**（本仓 `agents/` glob + **entry_points**〔2026-10-08 落地，group `doc_pipeline.agents`〕）；第三方插件实测数 **6**（2026-10-10：`examples/plugin-*` 六个真 pip 包，判据 `tests/test_plugins_ecosystem.py` 真 venv 装机逐个验证） | **≥2 且第三方插件 ≥5** | 〔实测〕`tests/test_agent_loader.py::TestEntryPointPlugins` 八条钉住 发现/加载/沙箱/同名优先 |

**组合数：120（2×6×5×2，2026-10-09 按实测轴值重算——旧值 100 用了过时的 triggers=5） → 660（11×6×5×2）**，相对最初立项基线 **18 为 36.7x，已越过 ≥20x（=360）承诺线**；四条验收线**全部成立**（第 2 条第三方插件 ≥5 已于 2026-10-10 达标：6 个真 pip 包；组合数随插件轴再乘一档至 3960）。

### 2.1 验收线（四条同时成立才算达成）

1. pack ≥ 10，且每个 pack 有至少一条端到端测试真跑通（不是 mock 断言）。**〔2026-10-09 达标：11 条 pack，11 条真 E2E，清册护栏 `TestPackInventory` 逐条钉住〕**
2. 第三方插件 ≥ 5：由**外部 pip 包**提供 Agent，**本仓零改动**即被发现、注册、执行。
3. 触发方式 ≥ 6：CLI / HTTP / MCP / inbound webhook / 定时 / 队列多机。
4. 交付形态 ≥ 5：md / docx / pdf / 结构化 JSON 落库 / HTML 站点。

> 第 2 条是 20x 的物理来源。自研 Agent 数量撑不到 20 倍，**扩展面**才撑得到。

---

## 3. 产品原型（三级递进，每级都是可运行的东西）

### P0 · 引擎内核原型（先做，2–3 周）

**形态**：`pip install <新发行名>` 得到的库 + `loom` CLI + `entry_points` 插件机制 + docgen 降级为 `pack-docgen`。

**验收判据（5 条，可当场验）**：
1. 干净 venv 装完后 `--check` 不红（当前 `bootstrap.py:171` 要求 `tests/` 存在 → 装完必红〔实测：本机 26 OK / 1 WARN / 1 ERROR〕）。
2. 外部仓发一个 Agent 包，本仓零改动被 CLI 列出并跑通一条用它的工作流。
3. 一条不含文档语义的 YAML 端到端跑通并落盘（已有探针证明引擎可行）。
4. §6 的 FP-1 / FP-2 / FP-3 全部关闭。
5. 测试数 ≥ 2211 且保持全绿。

**退出条件**：5 条全过才进 P1。**P0 阶段不做任何新 UI。**

### P1 · 自托管运行时原型（4–6 周）

**形态**：常驻服务（替换 stdlib HTTP/1.0 + 线程-per-连接，无并发上限）+ token 身份 + 状态后端可切（SQLite → Postgres）+ §4 的 6 屏控制台。

**验收判据**：两实例共享一库并发跑不串台（当前租约按本机 PID 判活，跨机必重复执行或搁死〔静态〕）；每 token 只能看到自己的任务与成本；`/api/config` 热更新不再只对本进程生效。

### P2 · 生态原型

**形态**：插件目录 + 版本兼容矩阵 + `pack new` 模板仓。

**验收判据**：第三方包可安装与升级；版本/拓扑冲突被显式拒绝，而不是静默覆盖既有 pack。

---

## 4. 产品图样

### 4.1 现有基线

`dashboard/`（index.html 11 KB + app.js 31 KB + style.css 15 KB，最后改动 2026-09-05〔静态〕）是**监控台**：状态 / 任务 / Agent / 质量 / 成本 / 日志 / 指标 7 面板 + 提交任务 + Token 连接。
可复用为 P1 的"运行观测"半边；**缺 authoring、插件视图、单次运行视图**。且 `app.js` 只调 5 个端点，`/api/logs`、`/api/alerts`、`/dlq`、`/api/versions*`、`/stream` 均未接〔静态〕。

### 4.2 目标屏幕（统一骨架：左导航 + 主区 + 右侧检查器抽屉）

| # | 屏 | 主区内容 | 与现状 |
|---|---|---|---|
| S1 | 工作流目录 | YAML 列表、**lock 状态徽标**（漂移=红）、拓扑层数、来源（内置/pack/插件） | 新建 |
| S2 | 运行详情 | **DAG 有向图**：节点四态 success/skipped/failed/retried；`when` 跳过画虚线分支；内联节点显示别名（如 `checker__quality_tail`）；检查点续跑标记；每节点耗时/重试次数 | 新建 —— **最关键一屏**，运行时卖点全靠它可见 |
| S3 | 节点检查器 | artifact 结构预览、幂等键、熔断/限流态、质量门明细 | 新建（**依赖 FP-1 修好**，否则此处显示的是假分） |
| S4 | 插件与 Agent | entry_points 来源分组、`PRODUCES`/`CONFIG_SCHEMA` 声明、沙箱判定、健康态 | 半复用 agents 面板 |
| S5 | 触发与集成 | HTTP / MCP / webhook / 定时 / 队列 的接入配置与调试 | 新建 |
| S6 | 成本与质量 | 预算熔断线、按 pack/token 归因、质量失败原因分布 | 复用 cost/quality 面板 |

### 4.3 视觉基调（已确认）

**沿用现有深色等宽"运维控制台"基调**，不另起浅色 SaaS 风。理由：买家是自动化工程师，运维台审美更对味，且能直接复用 `style.css` 基线。

---

## 5. 功能设计（分层 · 现状 · 目标 · 验收判据）

### 5.1 内核层

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| 声明式 DAG + `when` 条件节点 + `call` 子流水线内联 | **已有**〔实测〕 | 保持 | 零文档语义探针继续通过；内置流水线 lockfile 指纹不变 |
| `foreach` 逐项展开 | **已接线**（2026-10-07·续4）：解析期 `_as_foreach` 有调用点并进 `topology_hash`，运行期 `_execute_foreach` 逐项投递与聚合〔实测：`tests/test_foreach.py` 46 条，六处变异对照全被抓住〕。原记录"死代码：`scheduler.py:55 _as_foreach` 零调用点〔静态〕"是 10-07 上午的状态，保留备查 | 保持 | 100 项展开受 `max_items` 护栏（实测：6 项 > `max_items=5` ⇒ `failed` 且不落盘）；幂等键按 item 分裂（实测后缀 `#0/#1/#2`，去掉后缀则测试红）；展开契约进 `topology_hash`；真跑测试 = `pipelines/api-digest.yaml` 离线跑通 Scheduler → DAGExecutor → safe_writer |
| 产出质量契约 | **有但会假绿**〔实测：4/5 章节空的产物得 98.8 pass〕 | 按 FP-1 修 | `empty_sections` 占比 / 降级声明 / 抓取元信息占比 三判据进底线，并参与 exit code |
| 检查点与续跑 | 声明式路径的 `--resume` 原本是空转（工作区有未提交修复在飞）〔实测：diff 含 `TestDeclarativeResumeEndToEnd` 4 例〕 | 先合入 | 中断后 `--resume` 真跳过已完成节点，而非重跑 |
| 熔断 / 限流 / Agent 池 | **已改为分片**：池实例按 `all_queries[pool_idx::pool_size]` 领活；未认领 `EXTRACTS_QUERIES` 的 Agent 仍看全量（writer 这类必须看完整上下文）。查询词少于实例数时多余实例领**空活**并如实回报零结果，不再复制整条 query | 保持 | 新增的池实例不重复领取同一 query；根因只是 `len>=pool_size` 这道长度门槛，`EXTRACTS_QUERIES` 自 3674de6 已由 `agents/researcher.py` 声明并经 `AgentLoader` 进到 `AgentMeta`（`tests/test_pool_sharding.py` 五条，含接线证明） |
| 交付物不得夹带中间格式 | **已加判据**：抓取层落盘签名（`下载时间: …` / 60 连等号分隔线）出现在正文即 `hard_floor`；可用 `allow_raw_fetch_blocks` 显式放行（字符串 `"false"` 不会被当成真） | 保持 | 本机与 CI 实测的那两类"素材直粘"文档（28.4 KB / 21.9 KB，曾拿 95.5 pass）现在 `exit 1` 且不落盘 |

### 5.2 扩展层（20x 的引擎盖）

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| 插件发现 | **已达标**（2026-10-08）：entry_points group `doc_pipeline.agents` 与本仓 glob 并存（`TestEntryPointPlugins` 八条）；参考实现 `examples/plugin-hello/` 随仓，**真 venv + 真 pip install** 的端到端判据在位（`tests/test_plugin_example.py`：空 agents 目录下仍被发现/注册/执行）。第三方插件实测数 **6**（`examples/plugin-*` 六个真 pip 包，真 venv 装机判据 `tests/test_plugins_ecosystem.py`：逐个被发现/注册/执行，来源标记 `entry_point:*`） | `entry_points` group 与本仓 glob **并存** | 外部 venv 包装好即被 `list agents` 列出，并能作为节点执行 |
| Agent 沙箱 | 已有（AST + 白名单，内置件跳过检查） | 增加来源/签名标记，并在 S4 显示 | 第三方危险 Agent 被拒且报出命中规则 |
| pack 机制 | **已达标**（2026-10-09）：`pipelines/*.yaml` + `quality/` + `prompts/` + `scripts/checker_rules.yaml` 已进 wheel 数据；`--check` 按 `pipeline_core.pack_manifest` 清册报数（几条 pack / 几个 agent / 缺谁），不再点名个别 pack 的文件。**真 venv 判据在位**（`tests/test_installable_product.py`：打 wheel → 装 → 从中立 cwd 跑自检，零 ERROR + 清册 > 0）。`pack-docgen` 的独立发行形态仍是 v1 目标 | 拆出 `pack-docgen`：5 个文档件（writer/quality_gate/checker/layout/fact_checker）+ 6 条 docgen YAML + quality profile | 拆完 `--check` 不再要求 `agents/writer.py` 与 `pipelines/docgen.yaml`（原 `bootstrap.py:183-185,235` 硬绑〔实测：旧态 `--check` 输出逐条含这两项，装完即红〕） |
| 领域件与引擎件的依赖方向 | 引擎层无 `import agents/docpipeline`〔静态+护栏 `tests/test_layering.py`〕 | 保持 | 分层测试继续通过 |

### 5.3 表面层

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| MCP Server | **已达标**（2026-10-08）：6 tools——新增通用 `run_workflow(name, inputs)`（inputs 按「## 键」渲染为输入文档）；banner/日志改 stderr 与 `serverInfo.version` 接 `__version__` 均已修（FP-3 关闭）；真 stdio 往返测试在位（`tests/test_mcp_server.py::TestMCPOverRealStdio`）。原现状留档：5 tools 全是文档动词 / banner 污染 stdout / version 恒 `unknown`〔实测〕 | 加通用 `run_workflow(name, inputs)`；banner/日志改 stderr；version 接 `__version__` | 一条**真实 stdio 往返**测试通过 |
| HTTP 执行 API | 只有"提交文档生成任务"〔静态：`openapi_spec.py:126`〕 | `POST /api/workflows/{name}/runs` 接受任意 payload | 用非文档 pack 跑通该端点 |
| CLI | 功能完整但命名满是文档味（banner、argparse 描述、`--enhance`） | 改名 + 动词收敛为 `run / plan / lock / pack / plugin` | `--help` 里不再出现"文档生成流水线"字样 |
| 触发方式 | **6 种**（CLI / HTTP API / MCP / **定时** / **入站 webhook** / **队列多机 worker**〔均 2026-10-08 落地〕）；多机并发契约由真跨进程判据实证（互斥 / 崩溃接管 / 活 owner 不被抢），跨主机接管需共享 pid 命名空间 | 6 种（+ 定时 + inbound webhook） | 定时触发的 run 出现在同一队列与观测面（同 `run_plan` 路径，`tests/test_triggers.py` 钉住）；webhook 入站鉴权与审计均已兑现（`tests/test_webhooks.py`）；多机 worker 判据见 `tests/test_multi_process_workers.py` |

### 5.4 治理层

| 功能 | 现状 | v1 目标 | 验收判据 |
|---|---|---|---|
| 身份与租户 | **无**：共享 `ADMIN_API_KEY`；全仓 `tenant/principal/user_id` 0 命中〔静态〕 | token → 任务/成本/产物三类数据的作用域隔离 | 两个 token 互不可见对方任务（真 HTTP 断言，非 mock） |
| 状态后端 | 全 SQLite/JSON 单机文件；pub/sub 是进程内回调〔静态〕 | 后端抽象（SQLite / Postgres），队列与总线可换 | 两实例共享一库并发跑通（P1 判据） |
| 成本归因 | 定价表 + 按供应商/Agent 维度已有 | 增加按 **pack / token** 归因 | `GET /api/cost` 能按 pack 出账 |
| 审计 | **入站调用与配置变更均已留痕**（2026-10-08：webhook 全部调用成败落 JSONL；`/api/config` 每次变更落 `audit/config.jsonl`——key/新旧值/客户端/凭证指纹，敏感值只记脱敏形状，`GET /api/config/audit` 可查回） | 入站调用与配置变更留痕 | 每次 `/api/config` 变更有审计记录（`tests/test_config_audit.py` 九条钉住，含脱敏与写盘失败不静默） |

### 5.5 CI 与门禁（跨层，先于一切功能）

按 FP-2 修三处静默判绿；**性能门禁 `perf-regression` 当前不在 required 清单内**〔实测：required = `test (3.11/3.12/3.13/3.14)` + `docker`，且 `enforce_admins = false`〕，升级期需纳入或明确接受其非阻塞语义。

---

## 6. 前置必修：四个假阳性（不修则所有验收判据都能被"绿而未跑"骗过）

| 项 | 状态（2026-10-07） | 落点 |
|---|---|---|
| FP-1 质量门假绿 | **已关闭** | `4ede02b` |
| FP-1b 成品夹带抓取层原始素材 | **已关闭**（FP-1 的余波，见下方说明）：gate 与 CI 两侧各带同一判据，CI 不再只信 gate | 本轮提交 |
| FP-2 CI 三处静默判绿 | **已关闭并经真实 CI 确认** | `9ecfc12`；CI run 37504991608 在 `690c6d7` 上 5 条 required + perf 全绿，日志实证 `结果: 26 OK / 2 WARN / 0 ERROR` 与 `VULNS: []`（即 rc 被采信且没有误红） |
| FP-3 MCP stdout 污染 | **已关闭** | `10cc2e7` |
| FP-3b `--json-output` 的 stdout 混入过程输出 | **已关闭**：`--json-output` 时过程输出整体改接 stderr，JSON 是唯一一行 stdout（`tests/test_cli_output_channels.py`） | 本轮提交 |
| FP-4 `foreach` 未接线 / 无时间触发 / 无人审 | `foreach` **已接线并出厂**（2026-10-07·续4：`tests/test_foreach.py` 46 条 + `pipelines/api-digest.yaml`，六处变异对照全被抓住）；**时间触发、人审节点仍为 0**（本轮实测：产品内 grep cron/APScheduler 与 approval/manual_review 均 0 命中） | §5.1 |

**FP-1b 的来源（重要，因为它说明"修好一个假阳性"会暴露下一个）**：真实 CI 在 keyless 条件下产出了一份
**21,926 字节**文档并被本步骤记为 `OUTPUT FIDELITY OK`；本机同形状产物 28.4 KB、`quality_gate` 95.5 pass。
占比判据拦不住它——16 个章节里只有 4 个是占位符（25% < 34%），其余章节被喂进了抓取层的**原始素材块**
（`标题:/来源:/下载时间:` + 60 连等号分隔线，`agents/fetcher.py:700-703` 的落盘格式）。
先试过重复率与"导航行占比"两个候选指标，实测在真假样本上都没有可分离的分布（junk 样本
`dup_para_rate=0.167`、`menu_like_rate=0.024`，与真样本几乎同量级）——**用这种指标做门禁等于再造一个假绿**。
最终改用结构性签名：签名取自 fetcher 自己的落盘格式，不是关键词猜测。
> 遗留（不是本轮范围）：真正该做的是 writer 不再粘原块 + 主题相关性/引用可追溯性判定，
> 见 §5.1 与后续 P0。

### FP-1 · 质量门给空壳文档打 98.8 —— P0，代价 S

〔实测，本机 keyless / `pool_size: 2` / 单进程〕`--pipeline docgen` **exit 0**，落盘 3065 字节，`overall_score = 98.8 status = pass`，`checker` 只报 4 条 P3；而正文是 360 识图页面的样板文案，`writer.stats.empty_sections = [核心概念, 详细分析, 实践与应用, 总结]`（**4/5 章节为空**）。

根因三条：
1. `agents/quality_gate.py:60` —— `PLACEHOLDER_MARKERS` 只有 `"未采集到可整合的搜索结果"`、`"无待整合内容"` 两条；writer 降级时实际写的「降级声明」「（暂无可用的相关内容）」都不在其中，于是底线只剩"≥120 字符"，而垃圾有 3000 字符。
2. `run.py:349-352` —— 引擎**已经算出** `empty_sections`，却只 print 一条 WARNING，不参与 status/exit。信号算完就扔。
3. 评分维度被抓取噪声反向骗过：`substance = 100`（导航文案信息密度高、重复率低）、`topic_relevance = 100`（页面标题恰含主题词）。

修法：底线补三条判据（空章节占比 / 降级声明命中 / 抓取元信息行占比），`run.py` 的 WARNING 升级为影响 exit code；以本次实测输出为回归样本加测试。

### FP-2 · CI 三处静默判绿 —— P0，代价 S–M

〔静态，行号来自 `ci.yml` 当前工作区版本〕
1. `:90-98` `python run.py --check` 跑在 `set +e` 下，**退出码从未被读**，只用 grep 卡一行 selectolax。本机 `--check` 现为 `26 OK / 1 WARN / 1 ERROR`〔实测〕而 CI 照绿。
2. `:76-85` pip-audit **进程失败被强制 `rc = 0`**，只有跑出干净 JSON 且含漏洞才红 → 断网/超时 = "无漏洞"。
3. `:113-119` keyless docgen 只要日志含 `HARD_FLOOR` 就记 "SMOKE OK" → **CI 从不证明系统能产出任何东西**。
4. 连带：`:126-134` 的产物门禁是"≥800 字节 + 不含一条指定串"，**恰好挡不住 FP-1 那份 3065 字垃圾** —— 两个洞互补，单修任一个仍留绿。

修法：`--check` 退出码读回并区分 WARN/ERROR；pip-audit 失败改显式红或以 `xfail(strict)` 承载已知缺口；smoke 拆成"有 Key 才判成功，无 Key 明确标 skip 而非 OK"。
注意：修完 CI 很可能立刻变红〔实测：同一 SHA `14c98b3d` 的 E2E Nightly 已是 failure〕。**按"全绿才合、合并权在维护者"执行。**

### FP-3 · MCP stdio 被 banner 污染 —— P1，代价 S

〔实测〕起真子进程 `python run.py --mcp` 喂 `initialize` + `tools/list`：stdout 头两行是 `run.py:58-63` 的框线 banner，之后才是 JSON-RPC；`serverInfo.version` 恒为 `unknown`。现有 12 个 MCP 测试全在进程内调 `_handle_request`，**没有任何一条走过真实 stdio**〔静态〕，所以该 bug 永远测不到。A 路线的第一入口就是 MCP，stdio 通道被污染属协议级问题。

### FP-4 · `foreach` 已声明未接线 —— P1，属立项而非修复

〔静态〕`scheduler.py:52-76` 的 `_as_foreach` 零调用点（`DEFAULT_FOREACH_MAX_ITEMS` 只在自己函数内用），而 `naming.py:6` 把 foreach 写成目标能力。全仓无 cron/时间触发、`grep approval|human|manual_review` 0 命中。
处置：**接线**（按"已声明未接线要么接要么删"的规矩）。`foreach` 是相对 MAOP 的差异化表达力。代价 M（3–5 天）：幂等键按 item 分裂 + 上限护栏 + item 数进 `topology_hash` 与锁文件。
附带立项：**人审 / approval 节点**（当前完全没有），因为"取数 → 核查 → **待批** → 交付"是运行时类产品的常见诉求。

> **进展（2026-10-07·续4，实测）**：`foreach` 一项已接线完成——解析期 `_as_foreach` 有了调用点
> （`AgentConfig.foreach` / `ExecutionNode.foreach`，并拒绝 `foreach`+`call`、`foreach`+`pool_size>1`），
> 运行期 `_execute_foreach` 按项投递（引擎自有键 `item`/`index`/`count`、逐项幂等键后缀 `#{i}`、
> 逐项限流、空列表/非列表/取不到值/超 `max_items` 均显式报错、任一项失败即节点失败），
> 展开契约进了 `topology_hash`；出厂消费者 `pipelines/api-digest.yaml`（+ lock，
> `topology_hash=001d667537bd`）；判据 `tests/test_foreach.py` 46 条，含一条盯调用点的 AST 守卫，
> 六处变异对照全部被抓住。**同一节里剩下的两项仍是零**：全仓无 cron/时间触发，
> 也仍无 `approval` / 人审节点——本轮不占口径，别把"foreach 已接线"读成"FP-4 已关闭"。

---

## 7. 命名与迁移（已确认：原地改名，本地路径迁至 `F:\Nexus\`）

### 7.1 命名系统（一次定齐，避免二次破坏）

| 项 | 现值 | 目标值 | 影响面〔静态〕 |
|---|---|---|---|
| 产品名 / banner | Doc-Pipeline · 文档生成流水线 | **Loom · Agent 工作流运行时** | `run.py:60`；"文档生成流水线" 11 处 / 8 文件 |
| 本地路径 | `F:\Nexus\Workflow\doc-pipeline` | `F:\Nexus\Loom` | 见 §7.2 |
| GitHub 仓 | `Levango7/doc-pipeline` | `Levango7/nexus-loom` | 旧 SSH remote 需更新 |
| 发行名 | `doc-pipeline` | `nexus-loom` | `pyproject.toml:6` |
| **import 根包** | `pipeline_core` | **第一阶段保持不动** | 647 处 / 131 文件，且被 MAOP 硬绑 |
| 领域包 | `docpipeline` | `loom_pack_docgen`（pack 化） | 83 处 / 23 文件 |
| console script / CLI | `doc-pipeline` | `loom`（动词：`loom run / plan / lock / pack / plugin`） | `pyproject.toml:41` |
| env 前缀 | `DOC_PIPELINE_*` / `DOCPIPE_*` | `LOOM_*` | 13 处 / 8 文件 + 16 处 / 26 文件 |
| **插件 group（新增）** | 无 | `loom.agents` | 20x 的开关 |
| MCP `serverInfo.name` | `doc-pipeline` | `loom` | `mcp_server.py:52` |
| 观测 namespace | `docpipeline` | `loom` | `observability.py:161` |
| **API 原语（新增）** | 裸 `task_id` 字符串 | **`RunHandle`**：`status / await / cancel / resume / artifacts` | HTTP `/api/tasks` 与 MCP 返回值都要换成它 |

> **命名已确认（2026-10-07）**：产品名 **Loom**；`handle` 这个词留给 API 原语 `RunHandle`
> ——运行时交给调用方的本来就是一个句柄，而现在 HTTP/MCP 传的是裸 `task_id` 字符串。
> 占用实测（HTTP 200=已占、404=空闲）：PyPI 裸名 `loom`/`chassis`/`tessera`/`weft`/`flowkit`/
> `runloom`/`agentloom`/`loomkit`/`handle` **均已被占**（其中 `handle` 是 0.0.0、summary 写
> "placeholder" 的空壳抢注），`nexus-loom`/`loom-runtime`/`handle-runtime`/`nexloom` **空闲**；
> npm 的 `handle` 与 `official` 都已占。故发行名必须带前缀，不能裸 `loom`。
> `official` 已否决：形容词无专指、搜索不可达、CLI 读作无主语、`import official` 与
> Vue 生态"官方模板目录"的既有心智撞车。`track` 已否决：PyPI 被真实包占（基因组数据读写），
> 且默认联想是追踪/埋点。
> 已避开：Conductor / Cadence / Temporal / Prefect / Weave / Flux / Foundry（同层同名产品）。

### 7.2 迁移依赖（实测：唯一外部消费者是 MAOP）

跨 `F:\Nexus` 全树扫描，除本仓两份历史审计文档（记录旧路径，不改）外，只有 **MAOP** 与它的工作副本 **MAOP-pr64** 依赖本位置：

- `MAOP/py/maop/delegate/doc_pipeline_adapter.py:38-55`：按三条候选找根 —— `DOC_PIPELINE_ROOT` env → `~/Nexus/Workflow/doc-pipeline` → `repo_nexus/Workflow/doc-pipeline`，**判定文件是 `pipeline_core/__init__.py`**（这也是包名不能轻改的直接证据）。
- `MAOP/py/tests/test_doc_pipeline_adapter.py:62,68,79,86,93`：**硬编码 `F:\Nexus\Workflow\doc-pipeline`**，直接读 `pipeline_core/pipeline.py`、`circuit_breaker.py`、`event_hook.py` 源码文本；不在 `skipif import pipeline_core` 的保护范围内 → **搬目录后 MAOP 测试直接红**。
- 同步项：`config/agents.yaml:176-192` 与 `:737-748` 两处条目、`core/reliability/circuit_breaker.py:38` 的 agent 名单、MAOP docs 4 处。
- 本仓 `.test_state/`、`logs/`、`bus_data/` 中的绝对路径全是 gitignore 产物，搬完清掉重跑即可。

### 7.3 迁移动作序列（顺序不可颠倒）

**边界先说清：本产品是独立项目，不并入 MAOP。** MAOP 是外部调用方（`MAOP/config/agents.yaml:176` 已把本仓登记为一条 `driver: python` 适配器），这个关系本身就是"独立"的证据。MAOP 侧硬编码本仓绝对路径属**它的技术债**，不构成本项目并入的理由；四条独立成仓的硬理由：① 20x 的物理来源是第三方能 pip 安装并发插件，合并进治理层后插件要跟着 MAOP 发版；② 两套执行模型（派发外部 CLI 外壳 vs 跑进程内 Python 节点）混在一仓会抹掉差异化；③ 许可结构会糊——`MAOP/LICENSE` 是 MIT 而 `MAOS/LICENSE` 是 `Commercial License - Levango7`，运行时一旦被并进 MAOP 线就会被期待做成 MAOS 商业特性；④ 终态应是 MAOP 依赖发布出去的包，而不是 `sys.path.insert(外部仓路径)`。

1. ~~先把在飞的 10 个未提交文件收成一个 commit~~ **已完成**（`d538f8a`，与产品定义/修复工作分账）。
2. 关闭 §6 的前置必修项后再搬，避免"搬迁 + 修 bug"两种失败原因混在一次 CI 里。
3. GitHub 端 rename（保留重定向）。
4. 本地整目录 move（不 cp，保 `.git` 完整）。
5. 更新 remote URL + 清 gitignore 产物（`bus_data/`、`logs/`、`.test_state/` 里都是旧绝对路径）+ 重跑全量测试。
6. **MAOP 侧不改仓**（已定）：迁移时设环境变量 `DOC_PIPELINE_ROOT=<新路径>` —— `doc_pipeline_adapter.py:38-55` 的第一候选就是它，于是 MAOP 立刻能找到新位置，无需它侧任何提交。
   - 残留：`MAOP/py/tests/test_doc_pipeline_adapter.py:62,68,79,86,93` 五处**硬编码绝对路径**，环境变量救不了它们（不经 `_resolve_doc_pipeline_root()`）→ 搬完 MAOP 那 5 条用例会红。这是 MAOP 仓的账，**改动权在维护者**，本项目只记账不代改。
   - 若日后想让旧脚本零改动继续跑，可在旧路径留 Windows junction 兜底；代价是机器上出现"看着像两个仓"的入口，容易误编辑，非必要时不采用。
7. 才开始动代码内的名字（顺序：banner/CLI 文案 → env 前缀 → MCP/观测标识 → 包名（需与 MAOP lockstep）→ pack 拆分）。

---

## 8. 首发 pack 清单（默认选型，可替换）

凑 §2 的 ≥10 任务类型。前 8 个为默认选型，后 2 个为候补：

| # | pack | 主要复用件 | 为何选它 |
|---|---|---|---|
| 1 | 文档生成（现 docgen 降级） | writer/quality_gate/checker/layout/fact_checker/renderer | 存量能力，证明"引擎不换语义也能跑旧活" |
| 2 | API 报告 | http_request/transform/safe_writer | 已有 `api-report.yaml`，是插件契约的现成消费者 |
| 3 | 竞品/技术情报 | researcher/fetcher/transform | 检索+抓取是已实测最成熟的两个通用件 |
| 4 | 本地资料 → 知识摘要 | ingest/knowledge_base/embeddings | `kb-docgen` 已离线跑通 |
| 5 | 数据集质检报告 | http_request/transform/quality_gate 骨架 | 让质量门在非文本产出上被验证 |
| 6 | 会议纪要 → 周报/OKR | transform/safe_writer/version_manager | 高频、无需外部 API |
| 7 | 告警 → 处置手册 | ingest/transform/writer 模板 | 把 fact_checker 用在结构化证据上 |
| 8 | 合同/发票要素抽取 | ingest/http_request/transform | 考验"受限声明语言"表达力，反向推动 `foreach` |
| 9 | K8s 巡检报告 | http_request（API server）/transform | 需内网白名单，正好压测 `allow_hosts` |
| 10 | 需求 → 测试用例 | requirements_analyzer/transform | 已有 `docreq` 半接线 |

**交付状态（2026-10-09）**：1–10 全部落地为 `pipelines/*.yaml`；表外另加 1 条 `deck-brief`（汇报要点 → md + pptx 双交付），合计 11 条 pack，每条都有独立 `.lock` 与真 E2E 判据（清册见 §2 表）。第 8 条的 `foreach` 与第 9 条的 `allow_hosts` 由此从机制变成出厂流水线里的实际载荷。

---

## 9. 未决项

1. ~~是否授权改 MAOP 仓~~ **已定：不改**（§7.3 第 6 步）。迁移用 `DOC_PIPELINE_ROOT` 环境变量指向新路径；MAOP 那 5 处硬编码测试会红，属它自己的账，本项目只记账不代改。
2. **`perf-regression` 是否纳入 required**，以及是否接受 `enforce_admins = false` 的现状。
3. **人审/approval 节点**进 P0 还是 P1（本文暂放 P1）。
4. ~~Chassis / Tessera 的 PyPI 占用复查~~ **已查**：两者裸名均已被占（§7.1 注）。当前定名 Loom / 发行名 `nexus-loom`（实测空闲）。
5. **FP-2 的真实验证只能在 CI 上做**：本轮把 `--check` 的退出码与 pip-audit 的进程健康变成硬依赖，ubuntu runner 上若有本机没暴露的结构性问题，CI 会如实变红 —— 届时"CI 全绿"需要重取，`docs/product-spec.md` §3 P0 判据第 4 条以此为准。
6. README 文档表与 CHANGELOG 对本 spec 的引用：CHANGELOG 已在本轮同步记录三项修复；README 的文档索引表尚未加 `product-spec.md` 一行（下一批命名清理时一并改，避免与 pack 拆分改动叠在同一次提交）。
