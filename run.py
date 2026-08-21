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
import time
import uuid
from pathlib import Path

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
from pipeline_core.scheduler import Scheduler  # noqa: E402


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


def main():
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

  # 旧模式（直接注册 Agent）
  python run.py docs/README.md --legacy
        """
    )

    parser.add_argument("input", nargs="?", default=None, help="输入文件")
    parser.add_argument("--task-id", "-t", default=None, help="任务ID（默认自动生成）")
    parser.add_argument("--pipeline", "-p", default="docgen", help="流水线名称")
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
    parser.add_argument("--legacy", action="store_true", help="使用旧模式（直接注册 Agent，不经过 Scheduler）")
    parser.add_argument("--admin", action="store_true", help="启动管理 API 服务")
    parser.add_argument("--dashboard", action="store_true", help="启动管理 API + 仪表盘（隐含 --admin）")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式：流水线执行完后保持 Admin API 常驻")
    parser.add_argument("--check", action="store_true", help="运行启动自检后退出")
    parser.add_argument("--three-pass", action="store_true", help="使用三阶段流水线（研究→结构→精修）")
    parser.add_argument("--health-check", action="store_true", help="启动时运行 LLM 健康检查")
    parser.add_argument("--export", choices=["html", "word", "png"], help="导出格式（html/word/png）")
    parser.add_argument("--export-output", default=None, help="导出文件路径")
    parser.add_argument("--fix-ascii", action="store_true", help="将文档中的 ASCII 图转换为 Mermaid 图")
    parser.add_argument("--enhance", action="store_true", help="增强已有文档（逐章节 LLM 深化 + 搜索补充）")
    parser.add_argument("--enhance-output", default=None, help="增强输出目录")
    parser.add_argument("--no-search", action="store_true", help="禁用搜索补充（仅 LLM 增强）")
    parser.add_argument("--mcp", action="store_true", help="启动 MCP server（stdio JSON-RPC，供 AI agent 调度）")
    parser.add_argument("--recover", action="store_true", help="恢复中断的任务（重启后把 running 改回 pending 并重新执行）")

    args = parser.parse_args()

    # 非 --check 模式需要输入文件
    if args.input is None and not args.check and not args.mcp and not args.recover:
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
        # P1 修复：移除局部 import（会遮蔽模块级 PipelineOrchestrator，导致其他分支 UnboundLocalError）
        project_root = Path(__file__).parent
        orch = PipelineOrchestrator(
            agents_dir=str(project_root / "agents"),
            checkpoint_dir=str(project_root / "checkpoints"),
        )
        orch.register_agents()
        recovered = orch.recover_tasks()
        if not recovered:
            print("[recover] 没有需要恢复的中断任务")
            sys.exit(0)
        print(f"[recover] 发现 {len(recovered)} 个中断任务，开始恢复...")
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

    # 快速自检（非阻塞，仅警告）
    try:
        from pipeline_core.bootstrap import quick_check
        if not quick_check():
            print("[run] 启动自检发现错误，使用 --check 查看详情")
    except Exception:
        pass  # 自检失败不阻断启动

    # ─── 三阶段流水线模式 ──────────────────────
    if args.three_pass:
        from pipeline_core.three_pass_pipeline import ThreePassPipeline
        # 从输入文件读取主题
        input_text = Path(args.input).read_text(encoding="utf-8").strip()
        if not input_text:
            print("[run] 输入文件为空")
            return
        output = args.output or f"output/three_pass_{int(time.time())}.md"
        pipeline = ThreePassPipeline()
        result = pipeline.generate(input_text, output_path=output)
        print(f"\n{'='*60}")
        print(f"三阶段流水线完成 | 状态: {result['status']}")
        print(f"耗时: {result.get('duration', 0):.1f}s")
        if result["status"] == "ok":
            phases = result.get("phases", {})
            for name, info in phases.items():
                print(f"  {name}: {info['status']} ({info['duration']:.1f}s)")
            print(f"输出: {result.get('output_path', '')}")
            print(f"章节: {result.get('section_count', 0)} | 长度: {result.get('content_length', 0)} 字符")
        else:
            print(f"错误: {result.get('error', '')}")
        print(f"{'='*60}")

        # ─── ASCII 图转换 + 格式导出 ──────────────
        if result["status"] == "ok" and output:
            if args.fix_ascii:
                _run_ascii_fix(output)
            if args.export:
                _run_export(output, args.export, args.export_output)

        return

    # ─── 文档增强模式 ──────────────────────────
    if args.enhance:
        from pipeline_core.document_enhancer import DocumentEnhancer
        input_path = args.input
        output_dir = args.enhance_output or "output"
        with_search = not args.no_search
        print(f"\n[enhance] 开始增强文档: {input_path}")
        print(f"[enhance] 搜索补充: {'启用' if with_search else '禁用'}")
        enhancer = DocumentEnhancer()
        result = enhancer.enhance(
            input_path,
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
        # ─── ASCII 修复 + 格式导出 ──────────────
        output = result.get("output_path", "")
        if output and args.fix_ascii:
            _run_ascii_fix(output)
        if output and args.export:
            _run_export(output, args.export, args.export_output)
        return

    # 加载配置：优先 --config/-c 指定文件；未指定时自动加载项目根目录 config.json
    # （README 快速体验命令不传 -c，若此处不加载，agent 将使用代码内默认值而非 config.json）
    config = {}
    config_path = args.config or (Path(__file__).parent / "config.json")
    if Path(config_path).exists():
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

    # 初始化编排器
    base_dir = Path(__file__).parent
    orch = PipelineOrchestrator(
        agents_dir=str(base_dir / "agents"),
        checkpoint_dir=str(base_dir / "checkpoints")
    )

    # 发现并注册 Agent
    agent_names = args.agent or None
    loaded = orch.register_agents(agent_names, config=config)

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

    # 生成任务ID
    task_id = args.task_id or str(uuid.uuid4())[:8]

    # ─── 路由：Legacy 模式 vs Pipeline YAML 模式 ─────────────
    use_legacy = args.legacy
    plan = None

    if not args.legacy:
        # 尝试加载 pipeline YAML
        pipeline_files = []
        if args.pipeline_file:
            pipeline_files = [Path(args.pipeline_file)]
        else:
            # 自动发现：pipelines/ 下匹配 pipeline 名称的 .yaml
            pipelines_dir = base_dir / "pipelines"
            if pipelines_dir.exists():
                pipeline_files = sorted(pipelines_dir.glob(f"{args.pipeline}*.yaml"))
                if not pipeline_files:
                    pipeline_files = sorted(pipelines_dir.glob("*.yaml"))

        for pf in pipeline_files:
            if pf.exists():
                try:
                    sched = Scheduler()
                    plan = sched.parse_file(str(pf))
                    use_legacy = False
                    print(f"[run] 加载流水线配置: {pf.name}")
                    break
                except Exception as e:
                    print(f"[run] 加载 {pf.name} 失败: {e}")

    # 预览
    if args.plan:
        if plan:
            print(sched.visualize(plan))
        else:
            # P1 修复：plan 为 None 时 sched 也可能未定义，使用 legacy 路径预览
            try:
                plan_preview = orch.plan(args.pipeline, args.input, config)
                print(orch.visualize_plan(plan_preview))
            except Exception as e:
                print(f"[run] ✗ 无法生成预览: {e}")
                sys.exit(1)
        return

    print(f"\n{'='*60}")
    print(f"任务: {task_id}")
    print(f"流水线: {args.pipeline}")
    print(f"输入: {args.input}")
    print(f"模式: {'声明式 DAG' if not use_legacy else 'Legacy'}")
    print(f"Agent: {', '.join(loaded)}")
    if args.queries:
        print(f"查询: {args.queries}")
    if args.resume:
        print("模式: 断点续传")
    print(f"{'='*60}\n")

    # 构建配置
    run_config = {
        "timeout": args.timeout,
        "output": args.output,
        "queries": args.queries or [],
        **config
    }

    if use_legacy:
        # ─── Legacy 路径 ──
        task = orch.run(
            task_id=task_id,
            pipeline_name=args.pipeline,
            input_file=args.input,
            config=run_config,
            wait=not args.dry_run,
            resume=args.resume
        )
    else:
        # ─── 声明式 Pipeline 路径 ──
        # P0 修复：plan 可能为 None（所有 YAML 加载失败），友好退出而非 AttributeError
        if plan is None:
            print(f"[run] ✗ 无法加载流水线配置（pipeline={args.pipeline}）")
            print("[run] 请检查 pipelines/ 目录或 --pipeline-file 指定的 YAML 文件")
            print("[run] 或使用 --legacy 模式绕过 YAML 配置")
            sys.exit(1)
        # 将 CLI --output 注入 pipeline 配置，供 safe_writer 使用
        if args.output:
            plan.raw.setdefault("pipeline", {})["output"] = args.output
        task = orch.run_plan(
            plan=plan,
            input_file=args.input,
            task_id=task_id,
            wait=not args.dry_run,
        )

    if args.dry_run:
        print(f"[run] 计划执行（dry-run），任务ID: {task_id}")
        return

    # 可选：启动管理 API + 仪表盘
    use_admin = args.admin or args.dashboard
    if use_admin:
        dashboard_dir = str(Path(__file__).parent / "dashboard") if args.dashboard else None
        orch.start_admin_api(
            port=8910,
            serve_static=args.dashboard,
            dashboard_dir=dashboard_dir,
        )
        print("[run] 管理 API: http://127.0.0.1:8910")
        if args.dashboard:
            print("[run] 仪表盘:  http://127.0.0.1:8910/index.html")

    # 等待结果
    import time as time_module
    try:
        while task.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
            time_module.sleep(1)
            print(f"\r[run] 进度: {task.progress}%  状态: {task.status.value}", end="", flush=True)
    except KeyboardInterrupt:
        print("\n\n[run] 收到中断信号，正在暂停任务...")
        orch.pause(task_id)
        print("[run] 任务已暂停，可使用 --resume 续传")
        # 守护进程模式：暂停后不退出，继续提供 API 服务
        if args.daemon:
            print("\n[run] 守护进程模式激活，按 Ctrl+C 再次退出")
            try:
                while True:
                    time_module.sleep(10)
            except KeyboardInterrupt:
                pass
        return

    print()

    # 准备步骤信息
    steps = []
    output_path = ""
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

    if args.output and Path(args.output).exists():
        output_path = args.output
    elif task.result and isinstance(task.result, dict) and task.result.get("output_path"):
        output_path = task.result["output_path"]
    elif hasattr(task, 'output_path') and task.output_path:
        output_path = task.output_path

    status = task.status.value

    if args.json_output:
        output_json_result(task, output_path, steps, status)
    else:
        print(f"\n{'='*60}")
        print(f"流水线执行完成 | 状态: {task.status.value}")
        if task.finished_at and task.started_at:
            duration = task.finished_at - task.started_at
            print(f"耗时: {duration:.1f}s")

        if task.steps:
            print("\n执行步骤:")
            for step in task.steps:
                status_icon = "✅" if step.status == "success" else ("❌" if step.status == "failed" else "⏭️")
                print(f"  {status_icon} {step.agent_name:20s} {step.duration_ms:8.1f}ms")

        if task.error:
            print(f"\n错误: {task.error}")

        print(f"{'='*60}")

        if args.report:
            report_file = base_dir / "checkpoints" / f"report_{task_id}.json"
            if report_file.exists():
                print(f"\n详细报告已保存: {report_file}")

        # ─── ASCII 图转换 ──────────────────────
        if args.fix_ascii and output_path:
            _run_ascii_fix(output_path)

        # ─── 格式导出 ──────────────────────────
        if args.export and output_path:
            _run_export(output_path, args.export, args.export_output)

    # 守护进程模式：任务完成后保持 Admin API 常驻
    if args.daemon:
        use_admin = args.admin or args.dashboard
        if not use_admin:
            # 自动启动 admin API（无 dashboard）
            ok = orch.start_admin_api(port=8910)
            if not ok:
                print("[run] ERROR: Admin API 启动失败，守护进程无法维持", file=sys.stderr)
                sys.exit(1)
            print("[run] 管理 API: http://127.0.0.1:8910")
        print("\n[run] 守护进程模式 — 按 Ctrl+C 退出")
        try:
            while True:
                time_module.sleep(10)
        except KeyboardInterrupt:
            pass

    orch.shutdown()


if __name__ == "__main__":
    _install_sigterm_handler()
    main()
