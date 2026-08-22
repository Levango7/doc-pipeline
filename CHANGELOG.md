# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

### Added
- 新增10个测试模块（test_llm_router, test_search_engines, test_quality_gate_scoring, test_run, test_benchmark, test_markdown_checker, test_safe_writer, test_layout_optimizer, test_convert_ascii, test_format_converter）
- 525个测试全部通过

## [3.2.0] - 2026-08-08

### Added
- 成本追踪/告警/质量闭环/MCP Server/Agent沙箱/集成测试

## [3.1.0] - 2026-08-06

### Added
- async I/O + orjson + SSE reconnect + fast_json module
- PEV-ready API extensions + EventHook system
- /stream endpoint with end-to-end async pipeline
- run_plan_async + on_stop lifecycle hook