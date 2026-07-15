"""
PipelineOrchestrator v3.1 - 增强型流水线编排器
=============================================
核心特性：
  - DAG 并行执行（拓扑排序层级调度）
  - 断点续传支持（checkpoint + 恢复）
  - 自动重做（QualityGate 反馈循环）
  - Per-agent 熔断器 + 令牌桶限流
  - 指数退避重试（带 jitter）
  - 结构化日志 + Prometheus 指标 + 审计轨迹
  - 临时文件自动清理
  - Admin REST API + Dashboard
"""
from __future__ import annotations

import json
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, Callable
from enum import Enum

from .observability import StructuredLogger, MetricsRegistry, get_logger, get_metrics


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"      # 新增：暂停状态（断点续传）
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StepResult:
    """步骤执行结果"""
    step_name: str
    agent_name: str
    status: str  # success / failed / skipped
    started_at: float
    finished_at: float = 0  # 默认 0，finally 中会更新
    result: dict = field(default_factory=dict)
    error: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000


@dataclass
class TaskNode:
    """DAG 任务节点"""
    name: str                          # 节点名称（对应 agent 名称）
    agent_name: str                    # 执行的 Agent
    dependencies: list[str] = field(default_factory=list)  # 依赖的节点名
    payload: dict = field(default_factory=dict)             # 执行载荷
    max_workers: int = 1               # 并行度（>1 时可拆分子任务）
    retry_count: int = 0               # 当前重试次数
    max_retries: int = 3               # 最大重试次数
    timeout: float = 300               # 超时时间（秒）
    rate_limit: dict = field(default_factory=dict)  # 限流配置

    # 运行时状态
    status: str = "pending"            # pending/running/success/failed/skipped
    result: dict = field(default_factory=dict)
    error: str = ""
    started_at: float = 0
    finished_at: float = 0
    attempts: int = 0


@dataclass
class PipelineTask:
    """增强型流水线任务"""
    id: str
    pipeline_name: str
    input_file: str
    config: dict
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    steps: list[StepResult] = field(default_factory=list)
    current_step: int = 0
    result: dict = field(default_factory=dict)
    started_at: float = 0
    finished_at: float = 0
    error: str = ""
    checkpoint_file: Optional[str] = None  # 断点文件路径
    # DAG 执行相关
    dag_nodes: dict[str, TaskNode] = field(default_factory=dict)  # name -> TaskNode
    execution_order: list[list[str]] = field(default_factory=list)  # 拓扑层级
    # Per-task 取消事件（替代全局 _stop_event 实现精确取消）
    stop_event: threading.Event = field(default_factory=threading.Event)
    # Per-task 结果锁（保护 result dict 的多线程写入）
    result_lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pipeline": self.pipeline_name,
            "input": self.input_file,
            "status": self.status.value,
            "progress": self.progress,
            "steps": [
                {
                    "name": s.step_name,
                    "agent": s.agent_name,
                    "status": s.status,
                    "duration_ms": round(s.duration_ms, 2),
                    "error": s.error,
                }
                for s in self.steps
            ],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": round(self.finished_at - self.started_at, 2) if self.finished_at else None,
            "error": self.error,
        }


class PipelineOrchestrator:
    """增强型流水线编排器"""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "bus" and hasattr(self, "_executor"):
            self._executor.bus = value

    def __init__(self, agents_dir: str = "agents", checkpoint_dir: str = "checkpoints"):
        # 统一配置中心
        from .config import ConfigCenter
        self.config = ConfigCenter(
            config_file=str(Path(__file__).parent.parent / "config.yaml"),
            auto_reload=True,
        )

        # 延迟导入避免循环依赖
        from .message_bus_v3 import MessageBus
        from .registry import Registry
        from .agent_loader import AgentLoader
        from .dag_executor import DAGExecutor
        from .checkpoint_manager import CheckpointManager
        from .circuit_breaker import CircuitBreakerRegistry
        from .rate_limiter import RateLimiterRegistry

        self.bus = MessageBus()
        self.registry = Registry()
        self.agents_dir = Path(self.config.get("agents.dir", agents_dir))
        self.checkpoint_dir = Path(self.config.get("checkpoint.dir", checkpoint_dir))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        self._running_tasks: dict[str, PipelineTask] = {}
        self._task_callbacks: dict[str, list[Callable]] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        # 可观测性
        self._logger = get_logger()
        self._metrics = get_metrics()

        # ── 组件化 ──
        self._loader = AgentLoader(self.registry, self.bus, agents_dir, self._logger)
        self._checkpoint = CheckpointManager(checkpoint_dir, self._logger)

        # 熔断器注册中心
        self._cb_registry = CircuitBreakerRegistry()

        # 限流器注册中心
        self._rate_limiters = RateLimiterRegistry()

        self._executor = DAGExecutor(
            self.registry, self.bus,
            self._cb_registry, self._rate_limiters,
            self._metrics, self._logger,
            stop_event=self._stop_event,
            checkpoint_save_fn=self._checkpoint.save,
            audit_log_fn=self._audit_log,
        )

        self._admin_api = None

        # 性能统计
        self._execution_stats: list[dict] = []
        self._max_task_history: int = 100

        # 上次执行的计划和输入（用于 rerun）
        self.last_plan = None
        self.last_input = None

    # ─── 委托：限流 ─────────────────────────────

    def _acquire_rate_limit(self, agent_name: str, rate_limit_cfg: Optional[dict] = None,
                             timeout: float = 30.0) -> bool:
        return self._executor._acquire_rate_limit(agent_name, rate_limit_cfg, timeout)

    # ─── 委托：Agent 加载 ─────────────────────────────

    def discover_agents(self) -> list[str]:
        return self._loader.discover()

    def register_agents(self, agent_names: Optional[list[str]] = None, config: Optional[dict] = None) -> list[str]:
        return self._loader.register(agent_names, config)

    def _extract_meta(self, cls) -> "AgentMeta":
        return self._loader._extract_meta(cls)

    # ─── 执行计划 ─────────────────────────────

    def plan(self, pipeline_name: str, input_file: str,
             config: Optional[dict] = None) -> list[dict]:
        """生成执行计划（预览）"""
        agent_order = self.registry.deps_order()
        plan = []

        for i, name in enumerate(agent_order):
            meta = self.registry.get_meta(name)
            if meta:
                desc = meta.description or ""
                plan.append({
                    "step": i + 1,
                    "agent": name,
                    "priority": meta.priority,
                    "dependencies": meta.dependencies,
                    "description": desc[:50],
                })

        return plan

    def visualize_plan(self, plan: list[dict]) -> str:
        """可视化执行计划"""
        lines = ["执行计划:", "=" * 60]

        for step in plan:
            deps = ", ".join(step["dependencies"]) if step["dependencies"] else "-"
            lines.append(f"  Step {step['step']}: {step['agent']}")
            lines.append(f"    优先级: {step['priority']}")
            lines.append(f"    依赖: {deps}")
            lines.append(f"    描述: {step['description']}")
            lines.append("")

        return "\n".join(lines)

    # ─── 委托：DAG 构建与执行 ─────────────────────────────

    def _build_dag(self, agent_order: list[str], config: Optional[dict] = None) -> tuple[dict[str, TaskNode], list[list[str]]]:
        return self._executor.build_dag(agent_order, config)

    def _execute_level(self, task: PipelineTask, level: list, input_file: str,
                       plan, executor: ThreadPoolExecutor) -> bool:
        return self._executor.execute_level(task, level, input_file, plan, executor)

    def _get_task_output(self, task: PipelineTask, key: str, default=None):
        return self._executor._get_task_output(task, key, default)

    def _set_task_output(self, task: PipelineTask, key: str, value, dag_node: bool = True):
        self._executor._set_task_output(task, key, value, dag_node)

    def _get_latest_content(self, task: PipelineTask, current_deps: list[str] = None) -> str:
        return self._executor._get_latest_content(task, current_deps)

    def _get_dep_list_results(self, task: PipelineTask, deps: list[str], key: str = "results") -> list:
        return self._executor._get_dep_list_results(task, deps, key)

    def _execute_node(self, task: PipelineTask, node: TaskNode, input_file: str, config: dict) -> dict:
        return self._executor.execute_node(task, node, input_file, config)

    def _execute_node_from_scheduler(self, task: PipelineTask, node: "ExecutionNode",
                                     input_file: str, plan: ExecutionPlan) -> dict:
        return self._executor.execute_node_from_scheduler(task, node, input_file, plan)

    def _extract_queries(self, input_file: str, node: "ExecutionNode") -> list[str]:
        return self._executor._extract_queries(input_file, node)

    def _backoff_delay(self, backoff: str, initial_delay: float, attempt: int) -> float:
        return self._executor._backoff_delay(backoff, initial_delay, attempt)

    def _circuit_breaker(self, node: ExecutionNode, task: PipelineTask) -> bool:
        return self._executor._circuit_breaker(node, task)

    def _circuit_breaker_success(self, node):
        self._executor._circuit_breaker_success(node)

    # ─── DAG 并行执行（内部） ─────────────────────────────

    def _run_dag_parallel(self, task: PipelineTask, input_file: str, config: dict) -> bool:
        """并行执行 DAG"""
        from types import SimpleNamespace

        agent_order = self.registry.deps_order()
        nodes, execution_order = self._build_dag(agent_order, config)

        task.dag_nodes = nodes
        task.execution_order = execution_order
        total_nodes = len(nodes)
        completed = 0

        cb_cfg = config.get("circuit_breaker", {}) if config else {}

        # 使用单个线程池执行整个 DAG，避免重复创建/销毁
        max_workers = max(len(l) for l in execution_order) if execution_order else 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for level_idx, level_names in enumerate(execution_order):
                # 将 TaskNode 转换为 ExecutionNode 兼容的 SimpleNamespace
                level_nodes = []
                for name in level_names:
                    node = nodes[name]
                    exec_node = SimpleNamespace(
                        agent_name=node.agent_name,
                        timeout=node.timeout,
                        max_retries=node.max_retries,
                        rate_limit=node.rate_limit,
                        agent_config=SimpleNamespace(
                            config=config,
                            pool_size=1,
                            rate_limit=node.rate_limit,
                            circuit_breaker=cb_cfg,
                        ),
                        dependencies=node.dependencies,
                        backoff="exponential",
                        initial_delay=1.0,
                    )
                    level_nodes.append(exec_node)

                plan_ns = SimpleNamespace(
                    pipeline_name=task.pipeline_name,
                    checkpoint=config if isinstance(config, dict) else {},
                    fail_fast=config.get("fail_fast", True) if isinstance(config, dict) else True,
                    raw=config if isinstance(config, dict) else {},
                )

                if not self._execute_level(task, level_nodes, input_file, plan_ns, executor):
                    return False

                completed += len(level_names)
                task.current_step = completed
                task.progress = int((completed / total_nodes) * 100)

        return True

    # ─── 流水线执行 ─────────────────────────────

    def run(self, task_id: Optional[str] = None, pipeline_name: str = "",
            input_file: str = "", config: Optional[dict] = None,
            wait: bool = True, resume: bool = False) -> PipelineTask:
        """执行流水线（支持 DAG 并行）"""
        from .registry import AgentStatus

        task_id = task_id or str(uuid.uuid4())[:8]

        # 检查是否可以断点续传
        if resume:
            task = self._load_checkpoint(task_id)
            if task:
                self._logger.log("info", "从断点恢复", task_id=task_id)
            else:
                task = PipelineTask(task_id, pipeline_name, input_file, config or {})
        else:
            task = PipelineTask(task_id, pipeline_name, input_file, config or {})

        task.checkpoint_file = str(self.checkpoint_dir / f"{task_id}.json")

        with self._lock:
            self._running_tasks[task.id] = task

        task.status = TaskStatus.RUNNING
        task.started_at = time.time()

        self.bus.publish("pipeline.started", "orchestrator", {
            "task_id": task.id,
            "pipeline": pipeline_name,
            "input_file": input_file,
            "config": config,
        })

        def run_steps():
            try:
                # 使用 DAG 并行执行
                success = self._run_dag_parallel(task, input_file, config or {})

                # 完成
                if task.status != TaskStatus.FAILED and task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.DONE
                task.progress = 100
                task.finished_at = time.time()

                # 生成报告
                self._generate_report(task)

                # 通知回调
                self._notify_callbacks(task)

                self.bus.publish("pipeline.finished", "orchestrator", {
                    "task_id": task.id,
                    "status": task.status.value,
                    "result": task.result,
                    "error": task.error,
                    "duration": task.finished_at - task.started_at,
                })

                # 清理断点
                if task.status == TaskStatus.DONE:
                    self._remove_checkpoint(task.id)

            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.finished_at = time.time()
                self._logger.log("error", "任务执行异常", error=str(e))
            finally:
                with self._lock:
                    self._trim_task_history()

        if wait:
            run_steps()
            return task
        else:
            threading.Thread(target=run_steps, daemon=True).start()
            return task

    def pause(self, task_id: str) -> bool:
        """暂停任务（支持断点续传）"""
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.PAUSED
                self._save_checkpoint(task)
                # 通知所有 agent 暂停
                for name in self.registry.list_agent_names():
                    inst = self.registry.get_instance(name)
                    if inst:
                        try:
                            inst.on_pause()
                        except Exception as e:
                            self._log("warning", f"agent {name} on_pause 失败", error=str(e))
                return True
        return False

    def cancel(self, task_id: str) -> bool:
        """取消任务（per-task，不影响其他正在运行的任务）"""
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status in [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED]:
                task.status = TaskStatus.CANCELLED
                task.stop_event.set()  # per-task 取消信号
                return True
        return False

    def resume(self, task_id: str) -> bool:
        """恢复暂停的任务（从断点续传）"""
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status == TaskStatus.PAUSED:
                task.status = TaskStatus.RUNNING
                # 通知所有 agent 恢复
                for name in self.registry.list_agent_names():
                    inst = self.registry.get_instance(name)
                    if inst:
                        try:
                            inst.on_resume()
                        except Exception as e:
                            self._log("warning", f"agent {name} on_resume 失败", error=str(e))
                self._log("info", "Pipeline resumed", task_id=task_id)
                return True
        return False

    # ─── 委托：断点续传 ─────────────────────────────

    def _save_checkpoint(self, task: PipelineTask, full_state: bool = False):
        # 收集 agent 快照
        agent_snapshots = {}
        for name in self.registry.list_agent_names():
            inst = self.registry.get_instance(name)
            if inst:
                try:
                    agent_snapshots[name] = inst.on_snapshot()
                except Exception as e:
                    self._log("warning", f"agent {name} on_snapshot 失败", error=str(e))
        self._checkpoint.save(task, full_state, agent_snapshots)

    def _load_checkpoint(self, task_id: str) -> Optional[PipelineTask]:
        task, agent_snapshots = self._checkpoint.load(task_id)
        if task is not None and agent_snapshots:
            # 恢复 agent 快照
            for name, state in agent_snapshots.items():
                inst = self.registry.get_instance(name)
                if inst:
                    try:
                        inst.on_restore(state)
                    except Exception as e:
                        self._log("warning", f"agent {name} on_restore 失败", error=str(e))
        return task

    def _remove_checkpoint(self, task_id: str):
        self._checkpoint.remove(task_id)

    def _cleanup_old_checkpoints(self, max_age_days: int = 7):
        self._checkpoint.cleanup_old(max_age_days)

    # ─── 事务回滚 ─────────────────────────────

    def _rollback_task(self, task: PipelineTask, snapshot: dict) -> bool:
        """从快照回滚 task 状态（事务恢复）"""
        try:
            task.result = dict(snapshot.get("result", {}))
            task.steps = list(snapshot.get("steps", []))
            task.error = snapshot.get("error", "")
            task.status = TaskStatus.RUNNING
            task.progress = snapshot.get("progress", 0)
            task.current_step = snapshot.get("current_step", 0)
            self._logger.log("info", "已回滚", current_step=task.current_step, total_steps=len(task.steps))
            return True
        except Exception as e:
            self._logger.log("error", "回滚失败", error=str(e))
            return False

    def _snapshot_state(self, task: PipelineTask) -> dict:
        """获取当前任务状态快照（用于事务回滚）"""
        return {
            "result": dict(task.result),
            "steps": [
                {
                    "step_name": s.step_name,
                    "agent_name": s.agent_name,
                    "status": s.status,
                    "error": s.error,
                    "started_at": s.started_at,
                    "finished_at": s.finished_at,
                }
                for s in task.steps
            ],
            "error": task.error,
            "progress": task.progress,
            "current_step": task.current_step,
        }

    # ─── 报告和回调 ─────────────────────────────

    def run_plan(self, plan: ExecutionPlan, input_file: str = "",
                task_id: Optional[str] = None, wait: bool = True) -> PipelineTask:
        """按 Scheduler 生成的 ExecutionPlan 执行流水线"""
        self.last_plan = plan
        self.last_input = input_file

        from .scheduler import ExecutionPlan as EP
        if not isinstance(plan, EP):
            raise TypeError("run_plan 需要 ExecutionPlan 实例")

        task_id = task_id or str(uuid.uuid4())[:8]
        task = PipelineTask(
            id=task_id,
            pipeline_name=plan.pipeline_name,
            input_file=input_file,
            config=plan.raw.get("pipeline", {}),
            status=TaskStatus.RUNNING,
        )
        task.checkpoint_file = str(self.checkpoint_dir / f"{task_id}.json")

        with self._lock:
            self._running_tasks[task.id] = task

        task.started_at = time.time()
        total_nodes = plan.node_count

        self._log("info", "Pipeline started",
                  task_id=task.id, pipeline=plan.pipeline_name,
                  input_file=input_file, plan_id=plan.plan_id,
                  total_nodes=total_nodes)

        self.bus.publish("pipeline.started", "orchestrator", {
            "task_id": task.id,
            "pipeline": plan.pipeline_name,
            "input_file": input_file,
            "plan_id": plan.plan_id,
        })

        def execute():
            try:
                completed = 0

                # 逐层执行，保持 Scheduler 定义的并行度
                for level_idx, level in enumerate(plan.levels):
                    if task.stop_event.is_set() or self._stop_event.is_set():
                        task.status = TaskStatus.CANCELLED
                        return

                    # 暂停检查点：任务被暂停时阻塞，直到恢复或取消
                    while task.status == TaskStatus.PAUSED:
                        if task.stop_event.is_set() or self._stop_event.is_set():
                            task.status = TaskStatus.CANCELLED
                            return
                        task.stop_event.wait(0.5)
                    if task.status == TaskStatus.CANCELLED:
                        return

                    # ── 事务边界：快照当前状态 ──
                    level_snapshot = self._snapshot_state(task)

                    max_workers = max(
                        (node.agent_config.parallelism.get("max_workers", 1) for node in level),
                        default=1,
                    )

                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        # 创建 dag_nodes 供 _execute_level 使用
                        for node in level:
                            dag_node = TaskNode(
                                name=node.agent_name,
                                agent_name=node.agent_name,
                                dependencies=node.dependencies,
                                timeout=node.timeout,
                                max_retries=node.max_retries,
                            )
                            dag_node.attempts = 0
                            task.dag_nodes[node.agent_name] = dag_node

                        if not self._execute_level(task, level, input_file, plan, executor):
                            return

                    completed += len(level)
                    task.current_step = completed
                    task.progress = int((completed / total_nodes) * 100)

                    # ── 池化节点结果合并 ──
                    merged = set()
                    for key in list(task.result.keys()):
                        if "_pool_" in key:
                            base = key.split("_pool_")[0]
                            if base not in merged:
                                merged.add(base)
                                # 从所有 pool 实例合并结果
                                pooled_results = [
                                    self._get_task_output(task, k) for k in task.result
                                    if k.startswith(f"{base}_pool_") and self._get_task_output(task, k)
                                ]
                                if pooled_results:
                                    # 合并 researcher 的 results 列表
                                    meta = self.registry.get_meta(base)
                                    if getattr(meta, "results_merge", "") == "extend":
                                        combined = {"status": "ok", "task_id": task.id,
                                                     "total": 0, "results": [],
                                                     "query_count": 0, "engines_used": []}
                                        for pr in pooled_results:
                                            if isinstance(pr, dict):
                                                combined["results"].extend(pr.get("results", []))
                                                combined["total"] = len(combined["results"])
                                                combined["query_count"] += pr.get("query_count", 0)
                                                eng = pr.get("engines_used", [])
                                                combined["engines_used"] = list(
                                                    set(combined["engines_used"]) | set(eng)
                                                )
                                        self._set_task_output(task, base, combined)
                                    else:
                                        # 非 researcher 池：取第一个非空结果
                                        for pr in pooled_results:
                                            if pr:
                                                self._set_task_output(task, base, pr)
                                                break

                    # ── 事务提交：level 完成后保存完整状态 ──
                    self._save_checkpoint(task, full_state=True)

                    if task.status == TaskStatus.FAILED:
                        return

                if task.status != TaskStatus.FAILED and task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.DONE

            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)

            finally:
                task.progress = 100 if task.status == TaskStatus.DONE else task.progress
                task.finished_at = time.time()
                self._log("info", f"Pipeline {task.status.value}",
                          task_id=task.id, pipeline=plan.pipeline_name,
                          status=task.status.value, duration_sec=round(task.finished_at - task.started_at, 2),
                          error=task.error or "",
                          steps=len(task.steps))
                self._generate_report(task)
                self._notify_callbacks(task)

                self.bus.publish("pipeline.finished", "orchestrator", {
                    "task_id": task.id,
                    "status": task.status.value,
                    "result": task.result,
                    "error": task.error,
                    "duration": task.finished_at - task.started_at,
                    "plan_id": plan.plan_id,
                })

                self._cleanup_task_temp(task)

                if task.status == TaskStatus.DONE and not plan.checkpoint.get("keep_on_success", False):
                    self._remove_checkpoint(task.id)

                with self._lock:
                    self._trim_task_history()

        if wait:
            execute()
        else:
            threading.Thread(target=execute, daemon=True).start()

        return task

    def _generate_report(self, task: PipelineTask):
        """生成执行报告"""
        report_file = self.checkpoint_dir / f"report_{task.id}.json"
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._logger.log("error", "生成报告失败", error=str(e))

    def on_complete(self, task_id: str, callback: Callable[[PipelineTask], None]):
        """注册完成回调"""
        with self._lock:
            if task_id not in self._task_callbacks:
                self._task_callbacks[task_id] = []
            self._task_callbacks[task_id].append(callback)

    def _notify_callbacks(self, task: PipelineTask):
        """通知回调"""
        with self._lock:
            callbacks = self._task_callbacks.get(task.id, [])

        for callback in callbacks:
            try:
                callback(task)
            except Exception as e:
                self._logger.log("error", "回调执行失败", error=str(e))

    # ─── Audit Log ─────────────────────────────

    def _audit_log(self, task_id: str, agent_name: str,
                   input_summary: dict, output_summary: dict,
                   duration_ms: float, status: str, error: str = ""):
        """记录 agent 调用的完整审计轨迹"""
        try:
            entry = {
                "task_id": task_id,
                "agent": agent_name,
                "status": status,
                "duration_ms": round(duration_ms, 2),
                "error": error,
                "input_keys": list(input_summary.keys()) if input_summary else [],
                "output_keys": list(output_summary.keys()) if output_summary else [],
                "input_size": len(json.dumps(input_summary, default=str)) if input_summary else 0,
                "output_size": len(json.dumps(output_summary, default=str)) if output_summary else 0,
                "timestamp": time.time(),
            }
            # 将审计记录 publish 到 audit topic（可通过订阅者持久化）
            self.bus.publish("pipeline.audit", "orchestrator", entry)
        except Exception as e:
            self._logger.log("error", "Audit log 失败", error=str(e))

    # ─── 查询 ─────────────────────────────

    def get_task(self, task_id: str) -> Optional[PipelineTask]:
        return self._running_tasks.get(task_id)

    def replay_dlq(self, dlq_id: int) -> Optional[dict]:
        """重放一条死信：重新执行故障 node 并回填结果

        支持两种死信：
        - REQUEST 消息（agent 故障）：重新 bus.request 执行该 node，回填 task.result
        - EVENT 消息：直接广播回原 topic
        返回重放结果 dict；若 dlq_id 不存在返回 None。
        """
        if not self.bus._store:
            return None
        entry = self.bus._store.get_dlq_entry(dlq_id)
        if not entry:
            return None

        payload = entry["payload"]          # Message.to_dict()
        topic = entry["topic"]               # 外层 topic（如 writer.input）
        inner = payload.get("payload", {})   # 原消息的业务 payload

        # 更新重放计数
        self.bus._store.replay_dlq(dlq_id)

        # 判断消息类型：REQUEST 由 orchestrator 重执行，EVENT 由总线广播
        msg_type = payload.get("msg_type", "event")
        if msg_type == "request" or topic.endswith(".input"):
            node_name = inner.get("node") or payload.get("to_agent") or topic.replace(".input", "")
            task_id = inner.get("task_id", "")
            # 真实自愈：直接重新执行故障 node 的 agent（与正常执行路径一致）
            result = None
            instance = self.registry.get_instance(node_name)
            if instance is not None:
                try:
                    from .message_bus_v3 import Message, MessageType
                    msg = Message(
                        topic=topic,
                        payload=inner,
                        msg_type=MessageType.REQUEST,
                        from_agent=payload.get("from_agent", "orchestrator.replay"),
                        to_agent=node_name,
                        correlation_id=payload.get("correlation_id", ""),
                        trace_id=payload.get("trace_id", ""),
                    )
                    result = instance.handle(msg)
                except Exception as e:
                    self._logger.error(f"replay node {node_name} 失败: {e}")
                    return {"node": node_name, "task_id": task_id, "error": str(e)}
            else:
                # fallback：路由到总线（若已注册 subscriber）
                result = self.bus.request(
                    topic=topic, from_a="orchestrator.replay",
                    to_a=node_name, payload=inner, timeout=300,
                )
            # 回填 task.result（若 task 仍在内存）
            with self._lock:
                task = self._running_tasks.get(task_id)
                if task and isinstance(result, dict):
                    self._set_task_output(task, node_name, result)
            # 广播完成事件，让依赖节点可继续
            self.bus.publish(f"{node_name}.done", "orchestrator.replay", result)
            return {"node": node_name, "task_id": task_id, "result": result}

        # EVENT 类型：直接广播回原 topic
        original_payload = payload.get("payload", payload)
        from_agent = payload.get("from_agent", "dlq_replay")
        self.bus.publish(topic, from_agent, original_payload)
        return {"topic": topic, "replayed": True}

    def list_tasks(self, status: Optional[TaskStatus] = None) -> list[PipelineTask]:
        tasks = list(self._running_tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_execution_stats(self) -> list[dict]:
        return self._execution_stats

    def rerun(self, pipeline_name: str = "", input_file: str = "",
              task_id: Optional[str] = None) -> PipelineTask:
        """用上一次执行的 plan + 输入重新跑一遍"""
        if self.last_plan is None:
            # 没有内存中的 plan，尝试从 pipeline_name 重建
            if not pipeline_name:
                raise RuntimeError("没有可重用的执行计划，请指定 pipeline_name")
            from .scheduler import Scheduler
            sched = Scheduler()
            plan = sched.parse(pipeline_name)
            input_file = input_file or "test_input.md"
        else:
            plan = self.last_plan
            input_file = input_file or self.last_input

        self._logger.log("info", "重新执行流水线", pipeline=plan.pipeline_name, input=input_file)
        return self.run_plan(plan, input_file=input_file, task_id=task_id, wait=False)

    def shutdown(self):
        """关闭编排器"""
        self._stop_event.set()
        self._cleanup_all_stale_temp(max_age_hours=24)
        self.bus.shutdown()
        self.registry.shutdown()
        self.stop_admin_api()
        self._cleanup_old_checkpoints(max_age_days=7)

    def _trim_task_history(self):
        """限制 _running_tasks 历史大小，防止内存无限增长。
        仅在锁内调用。保留正在运行的任务和最近完成的任务。"""
        if len(self._running_tasks) <= self._max_task_history:
            return
        completed = sorted(
            [(tid, t) for tid, t in self._running_tasks.items()
             if t.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)],
            key=lambda x: x[1].finished_at or 0
        )
        excess = len(self._running_tasks) - self._max_task_history
        for tid, _ in completed[:excess]:
            self._running_tasks.pop(tid, None)

    def _cleanup_task_temp(self, task: PipelineTask):
        """任务完成后清理各 agent 产生的临时文件"""
        try:
            for agent_name in ("fetcher",):
                instance = self.registry.get_instance(agent_name)
                if instance and hasattr(instance, "cleanup_task_temp"):
                    try:
                        instance.cleanup_task_temp(task.id)
                    except Exception as e:
                        self._log("warning", f"清理 {agent_name} 临时文件失败", task_id=task.id, error=str(e))
        except Exception as e:
            self._log("warning", "任务临时文件清理异常", task_id=task.id, error=str(e))

    def _cleanup_all_stale_temp(self, max_age_hours: int = 24):
        """清理所有 agent 的过期临时文件"""
        for agent_name in ("fetcher",):
            instance = self.registry.get_instance(agent_name)
            if instance and hasattr(instance, "cleanup_stale_temp"):
                try:
                    instance.cleanup_stale_temp(max_age_hours)
                except Exception as e:
                    self._log("warning", f"清理 {agent_name} 过期临时文件失败", error=str(e))

    def start_admin_api(self, host: str = "127.0.0.1", port: int = 8910,
                         serve_static: bool = False,
                         dashboard_dir: Optional[str] = None) -> bool:
        """启动管理 API"""
        from .admin_api import AdminAPI
        self._admin_api = AdminAPI(host=host, port=port,
                                   serve_static=serve_static,
                                   dashboard_dir=dashboard_dir)
        return self._admin_api.start(self)

    def stop_admin_api(self):
        if self._admin_api:
            self._admin_api.stop()
            self._admin_api = None

    def _log(self, level: str, msg: str, **kw):
        """结构化日志记录"""
        self._logger.log(level, msg, **kw)

    def __repr__(self):
        agents = self.registry.list()
        tasks = len(self._running_tasks)
        return f"<PipelineOrchestrator agents={len(agents)} tasks={tasks}>"