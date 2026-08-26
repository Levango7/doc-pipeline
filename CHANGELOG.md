# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [3.8.0] - 2026-08-26

### Security（深度审计修复 — 3路并行审计，~45项发现）

- **P0 SSRF 全裸修复**：fetcher sync/async 出网请求接入新增 `pipeline_core/url_guard.py`
  （私网/环回/云元数据/DNS解析逐记录校验），redirect 改手动循环逐跳校验（上限5跳）；
  公网302跳内网/169.254元数据端点路径封死（58项新测试）
- **P0 断点续传静默数据损坏**：checkpoint load 恢复 DAG 节点状态，execute_level 跳过已完成节点
  不再重发 bus.request；resume 后 attempts==0 节点绕过持久化幂等键；save 改原子写且失败上浮
- **P0 成本控制双失灵**：`check_budget()` 接线进 llm_router chat/chat_async 调用前（超预算抛
  BudgetExceededError）；chat_async 补记成本（writer 主力路径不再漏记）；PRICING 补
  openai/deepseek/moonshot/qwen/default；响应 usage 字段优先计费
- **P1 error字典被判成功**：`{"error":...}` 结果进入业务失败通道（此前下游拿空数据绿色DONE）
- **P1 message_bus 持锁投递地雷拆除**：REQUEST/RESPONSE 移到锁外 deliver；订阅者异常立即回覆
  错误响应（消灭单节点空烧 node.timeout×max_retries≈20分钟）
- **P1 scheduler 同层依赖校验漏洞**：同层依赖现在正确报错（此前并行执行确定性读到空上游结果）
- **P1 熔断器/限流器切 time.monotonic()**：NTP 回拨不再把令牌桶扣成负值持续拒流
- **P1 进程池 BrokenProcessPool 自愈**：销毁中毒单例重建并重试一次，子进程崩溃不再永久毒化 auto 模式
- **P1 CORS 敞口收口**：Origin 白名单（同源/回环/ADMIN_CORS_ORIGINS env），移除通配 ACAO:*
- **P1 pipeline.started 事件配置脱敏**：api_key/token/secret/password 叶子值 ***redacted***
  （此前明文落盘消息库并广播 webhook）
- **P1 CLI 契约**：--pipeline 拼错报错列出可用名退出码2（此前静默改跑第一个yaml）；
  任务 FAILED 进程退出码 1（此前恒 exit 0）
- **P1 config 热更新半写容错** / **version_manager 索引原子写+损坏安全模式（不清零历史）** /
  rollback 写回前自动保存当前内容 / quality_gate 英文功能词不再误判专名（消灭无谓三轮重烧） /
  scripts/safe_writer 对齐 os.replace 原子替换

### Fixed

- `/stream/metrics` 恒为零的功能性失效（改为跨任务聚合快照）
- SSE 统一 StreamEvent 序列化（ts/section/total 上线）+ 15s 心跳帧 + 客户端断连取消流水线止损
- MCP 业务失败改 isError:true 语义；get_task 校验 task_id；generate_document 支持 output；
  流水线目录锚点统一项目根；mcp_ 临时输入文件清理
- Admin API 错误格式统一（503/400 语义、兜底不泄内部异常）；task_{id}.md 输入临时文件清理
- openapi_spec：TaskInfo 枚举补 paused、/stream/metrics 内容类型、补 / 与 Last-Event-ID 声明
- streaming pause 自旋空转（Event→Condition）；Registry get_or_create 支持配置热更新
- dashboard：API_BASE 同源化、progress 百分比换算修正

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added

- **dashboard 新建任务卡片**：query/pipeline(下拉含 docreq)/output 表单提交 POST /api/tasks，
  running 任务展开 EventSource 实时章节进度——docreq/docgen-verified 获得 UI 触达
- `pipeline_core/url_guard.py` 共享 URL 安全校验模块
- 新增测试 ~200 项（642→840 passed），ruff/mypy 零告警
## [3.7.0] - 2026-08-25

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（需求分析器）
- **requirements_analyzer 需求分析 Agent**：流水线最前端的意图解析节点，把用户输入
  解析为结构化 `DocumentSpec`（doc_type / scope / audience / depth / constraints /
  sources / template / language），供下游 researcher 与 writer 消费：
  - 双路径：有 LLM 走一次小调用生成 JSON（含枚举校验与置信度钳制，非法值回落默认），
    无 LLM / 失败时回退规则引擎（类型·深度·读者提示词匹配 + 关键词提取 + URL/文件引用收集）
  - 歧义检测：输入过短、类型不明、受众未指定时降低 confidence 并生成追问建议
    （field/question/suggestion），confidence 低于阈值（默认 0.7，可配）标记
    needs_clarification，追问条数上限可配（max_questions）
  - 下游接线：dag_executor 将 spec 注入所有后续节点 payload；researcher 用 spec.scope
    补充检索词；writer 用 doc_type 前缀标题、scope 兜底主题、audience 记录读者水平
- 新增 `pipelines/docreq.yaml`（9 层 DAG，首层 requirements_analyzer）。
  命名避开 `docgen*` 前缀以保持既有默认流水线解析顺序不变；
  运行方式：`python run.py input.md --pipeline docreq`

### 测试
- 新增 tests/test_requirements_analyzer.py（24 项）：规则分析各维度、DocumentSpec
  往返序列化、关键词提取去重/截断/停用词、handle 的 DAG 输入文件读取、LLM 成功/
  失败回退/非法枚举回落、analyze() 便捷函数

## [3.6.0] - 2026-08-24

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（内容生产能力提升）
- **fact_checker 事实核查 Agent（MVP）**：
  - 从最终文档提取数字类可验证声明（百分比/带单位数值/年份/版本号，上限可配），
    对照检索源做一致性核查：无 LLM 用归一化字符串匹配（零成本基线），有 LLM 用
    批量语义判定（supported/refuted/unverifiable），LLM 失败自动回退字符串匹配
  - 未核实声明在文档尾部附加「事实核查附注」（明确标注：启发式核查，
    unverifiable ≠ 错误），核查报告同时写入节点结果供 API/MCP 消费
  - 新增 `pipelines/docgen-verified.yaml`（8 层 DAG：checker → fact_checker → layout）；
    **默认 docgen 流水线零改动**
- **主流 LLM 供应商预置**：llm_router 新增 openai / deepseek / moonshot / qwen 四个
  OpenAI 兼容供应商定义（此前仅国内二线云厂商），`.env.example` 补三行组配置示例；
  Claude 原生接口非 OpenAI 格式，已在模板中说明经兼容网关接入

### Fixed（降级透明化）
- **空章节不再静默交付**：writer 无 LLM 路径下未能填充内容的章节，现在会在文档头部
  插入「⚠️ 降级声明」块列出章节名与修复建议，CLI 渲染时同步输出 stderr 警告，
  result.stats 带 empty_sections 字段供程序化消费
- 移除 run.py 中一段历史遗留的不可达死代码（three_pass 分支内 except 块后的 ascii-fix）

### Removed（Breaking）
- **移除已废弃的 ThreePassPipeline**（该模块自带的 DeprecationWarning 声明
  "不再维护，将在未来版本移除"）：删除模块、`--three-pass` CLI 参数、包导出。
  迁移：`--pipeline docgen` 已覆盖同等能力且更完善；`--three-pass` 现在打印迁移指引并 exit 2
- 删除 config.json / config.production.json 中零消费的死配置键 `writer.template_dir`
  （writer 实际使用内置骨架模板，templates 目录从未存在）

### 测试
- 新增 tests/test_fact_checker.py（13 项）：声明提取/来源匹配/嵌套来源收集/
  LLM 失败回退/附注渲染；docgen-verified.yaml 解析与 fact_checker 注册端到端验证

## [3.5.0] - 2026-08-24

### Fixed（第九轮审查修复：API 契约 / 鉴权可用性 / MCP / CICD）

**鉴权与 Dashboard 可用性**
- **鉴权本机信任模式**：未配置 `ADMIN_API_KEY` 时，绑定回环地址免鉴权访问
  （此前所有受保护端点无条件 401，Dashboard 永远空白且无任何提示）；
  绑定非回环地址 + 无 key → **拒绝启动**（安全门），与 docs/api.md 原有描述对齐
- Dashboard 前端：请求携带 Bearer Token（localStorage）；401 时弹出 Token 输入框自动重试
- run.py 消费配置文件的 `admin_api.host/port`（此前该配置块整体无效，
  config.production.json 的 `0.0.0.0` 绑定从未生效）

**API 契约修复**
- **OpenAPI 安全声明反转修正**：新增全局 `security: [BearerAuth]`；
  `/stream` 移除错误的 `security: []`（实际需鉴权）；`/health` 显式豁免——
  此前整份 Spec 将全部端点描述为公开，与实现完全相反
- **版本管理端点接线**：`_handle_versions_list/diff/rollback/stats` 四个 handler
  已实现但从未注册路由（全部 404 死代码）→ 现已接入 do_GET/do_POST 并补 OpenAPI 定义
- 错误响应信封统一为 `{"error": ...}`（消除三种顶层结构并存）
- `cancel/pause/resume/rerun` 任务不存在时返回 **404**（此前 200 + false 无法区分）
- `/api/dashboard`、`/api/pipeline`、`/stream/metrics`、versions 四端点补入 OpenAPI；
  TaskSubmit schema 补 `output` 参数

**前端字段契约修复（Dashboard 三处恒错数据显示）**
- Queue Depth 改读 `/health` 顶层 `queue_depth`（原读 metrics 子对象不存在的字段，恒 0）
- DB Store 显示 `store.messages` 条目数 + `db_size` MB（原读不存在字段恒显示 "6 entries"）
- 任务列表改用 `/api/dashboard` 聚合端点，进度条真实生效
  （原读 `/tasks` 列表不存在的 progress/steps 字段，恒 0%）
- 部分请求失败时状态栏显示"⚠ 部分数据不可用"角标（原先静默渲染空数据）

**其他修复**
- MCP Server `get_pipeline_info` 必失败修复：改读 `plan.levels`
  （原访问不存在的 `plan.execution_order/dag_nodes` 属性，每次调用 -32603）；
  SERVER_VERSION 动态取包版本（原硬编码 3.2.0）
- SSE 流水线线程 `worker.join()` 补 120s 超时（原无限阻塞可耗尽 HTTP 线程）
- pause() 不再在节点并发执行中途保存撕裂 checkpoint；改为执行循环在暂停边界
  （上一 level 完结后）保存一致性快照；语义已在 docstring 文档化
- POST 路由先鉴权后读请求体；task_id 路径参数接入 `_validate_task_id` 校验
- 删除死模块 `batch_queue.py`（167 行语句零调用方）

### Changed（CICD）
- **perf-regression job 从形同虚设变为真实回归门**：
  baseline 经 actions/cache 在运行间传递 + benchmark.py 对比通过后滚动更新基线
  （此前 baseline 被 gitignore 导致 CI 永远走"无 baseline 跳过检测"分支）

### 测试
- 新增 `tests/test_auth_contract.py`（15 项）：真实 HTTP 层鉴权矩阵、
  versions 路由接线、404 语义、OpenAPI 安全契约、MCP get_pipeline_info

## [3.4.0] - 2026-08-22

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added（进程执行模式正式支持）
- **`executor_type: process` 从实验性限制变为可用特性**——子进程上下文自动重建：
  - `DAGExecutor` 新增 `child_context` 配置（agents_dir / agent_names / config，
    由 `PipelineOrchestrator.register_agents()` 写入），纯数据、可 pickle
  - `_execute_node_worker` 在 worker 进程内依据 child_context 一次性重建
    Registry（关闭健康检查线程）与非持久化 MessageBus，经 AgentLoader 加载 Agent；
    每个worker 进程仅重建一次（模块级缓存）
  - `__getstate__` 补齐剥离全部含线程锁组件（熔断器/限流器/Metrics/查询缓存），
    修复此前 DAGExecutor 整体 pickle 必然失败导致节点回落父进程重试的问题；
    `__setstate__` 对剥离组件按需重建可用实例
- 新增 `tests/test_process_mode.py`：pickle 往返、无 context 守卫报错、
  **真实 ProcessPoolExecutor 跨进程执行**（探针 Agent 返回子进程 pid ≠ 父进程 pid）
- 文档：docs/architecture.md §5 更新为进程模式工作原理 + 已知限制

### Changed
- executor_factory 进程模式告警更新：不再称"实验性/必然失败"，改为说明序列化开销与隔离限制

## [3.3.3] - 2026-08-22

### Changed（剩余技术债清零）
- **边缘超长方法拆分**（上轮 150L 边缘项全部处理，全仓 >150L 函数归零）：
  - `run.py:main` 199L → ~110L（提取 `build_arg_parser`）
  - `run.py:_run_single_task` 157L → ~60L（提取任务摘要/进度轮询/步骤收集/
    输出路径解析/结果渲染 5 个函数）
  - `pipeline.py:run_plan_async` 153L → ~85L（池化合并复用 `_merge_pooled_results`；
    收尾提取为 `_finalize_plan_task_async`，与同步版差异保留：不更新 task_queue、不发事件钩子）
  - `scripts/safe_writer.py:safe_write` 161L → ~90L（提取备份+manifest/换行符处理/
    临时文件写入/体积行数校验 4 个函数）
  - `admin_api.py:_handle_stream` 167L → ~80L（提取 SSE 帧发送/重连 replay/
    writer 查找/后台流水线线程 4 个方法）
- **.dockerignore 补全**：新增 checkpoints/versions/backups/cache/bus_data/
  .pytest_tmp/.test_checkpoints/.test_outputs/.zcode（运行时产物不进镜像构建上下文）

### Fixed
- mypy：run.py 提取函数的 Any 返回值显式 str() 收敛（3 处 no-any-return）

## [3.3.2] - 2026-08-22

### Changed（技术债清理）
- **超长方法拆分**（行为不变，582 测试全过）：
  - `writer.py:_restructure_document` 237L → 57L（提取 prompt 增强/素材构建/异步生成/
    Mermaid 修复/拼接装配等 7 个方法；顺带删除无调用的死代码嵌套函数 `_llm_generate`）
  - `pipeline.py:run_plan` 192L → 116L（提取 `_merge_pooled_results` / `_finalize_plan_task`）
  - `dag_executor.py:execute_level` 190L → 62L、`execute_level_async` 178L → 瘦身
    （共享提取：业务失败判定/重试结果评估/节点成功落账/futures 提交/同步与异步重试循环/步骤记录，
     两版本 finally 记录逻辑去重为单一 `_record_step_result`）
  - `openapi_spec.py:generate_spec` 267L → ~30L（按 section 拆为模块级函数；
     生成结果经 JSON diff 验证字节级一致）
- **Dashboard XSS 加固**：app.js 全部 6 处 innerHTML 字符串拼接改为
  createElement/textContent DOM 构建，动态数据不再经过 HTML 解析；
  移除不再需要的 escapeHtml 辅助函数
- **文档补全**：新增 docs/api.md（REST API 参考）、docs/architecture.md（分层架构 +
  线程模型 + 数据流）、docs/agents.md（Agent 开发指南 + 沙箱规则）；README 增加文档索引

### Fixed
- mypy：run_plan 提取后 `combined` 字典失去上下文推断导致的 2 个新错误（显式标注）；
  dag_executor 辅助函数返回类型收紧（原代码靠 type: ignore 压制）

## [3.3.1] - 2026-08-22

### Fixed
- **writer.py 生产 bug**：`_restructure_document` 的 `asyncio.gather` 在事件循环外调用，
  配置 LLM Key 且走同步调用路径时必然抛 `RuntimeError`（无 current event loop，协程未 await）。
  修复为在运行中的 loop 内执行 gather；同时移除集成测试中掩盖该缺陷的 `contextlib.suppress`
- scripts/format_converter.py: 清零 18 个 mypy 类型错误（str/Path 混用、Optional 未收窄），行为不变
- llm_router.py: 注册 atexit 钩子关闭共享 aiohttp Session，消除连接泄漏告警
- Dockerfile: 显式创建并 chown checkpoints/logs/versions/backups 目录，补全 VOLUME 声明
  （修复匿名卷 root 属主隐患；versions/backups 数据不再随容器销毁丢失）
- 文档：README/deployment.md 与 config.json 实际搜索引擎列表对齐（原 mock 描述过时）；
  Python 版本要求统一为 3.11+（与 pyproject 一致）；测试计数更新为 582；
  生产配置对比表与 config.production.json 实际内容对齐

### Changed
- CI: perf-regression 在 push main 时也触发（原来仅 PR）；dev 依赖安装加版本下界（对齐 pyproject dev extras）
- run.py: `_resolve_pipeline_plan` 返回类型精确为 `tuple[Any, bool]`

## [3.3.0] - 2026-08-11

### Fixed
- 修复全部140项审计问题（24 P0 + 58 P1 + 58 P2）
- P0: pipeline.py run() NameError、run_steps清理、PipelineTask pickle化
- P0: dag_executor.py fail_fast软中断
- P0: message_bus_v3.py 超时竞态、幂等原子性、DLQ处理
- P0: task_queue.py fd泄漏(threading.local)、total_changes回归
- P0: circuit_breaker.py HALF_OPEN CAS原子递增、回调移出锁外
- P0: rate_limiter.py Condition关联锁、notify_all
- P0: cache_manager.py 回填用文件原始ts、双重record_set修复
- P0: llm_router.py aiohttp timeout、异常吞没
- P0: search_engines.py 异常吞没、CacheManager.put→set、线程安全
- P0: admin_api.py /stream鉴权、webhook SSRF防护、output路径白名单
- P0: checkpoint_manager.py task_id路径遍历校验
- P0: version_manager.py rollback路径白名单
- P0: agent_loader.py AST沙箱增强(ImportFrom+裸名黑名单)
- P0: quality_gate.py 专有名词覆盖率阈值策略
- P0: safe_writer_agent.py _current_payload初始化、handle_writer_done写入
- P0: checker.py 移除重复订阅
- P0: researcher.py _search_manager属性名修复
- P0: run.py YAML加载失败友好退出、SIGTERM handler、局部导入修复
- P1: observability.py log put阻塞→put_nowait
- P1: event_hook.py 冗余_ensure_webhook_engine
- P1: registry.py _check_respawn持锁外部调用
- P1: quality_feedback.py busy_timeout PRAGMA
- P1: document_enhancer.py 空内容回退
- P1: three_pass_pipeline.py ThreadPoolExecutor(0)边界
- P1: benchmark.py 除零保护
- P1: scripts/markdown_checker.py _check_structure修复
- P1: scripts/safe_writer.py checksum自引用修复、file_checksum异常处理
- P1: scripts/convert_ascii.py ASCII_TREE_PATTERN乱码修复
- P1: scripts/format_converter.py 列表项<ul>包裹、mermaid重名

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- 新增10个测试模块（test_llm_router, test_search_engines, test_quality_gate_scoring, test_run, test_benchmark, test_markdown_checker, test_safe_writer, test_layout_optimizer, test_convert_ascii, test_format_converter）
- 525个测试全部通过

## [3.2.0] - 2026-08-08

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- 成本追踪/告警/质量闭环/MCP Server/Agent沙箱/集成测试

## [3.1.0] - 2026-08-06

- **P1 版本锁定机制接线**：parse/parse_file 自动校验同名 .lock（含 config_hash 配置漂移检测，
  此前仅生成从不校验且零调用方）；run.py 新增 --write-lock；docgen.lock 重生成至当前真实状态
- **P1 document_enhancer 三连**：输出原子写；_clean_llm_output 感知代码 fence（不再删除代码块内
  ## 注释行）；主路径 LLM 失败回退原文不再被误清洗（对齐分块路径 identity 判定）
- P2 收尾批：task_queue recover 支持 stale_seconds+owner_pid 跨进程判别与 close_all；
  cache file 后端接入 TTL 淘汰（BaseAgent CACHE_TTL 默认 0→3600）；
  message_bus publish 硬上限强制+shutdown 竞态消除+worker 连接自关；
  registry respawn per-name 锁防双建泄漏；streaming 队满分级丢弃保边界事件完整；
  writer 双流回调按 task_id 路由；safe_writer payload 并发隔离+manifest .bak 兜底+
  备份清理限定本文档；quality_gate profile 缺键启动期报错定位；
  base_agent 统计计数加锁
- 接口面收尾：cache/clear、config set、versions/rollback、dlq replay 四类危险操作要求
  X-Confirm: yes（428）并输出结构化审计日志；访问日志 token 打码；
  MCP initialize 协议版本回显；dashboard token 改 sessionStorage；
  new_task_id() 统一三入口任务号（uuid4 hex[:16]）

### Added
- async I/O + orjson + SSE reconnect + fast_json module
- PEV-ready API extensions + EventHook system
- /stream endpoint with end-to-end async pipeline
- run_plan_async + on_stop lifecycle hook