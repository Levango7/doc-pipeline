# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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