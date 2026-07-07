#!/usr/bin/env python3
"""
run.py - 文档生成流水线入口 v3
=============================
改进点：
  - 支持声明式流水线配置 (YAML) via Scheduler + run_plan
  - DAG 并行执行 + 自动重做循环 + 熔断器
  - 完整审计日志 + 事务 checkpoint
"""
import argparse
import sys
import os
import json
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from pipeline_core import PipelineOrchestrator, TaskStatus
from pipeline_core.scheduler import Scheduler


def print_banner():
    print("""
╔══════════════════════════════════════════════════════════════╗
║          Doc-Pipeline v3.0 - 文档生成流水线                   ║
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


def main():
    parser = argparse.ArgumentParser(
        description="文档生成流水线 v3.0 - 声明式 DAG + 自动重做",
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

    parser.add_argument("input", help="输入文件")
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

    args = parser.parse_args()

    print_banner()

    # 加载配置
    config = {}
    if args.config and Path(args.config).exists():
        with open(args.config, "r", encoding="utf-8") as f:
            config = json.load(f)

    # 初始化编排器
    base_dir = Path(__file__).parent
    orch = PipelineOrchestrator(
        agents_dir=str(base_dir / "agents"),
        checkpoint_dir=str(base_dir / "checkpoints")
    )

    # 发现并注册 Agent
    agent_names = args.agent or None
    loaded = orch.register_agents(agent_names)

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
            plan_preview = orch.plan(args.pipeline, args.input, config)
            print(orch.visualize_plan(plan_preview))
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
        print(f"模式: 断点续传")
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
        print(f"[run] 管理 API: http://127.0.0.1:8910")
        if args.dashboard:
            print(f"[run] 仪表盘:  http://127.0.0.1:8910/index.html")

    # 等待结果
    import time as time_module
    try:
        while task.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
            time_module.sleep(1)
            print(f"\r[run] 进度: {task.progress}%  状态: {task.status.value}", end="", flush=True)
    except KeyboardInterrupt:
        print("\n\n[run] 收到中断信号，正在暂停任务...")
        orch.pause(task_id)
        print(f"[run] 任务已暂停，可使用 --resume 续传")
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
            print(f"\n执行步骤:")
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

    # 守护进程模式：任务完成后保持 Admin API 常驻
    if args.daemon:
        use_admin = args.admin or args.dashboard
        if not use_admin:
            # 自动启动 admin API（无 dashboard）
            orch.start_admin_api(port=8910)
            print(f"[run] 管理 API: http://127.0.0.1:8910")
        print("\n[run] 守护进程模式 — 按 Ctrl+C 退出")
        try:
            while True:
                time_module.sleep(10)
        except KeyboardInterrupt:
            pass

    orch.shutdown()


if __name__ == "__main__":
    main()