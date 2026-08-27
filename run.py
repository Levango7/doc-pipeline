#!/usr/bin/env python3
"""
run.py - 文档生成流水线入口 v3.1
===============================
核心特性：
  - 支持声明式流水线配置 (YAML) via Scheduler + run_plan
  - DAG 并行执行 + 自动重做循环 + 熔断器 + 指数退避重试
  - 完整审计日志 + 事务 checkpoint + 临时文件自动清理
"""
import argparse
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))


# ─── 优雅退出：SIGTERM (Docker/systemd) → 触发 KeyboardInterrupt ───
# 注意：handler 注册移入 main()，避免 import run 副作用（测试时 import 会劫持 SIGTERM）
def _sigterm_handler(signum, frame):
    raise KeyboardInterrupt


def _install_sigterm_handler():
    """注册 SIGTERM 处理器（仅在 main 中调用，避免 import 副作用）"""
    # 非主线程或非主解释器（如 worker 进程）无法注册信号，忽略
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _sigterm_handler)


def _load_dotenv():
    """从项目根目录 .env 文件加载环境变量（不覆盖已存在的）"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

from pipeline_core import PipelineOrchestrator, TaskStatus, __version__  # noqa: E402
from pipeline_core.ids import new_task_id  # noqa: E402


def print_banner():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║          Doc-Pipeline v{__version__} - 文档生成流水线               ║
║          声明式 DAG | 自动重做 | 熔断器 | 审计日志             ║
╚══════════════════════════════════════════════════════════════╝
""")


def output_json_result(task, output_path, steps, status):
    """输出 JSON 结果供 wrapper 解析"""
    result = {
        "exit_code": 0 if task.status == TaskStatus.DONE else 1,
        "stdout": "",
        "stderr": task.error or "",
        "output_path": output_path,
        "status": status,
        "steps": steps
    }
    print(json.dumps(result, ensure_ascii=False))


def _run_ascii_fix(output_path: str):
    """将文档中的 ASCII 图转换为 Mermaid 图"""
    try:
        from scripts.convert_ascii import AsciiConverter
        converter = AsciiConverter()
        text = Path(output_path).read_text(encoding="utf-8")
        converted = converter.detect_and_convert(text)
        if converted != text:
            Path(output_path).write_text(converted, encoding="utf-8")
            print(f"\n[ascii] ASCII 图已转换为 Mermaid，保存至: {output_path}")
        else:
            print("\n[ascii] 未检测到 ASCII 图")
    except Exception as e:
        print(f"\n[ascii] ASCII 转换失败: {e}")


def _run_export(md_path: str, fmt: str, export_path: str = None):
    """导出 Markdown 为其他格式"""
    try:
        from scripts.format_converter import FormatConverter
        converter = FormatConverter()
        if fmt == "html":
            out = export_path or md_path.replace(".md", ".html")
            converter.markdown_to_html(md_path, out)
            print(f"\n[export] HTML 已导出: {out}")
        elif fmt == "word":
            out = export_path or md_path.replace(".md", ".docx")
            converter.markdown_to_word(md_path, out)
            print(f"\n[export] Word 已导出: {out}")
        elif fmt == "png":
            out_dir = export_path or Path(md_path).parent / "images"
            converter.render_mermaid_in_markdown(md_path, str(out_dir))
            print(f"\n[export] Mermaid 图片已渲染至: {out_dir}")
    except Exception as e:
        print(f"\n[export] 导出失败: {e}")


def _get_orchestrator(project_root: Path) -> PipelineOrchestrator:
    """创建并注册 Agent 的编排器实例"""
    orch = PipelineOrchestrator(
        agents_dir=str(project_root / "agents"),
        checkpoint_dir=str(project_root / "checkpoints"),
    )
    return orch


def _load_config(args_args: argparse.Namespace, project_root: Path) -> dict:
    """加载项目配置：优先 --config，其次 config.json"""
    config_path = args_args.config or (project_root / "config.json")
    if Path(config_path).exists():
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def _available_pipeline_names() -> list[str]:
    """列出 pipelines/ 目录下可用的流水线名（yaml 文件 stem）"""
    pipelines_dir = Path(__file__).parent / "pipelines"
    if not pipelines_dir.exists():
        return []
    return sorted(p.stem for p in pipelines_dir.glob("*.yaml"))


def _resolve_pipeline_plan(args_args: argparse.Namespace, orch: PipelineOrchestrator,
                           config: dict) -> tuple[Any, bool]:
    """尝试从 YAML 文件解析 pipeline plan；失败时返回 (None, False) 表示应走 legacy 路径"""
    from pipeline_core.scheduler import LockfileMismatchError, Scheduler
    sched = Scheduler()
    base_dir = Path(__file__).parent
    pipeline_files = []
    if args_args.pipeline_file:
        pipeline_files = [Path(args_args.pipeline_file)]
    else:
        pipelines_dir = base_dir / "pipelines"
        pipeline_files = sorted(pipelines_dir.glob(f"{args_args.pipeline}*.yaml"))
        if not pipeline_files:
            available = ", ".join(_available_pipeline_names())
            print(f"[run] ERROR: 未找到流水线 '{args_args.pipeline}'"
                  f"（pipelines/ 下可用: {available or '无'}）", file=sys.stderr)
            sys.exit(2)

    write_lock = bool(getattr(args_args, "write_lock", False))
    for pf in pipeline_files:
        if pf.exists():
            try:
                plan = sched.parse_file(str(pf), verify_lock=not write_lock)
            except LockfileMismatchError as e:
                print(f"[run] ERROR: {pf.name} 与版本锁定不一致，已阻止执行:\n{e}", file=sys.stderr)
                print("[run] 提示: 若配置变更是有意的，使用 --write-lock 重新生成 lockfile 后再运行",
                      file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"[run] 加载 {pf.name} 失败: {e}")
                continue
            print(f"[run] 加载流水线配置: {pf.name}")
            if write_lock:
                lock_path = sched.generate_lockfile(plan)
                agent_count = len({n.agent_name.split("_pool_")[0]
                                   for level in plan.levels for n in level})
                print(f"[run] 已写入版本锁定: {lock_path}"
                      f"（pipeline={plan.pipeline_name}, agents={agent_count}, nodes={plan.node_count}）")
            return (plan, True)
    return (None, False)


_TASK_EXIT_CODES = {"failed": 1, "cancelled": 2}


def _task_exit_code(task) -> int:
    """按任务终态映射进程退出码：failed=1、cancelled=2、其余=0"""
    try:
        status = task.status.value if hasattr(task.status, "value") else str(task.status)
    except Exception:
        return 0
    return _TASK_EXIT_CODES.get(str(status).lower(), 0)


def _print_task_summary(args_args: argparse.Namespace, orch: PipelineOrchestrator,
                        task_id: str, use_legacy: bool) -> None:
    """打印任务摘要横幅"""
    print(f"\n{'='*60}")
    print(f"任务: {task_id}")
    print(f"流水线: {args_args.pipeline}")
    print(f"输入: {args_args.input}")
    print(f"模式: {'声明式 DAG' if not use_legacy else 'Legacy'}")
    print(f"Agent: {', '.join(orch.registry.list_agent_names())}")
    if args_args.queries:
        print(f"查询: {args_args.queries}")
    if args_args.resume:
        print("模式: 断点续传")
    print(f"{'='*60}\n")


def _poll_task_progress(orch: PipelineOrchestrator, args_args: argparse.Namespace,
                        task, task_id: str) -> bool:
    """轮询任务进度直到结束。返回 False 表示被中断（已暂停/进入守护等待）。

    中断且 --daemon 时保持进程存活（Admin API 常驻），再次 Ctrl+C 退出。
    """
    import time as time_module
    try:
        from pipeline_core import TaskStatus
        while task.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
            time_module.sleep(1)
            print(f"[run] 进度: {task.progress}%  状态: {task.status.value}")
    except KeyboardInterrupt:
        print("\n\n[run] 收到中断信号，正在暂停任务...")
        orch.pause(task_id)
        print("[run] 任务已暂停，可使用 --resume 续传")
        if args_args.daemon:
            print("\n[run] 守护进程模式激活，按 Ctrl+C 再次退出")
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    time_module.sleep(10)
        return False
    return True


def _collect_steps(task) -> list[dict]:
    """将 StepResult 列表转为可 JSON 序列化的 dict 列表"""
    steps = []
    if task.steps:
        for step in task.steps:
            steps.append({
                "step_name": step.step_name,
                "agent_name": step.agent_name,
                "status": step.status,
                "duration_ms": step.duration_ms,
                "started_at": step.started_at,
                "finished_at": step.finished_at,
                "result": step.result if hasattr(step, 'result') else {}
            })
    return steps


def _resolve_output_path(args_args: argparse.Namespace, task) -> str:
    """确定输出文档路径：CLI 指定 > 任务结果 > 任务属性"""
    if args_args.output and Path(args_args.output).exists():
        return str(args_args.output)
    if task.result and isinstance(task.result, dict) and task.result.get("output_path"):
        return str(task.result["output_path"])
    if hasattr(task, 'output_path') and task.output_path:
        return str(task.output_path)
    return ""


def _render_task_result(args_args: argparse.Namespace, task, task_id: str) -> None:
    """渲染最终结果：JSON 输出或人类可读报告 + ASCII 修复 + 格式导出"""
    base_dir = Path(__file__).parent

    steps = _collect_steps(task)
    output_path = _resolve_output_path(args_args, task)

    if args_args.json_output:
        output_json_result(task, output_path, steps, task.status.value)
        return

    print(f"\n{'='*60}")
    print(f"流水线执行完成 | 状态: {task.status.value}")
    if task.finished_at and task.started_at:
        with contextlib.suppress(TypeError, ValueError):
            duration = float(task.finished_at) - float(task.started_at)
            print(f"耗时: {duration:.1f}s")

    if task.steps:
        print("\n执行步骤:")
        for step in task.steps:
            status_icon = "✅" if step.status == "success" else ("❌" if step.status == "failed" else "⏭️")
            print(f"  {status_icon} {step.agent_name:20s} {step.duration_ms:8.1f}ms")

    if task.error:
        print(f"\n错误: {task.error}")

    # 降级警告：writer 报告了内容不足的章节时，stderr 显式提示
    writer_result = (task.result or {}).get("writer") or {}
    empty_sections = (writer_result.get("stats") or {}).get("empty_sections") or []
    if empty_sections:
        print(f"\n⚠️ WARNING: {len(empty_sections)} 个章节内容不足（降级占位）："
              f"{'、'.join(empty_sections)}；建议配置 LLM API Key 后重新生成",
              file=sys.stderr)

    # 质量门控警告：重做耗尽仍不达标时任务仍为 DONE，必须在此显式呈现，
    # 否则用户拿到低分文档却只看到"执行完成"（此前仅存在于日志）
    qg_result = (task.result or {}).get("quality_gate") or {}
    if isinstance(qg_result, dict) and qg_result.get("status") == "accepted_with_warnings":
        print(f"\n⚠️ WARNING: 质量门控经 {qg_result.get('generation_count', '?')} 轮重做后仍未达标"
              f"（得分 {qg_result.get('overall_score', '?')}，低于阈值），"
              f"文档以带警告状态发布。各维度得分: {qg_result.get('scores', {})}",
              file=sys.stderr)

    print(f"{'='*60}")

    if args_args.report:
        report_file = base_dir / "checkpoints" / f"report_{task_id}.json"
        if report_file.exists():
            print(f"\n详细报告已保存: {report_file}")

    if args_args.fix_ascii and output_path:
        _run_ascii_fix(output_path)

    if args_args.export and output_path:
        _run_export(output_path, args_args.export, args_args.export_output)


def _run_single_task(args_args: argparse.Namespace, orch: PipelineOrchestrator,
                     config: dict):
    """执行单任务：构建 plan/task、等待结果、输出报告。返回终态 task（预览/中断返回 None）"""
    task_id = args_args.task_id or new_task_id()

    use_legacy = args_args.legacy
    plan = None

    if not use_legacy:
        plan, plan_loaded = _resolve_pipeline_plan(args_args, orch, config)
        if not plan_loaded:
            use_legacy = True

    # 预览模式
    if args_args.plan:
        if plan:
            from pipeline_core.scheduler import Scheduler
            sched = Scheduler()
            print(sched.visualize(plan))
        else:
            try:
                plan_preview = orch.plan(args_args.pipeline, args_args.input, config)
                print(orch.visualize_plan(plan_preview))
            except Exception as e:
                print(f"[run] ✗ 无法生成预览: {e}")
                sys.exit(1)
        return

    _print_task_summary(args_args, orch, task_id, use_legacy)

    run_config = {
        "timeout": args_args.timeout,
        "output": args_args.output,
        "queries": args_args.queries or [],
        **config
    }

    if use_legacy:
        task = orch.run(
            task_id=task_id,
            pipeline_name=args_args.pipeline,
            input_file=args_args.input,
            config=run_config,
            wait=not args_args.dry_run,
            resume=args_args.resume,
        )
    else:
        if plan is None:
            print(f"[run] ✗ 无法加载流水线配置（pipeline={args_args.pipeline}）")
            print("[run] 请检查 pipelines/ 目录或 --pipeline-file 指定的 YAML 文件")
            print("[run] 或使用 --legacy 模式绕过 YAML 配置")
            sys.exit(1)
        if args_args.output:
            plan.raw.setdefault("pipeline", {})["output"] = args_args.output
        task = orch.run_plan(
            plan=plan,
            input_file=args_args.input,
            task_id=task_id,
            wait=not args_args.dry_run,
        )

    if args_args.dry_run:
        print(f"[run] 计划执行（dry-run），任务ID: {task_id}")
        return

    if not _poll_task_progress(orch, args_args, task, task_id):
        return None

    _render_task_result(args_args, task, task_id)
    return task


def _run_daemon(orch: PipelineOrchestrator) -> None:
    """守护进程模式：保持 Admin API 常驻直到收到 KeyboardInterrupt"""
    print("\n[run] 守护进程模式 — 按 Ctrl+C 退出")
    with contextlib.suppress(KeyboardInterrupt):
        import time
        while True:
            time.sleep(10)


def build_arg_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器（含输入校验规则）"""
    parser = argparse.ArgumentParser(
        description=f"文档生成流水线 v{__version__} - 声明式 DAG + 自动重做",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 运行完整流水线 (自动发现 pipeline YAML)
  python run.py docs/README.md

  # 指定 pipeline 配置文件
  python run.py docs/README.md --pipeline-file pipelines/docgen.yaml

  # 从断点续传
  python run.py docs/README.md --task-id mytask --resume

  # 旧模式（已冻结，仅兜底：按 Agent 注册元数据执行，不经 Scheduler/YAML）
  python run.py docs/README.md --legacy
        """
    )

    parser.add_argument("input", nargs="?", default=None, help="输入文件")
    parser.add_argument("--task-id", "-t", default=None, help="任务ID（默认自动生成）")
    _pipeline_names = _available_pipeline_names()
    parser.add_argument("--pipeline", "-p", default="docgen",
                        choices=_pipeline_names or None,
                        help="流水线名称（可选值来自 pipelines/ 目录）")
    parser.add_argument("--pipeline-file", "-f", default=None, help="pipeline YAML 配置路径")
    parser.add_argument("--queries", "-q", nargs="*", help="检索词（多个）")
    parser.add_argument("--agent", "-a", action="append", help="指定运行的 Agent")
    parser.add_argument("--list-agents", "-l", action="store_true", help="列出所有 Agent")
    parser.add_argument("--plan", action="store_true", help="仅预览执行计划")
    parser.add_argument("--dry-run", "-n", action="store_true", help="仅计划，不执行")
    parser.add_argument("--resume", action="store_true", help="从断点续传")
    parser.add_argument("--timeout", default=600, type=int, help="超时（秒）")
    parser.add_argument("--output", "-o", default=None, help="输出文件")
    parser.add_argument("--report", action="store_true", help="生成详细报告")
    parser.add_argument("--config", "-c", help="配置文件路径")
    parser.add_argument("--json-output", action="store_true", help="输出 JSON 结果到 stdout（供 wrapper 解析）")
    parser.add_argument("--legacy", action="store_true",
                        help="（已冻结，仅兜底）旧模式：直接按 Agent 注册元数据执行，"
                             "不经过 Scheduler/YAML。生产请使用默认 DAG 模式")
    parser.add_argument("--write-lock", action="store_true",
                        help="加载流水线后重新生成 pipelines/<name>.lock（覆盖），然后继续执行")
    parser.add_argument("--admin", action="store_true", help="启动管理 API 服务")
    parser.add_argument("--dashboard", action="store_true", help="启动管理 API + 仪表盘（隐含 --admin）")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式：流水线执行完后保持 Admin API 常驻")
    parser.add_argument("--check", action="store_true", help="运行启动自检后退出")
    parser.add_argument("--three-pass", action="store_true", help="（已移除）三阶段流水线已并入 DAG 模式，此参数保留仅为兼容报错")
    parser.add_argument("--health-check", action="store_true", help="启动时运行 LLM 健康检查")
    parser.add_argument("--export", choices=["html", "word", "png"], help="导出格式（html/word/png）")
    parser.add_argument("--export-output", default=None, help="导出文件路径")
    parser.add_argument("--fix-ascii", action="store_true", help="将文档中的 ASCII 图转换为 Mermaid 图")
    parser.add_argument("--enhance", action="store_true", help="增强已有文档（逐章节 LLM 深化 + 搜索补充）")
    parser.add_argument("--enhance-output", default=None, help="增强输出目录")
    parser.add_argument("--no-search", action="store_true", help="禁用搜索补充（仅 LLM 增强）")
    parser.add_argument("--mcp", action="store_true", help="启动 MCP server（stdio JSON-RPC，供 AI agent 调度）")
    parser.add_argument("--recover", action="store_true", help="恢复中断的任务（重启后把 running 改回 pending 并重新执行）")
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    # 无输入文件时：仅 --check/--mcp/--recover 及服务模式（admin/dashboard/daemon）可用
    if args.input is None and not args.check and not args.mcp and not args.recover \
            and not args.admin and not args.dashboard and not args.daemon:
        parser.error('需要指定输入文件，或使用 --check 运行启动自检')

    print_banner()

    # ─── 启动自检 ──────────────────────────────
    if args.check:
        from pipeline_core.bootstrap import run_startup_check
        report = run_startup_check(run_health_check=args.health_check)
        print(report.summary())
        sys.exit(1 if report.has_errors else 0)

    # ─── MCP server 模式 ──────────────────────
    if args.mcp:
        from pipeline_core.mcp_server import run_mcp_server
        run_mcp_server()
        return

    # ─── 恢复中断任务 ──────────────────────
    if args.recover:
        project_root = Path(__file__).parent
        orch = _get_orchestrator(project_root)
        orch.register_agents()
        recovered = orch.recover_tasks()
        if not recovered:
            print("[recover] 没有需要恢复的中断任务")
            sys.exit(0)
        print(f"[recover] 发现 {len(recovered)} 个中断任务，开始恢复...")
        from pipeline_core.scheduler import Scheduler
        sched = Scheduler()
        for t in recovered:
            print(f"  → 恢复任务 {t['task_id']} (pipeline={t['pipeline_name']}, input={t['input_file']})")
            try:
                plan = sched.parse(t["pipeline_name"])
                task = orch.run_plan(plan, input_file=t["input_file"],
                                     task_id=t["task_id"], wait=False)
                print(f"    已重新启动，新状态: {task.status.value}")
            except Exception as e:
                print(f"    恢复失败: {e}")
        print(f"[recover] 恢复完成，{len(recovered)} 个任务已重新提交")
        orch.shutdown()
        return

    # ─── 三阶段流水线模式（已移除，提供迁移指引）──────────
    if args.three_pass:
        print("[run] ERROR: --three-pass 已在 v3.6.0 移除（ThreePassPipeline 已废弃）。"
              "其能力已由 DAG 模式覆盖且更完善：python run.py <input> --pipeline docgen",
              file=sys.stderr)
        sys.exit(2)

    # 快速自检（非阻塞，仅警告）
    try:
        from pipeline_core.bootstrap import quick_check
        if not quick_check():
            print("[run] 启动自检发现错误，使用 --check 查看详情")
    except Exception:
        pass  # 自检失败不阻断启动

    # ─── 文档增强模式 ──────────────────────────
    if args.enhance:
        from pipeline_core.document_enhancer import DocumentEnhancer
        output_dir = args.enhance_output or "output"
        with_search = not args.no_search
        print(f"\n[enhance] 开始增强文档: {args.input}")
        print(f"[enhance] 搜索补充: {'启用' if with_search else '禁用'}")
        enhancer = DocumentEnhancer()
        result = enhancer.enhance(
            args.input,
            output_dir=output_dir,
            with_search=with_search,
        )
        print(f"\n{'='*60}")
        print(f"文档增强完成 | 状态: {result['status']}")
        print(f"耗时: {result['duration']:.1f}s")
        stats = result.get("stats", {})
        print(f"章节: {stats.get('sections', 0)} | 增强: {stats.get('enhanced', 0)} | 搜索: {stats.get('searched', 0)} | ASCII修复: {stats.get('ascii_fixed', 0)}")
        print(f"输出: {result.get('output_path', '')}")
        print(f"{'='*60}")
        output = result.get("output_path", "")
        if output and args.fix_ascii:
            _run_ascii_fix(output)
        if output and args.export:
            _run_export(output, args.export, args.export_output)
        return

    # ─── 正常流水线执行 ──────────────────────
    project_root = Path(__file__).parent
    orch = _get_orchestrator(project_root)
    config = _load_config(args, project_root)
    loaded = orch.register_agents(agent_names=args.agent or None, config=config)

    if args.list_agents:
        print(f"\n已注册 {len(loaded)} 个 Agent:")
        print("-" * 60)
        for name in loaded:
            meta = orch.registry.get(name)
            if meta:
                status = orch.registry.get_status(name).value
                print(f"  {name:20s} v{meta['version']:6s} [{status:12s}] {meta.get('description', '')[:40]}")
        print()
        return

    if not loaded:
        print("[run] 没有加载任何 Agent，请检查 agents 目录")
        return

    # 纯服务模式：无输入文件，仅常驻 Admin API（任务经 POST /api/tasks 提交）
    service_only = args.input is None
    finished_task = None
    if not service_only:
        finished_task = _run_single_task(args, orch, config)

    # ─── 管理 API / 仪表盘 / 守护进程 ──────────────
    want_server = args.admin or args.dashboard or args.daemon or service_only
    if want_server:
        # admin_api.host/port 可由配置文件覆盖（config.production.json 等）；
        # 未配置时默认仅本机回环绑定
        admin_cfg = config.get("admin_api") or {}
        host = str(admin_cfg.get("host", "127.0.0.1"))
        try:
            port = int(admin_cfg.get("port", 8910))
        except (TypeError, ValueError):
            print(f"[run] ERROR: admin_api.port 配置非法: {admin_cfg.get('port')!r}",
                  file=sys.stderr)
            sys.exit(1)
        if args.admin or args.dashboard:
            dashboard_dir = str(project_root / "dashboard") if args.dashboard else None
            ok = orch.start_admin_api(
                host=host,
                port=port,
                serve_static=args.dashboard,
                dashboard_dir=dashboard_dir,
            )
        else:
            # --daemon / 纯服务模式未显式开启 admin 时自动补启
            ok = orch.start_admin_api(host=host, port=port)
        if not ok:
            print("[run] ERROR: Admin API 启动失败（详见上方日志；"
                  "非本机绑定需设置 ADMIN_API_KEY）", file=sys.stderr)
            sys.exit(1)
        print(f"[run] 管理 API: http://{host}:{port}")
        if args.dashboard:
            print(f"[run] 仪表盘:  http://{host}:{port}/index.html")
        if args.daemon or service_only:
            _run_daemon(orch)

    orch.shutdown()

    # 任务终态映射退出码（failed=1 / cancelled=2），服务模式与既有语义不受影响
    exit_code = _task_exit_code(finished_task) if finished_task is not None else 0
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    _install_sigterm_handler()
    main()
