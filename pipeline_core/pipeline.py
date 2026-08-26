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

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .event_hook import emit_event
from .executor_factory import create_executor
from .observability import get_logger, get_metrics

if TYPE_CHECKING:
    from .registry import AgentMeta
    from .scheduler import ExecutionNode, ExecutionPlan


_REDACT_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)")


def _redact_config(config):
    """递归脱敏配置副本：敏感键的叶子值替换为 ***redacted***，不影响原 config 对象"""
    if not isinstance(config, dict):
        return config
    redacted = {}
    for key, value in config.items():
        if isinstance(value, dict):
            redacted[key] = _redact_config(value)
        elif isinstance(value, list):
            redacted[key] = [
                _redact_config(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif _REDACT_KEY_RE.search(str(key)):
            redacted[key] = "***redacted***"
        else:
            redacted[key] = value
    return redacted


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
class NodeConfig:
    """节点配置（替代 SimpleNamespace，提供类型安全的 agent_config）"""
    config: dict = field(default_factory=dict)
    pool_size: int = 1
    rate_limit: dict = field(default_factory=dict)
    circuit_breaker: dict = field(default_factory=dict)
    parallelism: dict = field(default_factory=dict)


@dataclass
class TaskNode:
    """DAG 任务节点（统一模型，兼容 ExecutionNode 接口）

    合并了原 TaskNode（运行时状态）和 ExecutionNode（计划配置）的职责，
    消除 SimpleNamespace 中间转换层。
    """
    name: str                          # 节点名称（对应 agent 名称）
    agent_name: str                    # 执行的 Agent
    dependencies: list[str] = field(default_factory=list)  # 依赖的节点名
    payload: dict = field(default_factory=dict)             # 执行载荷
    max_workers: int = 1               # 并行度（>1 时可拆分子任务）
    retry_count: int = 0               # 当前重试次数
    max_retries: int = 3               # 最大重试次数
    timeout: float = 300               # 超时时间（秒）
    rate_limit: dict = field(default_factory=dict)  # 限流配置
    # 原 ExecutionNode 字段（统一后无需 SimpleNamespace 转换）
    backoff: str = "exponential"       # 退避策略: exponential | linear | fixed
    initial_delay: float = 1.0         # 初始退避延迟（秒）
    agent_config: NodeConfig = field(default_factory=lambda: NodeConfig())

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
    checkpoint_file: str | None = None  # 断点文件路径
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

    # pickle 支持：PipelineTask 含 threading.Event / threading.Lock（均不可序列化），
    # __getstate__ 剥离同步原语，__setstate__ 重建全新实例。
    # 注意：重建的 stop_event 是反序列化侧的独立副本——父进程 cancel() 设置的信号
    # 不会传播到子进程（Python 多进程同步原语只能通过继承共享，无法经 pickle 传递）。
    # 进程模式（executor_type=process）下节点在子进程中会因 registry/bus 未重建
    # （DAGExecutor.from_config 未接线）而失败，重试在父进程执行、stop_event 有效；
    # 见 dag_executor._execute_node_worker 与 executor_factory.create_executor 的告警。
    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state["stop_event"] = None
        state["result_lock"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        # 反序列化时重建同步原语（新进程内独立实例）
        if self.__dict__.get("stop_event") is None:
            self.__dict__["stop_event"] = threading.Event()
        if self.__dict__.get("result_lock") is None:
            self.__dict__["result_lock"] = threading.Lock()


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
            config_file=str(Path(__file__).parent.parent / "config.json"),
            auto_reload=True,
        )

        # 延迟导入避免循环依赖
        from .agent_loader import AgentLoader
        from .checkpoint_manager import CheckpointManager
        from .circuit_breaker import CircuitBreakerRegistry
        from .dag_executor import DAGExecutor
        from .message_bus_v3 import MessageBus
        from .rate_limiter import RateLimiterRegistry
        from .registry import Registry

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

        # 持久化任务队列
        from .task_queue import TaskQueue
        self.task_queue = TaskQueue()

        # 性能统计
        self._execution_stats: list[dict] = []
        self._max_task_history: int = 100

        # 上次执行的计划和输入（用于 rerun）
        self.last_plan = None
        self.last_input = None

    # ─── 委托：限流 ─────────────────────────────

    def _acquire_rate_limit(self, agent_name: str, rate_limit_cfg: dict | None = None,
                             timeout: float = 30.0) -> bool:
        return self._executor._acquire_rate_limit(agent_name, rate_limit_cfg, timeout)

    # ─── 委托：Agent 加载 ─────────────────────────────

    def discover_agents(self) -> list[str]:
        return self._loader.discover()

    def register_agents(self, agent_names: list[str] | None = None, config: dict | None = None) -> list[str]:
        loaded = self._loader.register(agent_names, config)
        if loaded:
            # 记录注册信息，供 process 执行模式在子进程内重建上下文使用
            # （见 dag_executor._get_child_context）
            self._executor.child_context = {
                "agents_dir": str(self.agents_dir),
                "agent_names": list(loaded),
                "config": config or {},
            }
        return loaded

    def _extract_meta(self, cls) -> AgentMeta:
        return self._loader._extract_meta(cls)

    # ─── 执行计划 ─────────────────────────────

    def plan(self, pipeline_name: str, input_file: str,
             config: dict | None = None) -> list[dict]:
        """生成执行计划（预览）"""
        agent_order = self.registry.deps_order()
        plan = []

        for i, name in enumerate(agent_order):  # type: ignore[var-annotated]
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

    # ─── 委托：DAG 构建与执行（通过 _executor 直接访问） ─────────────────────────────
    # 注：内部方法通过 self._executor 直接调用，无需逐一包装

    def _build_dag(self, agent_order: list[str], config: dict | None = None) -> tuple[dict[str, TaskNode], list[list[str]]]:
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

    def _execute_node_from_scheduler(self, task: PipelineTask, node: ExecutionNode,
                                     input_file: str, plan: ExecutionPlan) -> dict:
        return self._executor.execute_node_from_scheduler(task, node, input_file, plan)

    def _extract_queries(self, input_file: str, node: ExecutionNode) -> list[str]:
        return self._executor._extract_queries(input_file, node)

    def _backoff_delay(self, backoff: str, initial_delay: float, attempt: int) -> float:
        return self._executor._backoff_delay(backoff, initial_delay, attempt)

    def _circuit_breaker(self, node: ExecutionNode, task: PipelineTask) -> bool:
        return self._executor._circuit_breaker(node, task)

    def _circuit_breaker_success(self, node):
        self._executor._circuit_breaker_success(node)

    # ─── DAG 并行执行（内部） ─────────────────────────────

    def _run_dag_parallel(self, task: PipelineTask, input_file: str, config: dict) -> bool:
        """并行执行 DAG（统一节点模型，无 SimpleNamespace 转换）"""
        from types import SimpleNamespace

        agent_order = self.registry.deps_order()
        nodes, execution_order = self._build_dag(agent_order, config)

        task.dag_nodes = nodes
        task.execution_order = execution_order
        total_nodes = len(nodes)
        completed = 0

        cb_cfg = config.get("circuit_breaker", {}) if config else {}

        # 为每个 TaskNode 填充 agent_config（统一模型，无需 SimpleNamespace）
        for _name, node in nodes.items():
            node.agent_config = NodeConfig(
                config=config or {},
                pool_size=1,
                rate_limit=node.rate_limit,
                circuit_breaker=cb_cfg,
            )

        # 使用执行器工厂：支持 thread（默认）或 process（多进程水平扩展）
        executor_type = (config or {}).get("executor_type", "thread")
        max_workers = max(len(level) for level in execution_order) if execution_order else 1
        with create_executor(max_workers=max_workers, executor_type=executor_type) as executor:
            for _level_idx, level_names in enumerate(execution_order):
                # 直接使用 TaskNode（已兼容 ExecutionNode 接口）
                level_nodes = [nodes[name] for name in level_names if name in nodes]

                plan_ns = SimpleNamespace(
                    pipeline_name=task.pipeline_name,
                    checkpoint=config if isinstance(config, dict) else {},
                    fail_fast=config.get("fail_fast", True) if isinstance(config, dict) else True,
                    raw=config if isinstance(config, dict) else {},
                )

                if not self._execute_level(task, level_nodes, input_file, plan_ns, executor):  # type: ignore[arg-type]
                    return False

                completed += len(level_names)
                task.current_step = completed
                task.progress = int((completed / total_nodes) * 100)

        return True

    # ─── 流水线执行 ─────────────────────────────

    def run(self, task_id: str | None = None, pipeline_name: str = "",
            input_file: str = "", config: dict | None = None,
            wait: bool = True, resume: bool = False) -> PipelineTask:
        """执行流水线（支持 DAG 并行）"""

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

        # 修复 P0：原代码引用未定义变量 `plan`（NameError）。
        # run() 接收的是 pipeline_name + config，而非 ExecutionPlan；
        # 用方法参数直接提交任务队列。
        self.task_queue.submit(task.id, pipeline_name, input_file,
                               (config or {}).get("pipeline", {}))

        task.started_at = time.time()

        emit_event("task.created", {"task_id": task.id, "pipeline": pipeline_name, "input_file": input_file})
        emit_event("task.started", {"task_id": task.id, "pipeline": pipeline_name, "started_at": task.started_at})

        self.bus.publish("pipeline.started", "orchestrator", {
            "task_id": task.id,
            "pipeline": pipeline_name,
            "input_file": input_file,
            "config": _redact_config(config),
        })

        def run_steps():
            try:
                # 使用 DAG 并行执行
                self._run_dag_parallel(task, input_file, config or {})

                # 完成
                if task.status != TaskStatus.FAILED and task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.DONE
                task.progress = 100 if task.status == TaskStatus.DONE else task.progress
                task.finished_at = time.time()

                # ── 事件钩子 ──
                if task.status == TaskStatus.DONE:
                    emit_event("task.completed", {"task_id": task.id, "pipeline": pipeline_name,
                                 "duration": task.finished_at - task.started_at, "result_keys": list(task.result.keys())})
                elif task.status == TaskStatus.FAILED:
                    emit_event("task.failed", {"task_id": task.id, "pipeline": pipeline_name,
                               "error": task.error, "duration": task.finished_at - task.started_at})
                elif task.status == TaskStatus.CANCELLED:
                    emit_event("task.cancelled", {"task_id": task.id, "pipeline": pipeline_name})

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

            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                task.finished_at = time.time()
                self._logger.log("error", "任务执行异常", error=str(e))
                emit_event("task.failed", {"task_id": task.id, "pipeline": pipeline_name,
                           "error": str(e), "duration": task.finished_at - task.started_at})
            finally:
                # 修复 P0：run_steps 的 finally 块缺失 task_queue.update_status、
                # _cleanup_task_temp、PAUSED 检查点保留逻辑，导致：
                #   1) 任务队列状态不同步（永远停在 running）
                #   2) 临时文件泄漏
                #   3) PAUSED 任务被误删断点无法续传
                # 对齐 run_plan() 的 finally 块（见本类 run_plan 方法）。
                try:
                    self.task_queue.update_status(
                        task.id,
                        task.status.value if hasattr(task.status, "value") else str(task.status),
                        result=dict(task.result) if task.result else None,
                        error=task.error,
                    )
                except Exception as e:
                    self._logger.log("warning", "task_queue.update_status 失败",
                                     task_id=task.id, error=str(e))

                # 清理临时文件（即使任务失败也需清理）
                self._cleanup_task_temp(task)

                # PAUSED 任务保留断点以支持续传；DONE 且未要求保留时删除断点
                if task.status == TaskStatus.PAUSED:
                    try:
                        self._save_checkpoint(task)
                    except Exception as e:
                        self._logger.log("warning", "保存 PAUSED 断点失败",
                                         task_id=task.id, error=str(e))
                elif task.status == TaskStatus.DONE:
                    keep = bool((config or {}).get("checkpoint", {}).get("keep_on_success", False))
                    if not keep:
                        self._remove_checkpoint(task.id)

                with self._lock:
                    self._trim_task_history()

        if wait:
            run_steps()
            return task
        else:
            threading.Thread(target=run_steps, daemon=True).start()
            return task

    def pause(self, task_id: str) -> bool:
        """暂停任务（在层级边界生效，支持断点续传）。

        语义说明：正在执行的 level 内节点不会被中断，执行器推进到下一 level 前
        检测到 PAUSED 即阻塞等待；边界 checkpoint 由执行循环在进入等待时保存
        （此时无并发节点写入，避免撕裂快照），pause() 本身不落盘。
        """
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.PAUSED
                # 通知所有 agent 暂停
                for name in self.registry.list_agent_names():  # type: ignore[attr-defined]
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
                emit_event("task.cancelled", {"task_id": task_id, "pipeline": task.pipeline_name})
                return True
        return False

    def resume(self, task_id: str) -> bool:
        """恢复暂停的任务（从断点续传）"""
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status == TaskStatus.PAUSED:
                task.status = TaskStatus.RUNNING
                # 通知所有 agent 恢复
                for name in self.registry.list_agent_names():  # type: ignore[attr-defined]
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
        for name in self.registry.list_agent_names():  # type: ignore[attr-defined]
            inst = self.registry.get_instance(name)
            if inst:
                try:
                    agent_snapshots[name] = inst.on_snapshot()
                except Exception as e:
                    self._log("warning", f"agent {name} on_snapshot 失败", error=str(e))
        self._checkpoint.save(task, full_state, agent_snapshots)

    def _load_checkpoint(self, task_id: str) -> PipelineTask | None:
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
        return task  # type: ignore[no-any-return]

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

    def _merge_pooled_results(self, task: PipelineTask) -> None:
        """池化节点结果合并：将 *_pool_* 实例键的结果按 agent 元信息聚合回基础名"""
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
                            combined: dict = {"status": "ok", "task_id": task.id,
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

    def _finalize_plan_task(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """计划任务收尾：进度/时间戳、队列状态、事件钩子、报告、临时文件与 checkpoint 清理"""
        task.progress = 100 if task.status == TaskStatus.DONE else task.progress
        task.finished_at = time.time()

        self.task_queue.update_status(
            task.id,
            task.status.value if hasattr(task.status, "value") else str(task.status),
            result=dict(task.result) if task.result else None,
            error=task.error,
        )

        # ── 事件钩子 ──
        if task.status == TaskStatus.DONE:
            emit_event("task.completed", {"task_id": task.id, "pipeline": plan.pipeline_name,
                         "duration": task.finished_at - task.started_at, "plan_id": plan.plan_id})
        elif task.status == TaskStatus.FAILED:
            emit_event("task.failed", {"task_id": task.id, "pipeline": plan.pipeline_name,
                       "error": task.error, "duration": task.finished_at - task.started_at, "plan_id": plan.plan_id})
        elif task.status == TaskStatus.CANCELLED:
            emit_event("task.cancelled", {"task_id": task.id, "pipeline": plan.pipeline_name, "plan_id": plan.plan_id})

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

    def run_plan(self, plan: ExecutionPlan, input_file: str = "",
                task_id: str | None = None, wait: bool = True) -> PipelineTask:
        """按 Scheduler 生成的 ExecutionPlan 执行流水线"""
        self.last_plan = plan  # type: ignore[assignment]
        self.last_input = input_file  # type: ignore[assignment]

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

        emit_event("task.created", {"task_id": task.id, "pipeline": plan.pipeline_name,
                     "input_file": input_file, "plan_id": plan.plan_id, "total_nodes": total_nodes})
        emit_event("task.started", {"task_id": task.id, "pipeline": plan.pipeline_name, "started_at": task.started_at})

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
                for _level_idx, level in enumerate(plan.levels):
                    if task.stop_event.is_set() or self._stop_event.is_set():
                        task.status = TaskStatus.CANCELLED
                        return

                    # 暂停检查点：任务被暂停时阻塞，直到恢复或取消；
                    # 进入等待时保存边界快照（此时上一 level 已完结、无并发节点写入）
                    paused_saved = False
                    while task.status == TaskStatus.PAUSED:
                        if not paused_saved:
                            try:
                                self._save_checkpoint(task)
                            except Exception as e:
                                self._log("warning", "暂停边界 checkpoint 保存失败",
                                          task_id=task.id, error=str(e))
                            paused_saved = True
                        if task.stop_event.is_set() or self._stop_event.is_set():
                            task.status = TaskStatus.CANCELLED
                            return
                        task.stop_event.wait(0.5)
                    if task.status == TaskStatus.CANCELLED:
                        return

                    # ── 事务边界：快照当前状态 ──
                    self._snapshot_state(task)

                    max_workers = max(
                        (node.agent_config.parallelism.get("max_workers", 1) for node in level),
                        default=1,
                    )

                    # 执行器类型从 pipeline 配置读取（默认 thread，可选 process）
                    executor_type = plan.raw.get("pipeline", {}).get("executor_type", "thread")
                    with create_executor(max_workers=max_workers, executor_type=executor_type) as executor:
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
                    self._merge_pooled_results(task)

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
                self._finalize_plan_task(task, plan)

        if wait:
            execute()
        else:
            threading.Thread(target=execute, daemon=True).start()

        return task

    def _finalize_plan_task_async(self, task: PipelineTask, plan: ExecutionPlan) -> None:
        """async 任务收尾：与 _finalize_plan_task 的差异是不更新 task_queue、不发事件钩子"""
        task.progress = 100 if task.status == TaskStatus.DONE else task.progress
        task.finished_at = time.time()
        self._log("info", f"Pipeline {task.status.value} (async)",
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

    async def run_plan_async(self, plan: ExecutionPlan, input_file: str = "",
                             task_id: str | None = None) -> PipelineTask:
        """async 版 run_plan：在已有事件循环中直接 await，消除 asyncio.run 嵌套开销。

        与 run_plan(wait=True) 的区别：
        - 用 asyncio.gather + asyncio.to_thread 替代 ThreadPoolExecutor
        - 可在 async 上下文（如 SSE handler）中直接 await，无需 asyncio.run()
        - 逐层执行逻辑、池化合并、断点续传、报告生成均与 run_plan 一致
        """
        self.last_plan = plan  # type: ignore[assignment]
        self.last_input = input_file  # type: ignore[assignment]

        from .scheduler import ExecutionPlan as EP
        if not isinstance(plan, EP):
            raise TypeError("run_plan_async 需要 ExecutionPlan 实例")

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

        self._log("info", "Pipeline started (async)",
                  task_id=task.id, pipeline=plan.pipeline_name,
                  input_file=input_file, plan_id=plan.plan_id,
                  total_nodes=total_nodes)

        self.bus.publish("pipeline.started", "orchestrator", {
            "task_id": task.id,
            "pipeline": plan.pipeline_name,
            "input_file": input_file,
            "plan_id": plan.plan_id,
        })

        try:
            completed = 0
            for _level_idx, level in enumerate(plan.levels):
                if task.stop_event.is_set() or self._stop_event.is_set():
                    task.status = TaskStatus.CANCELLED
                    break

                # 暂停边界：与同步版一致，进入等待时保存无并发写入的边界快照
                paused_saved = False
                while task.status == TaskStatus.PAUSED:
                    if not paused_saved:
                        try:
                            self._save_checkpoint(task)
                        except Exception as e:
                            self._log("warning", "暂停边界 checkpoint 保存失败",
                                      task_id=task.id, error=str(e))
                        paused_saved = True
                    if task.stop_event.is_set() or self._stop_event.is_set():
                        task.status = TaskStatus.CANCELLED
                        break
                    await asyncio.sleep(0.5)
                if task.status == TaskStatus.CANCELLED:
                    break

                self._snapshot_state(task)

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

                if not await self._executor.execute_level_async(task, level, input_file, plan):
                    break

                completed += len(level)
                task.current_step = completed
                task.progress = int((completed / total_nodes) * 100)

                # 池化节点结果合并（与 run_plan 共用同一实现）
                self._merge_pooled_results(task)

                self._save_checkpoint(task, full_state=True)

                if task.status == TaskStatus.FAILED:
                    break

            if task.status != TaskStatus.FAILED and task.status != TaskStatus.CANCELLED:
                task.status = TaskStatus.DONE

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)

        finally:
            self._finalize_plan_task_async(task, plan)

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

    def get_task(self, task_id: str) -> PipelineTask | None:
        return self._running_tasks.get(task_id)

    def recover_tasks(self) -> list[dict]:
        """恢复中断的任务：重启时把 running 状态改回 pending。

        返回被恢复的任务列表。调用方可选择重新执行：
            recovered = orch.recover_tasks()
            for t in recovered:
                plan = scheduler.parse(t["pipeline_name"])
                orch.run_plan(plan, input_file=t["input_file"], task_id=t["task_id"])

        内存侧同步：_running_tasks 中同 id 且仍为 RUNNING 的任务一并置为 PENDING，
        避免 get_task()/list_tasks() 返回与持久化队列不一致的旧状态
        （PAUSED/DONE 等其他状态不动）。
        """
        recovered = self.task_queue.recover()
        if not recovered:
            return recovered
        with self._lock:
            for entry in recovered:
                task = self._running_tasks.get(entry.get("task_id", ""))
                if task is not None and task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PENDING
        return recovered

    def list_queued_tasks(self, status: str = None) -> list[dict]:
        """列出持久化队列中的任务"""
        return self.task_queue.list_all(status=status)

    def replay_dlq(self, dlq_id: int) -> dict | None:
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

    def list_tasks(self, status: TaskStatus | None = None) -> list[PipelineTask]:
        tasks = list(self._running_tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_execution_stats(self) -> list[dict]:
        return self._execution_stats

    def rerun(self, pipeline_name: str = "", input_file: str = "",
              task_id: str | None = None) -> PipelineTask:
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
        """任务完成后清理各 agent 产生的临时文件。

        动态遍历所有已注册 agent，凡实现 cleanup_task_temp 的均调用
        （契约见 BaseAgent.cleanup_task_temp），不再硬编码单个 agent 名。
        """
        try:
            cleaned = 0
            for agent_name in self.registry.list_agent_names():
                instance = self.registry.get_instance(agent_name)
                if instance is None or not hasattr(instance, "cleanup_task_temp"):
                    continue
                try:
                    cleaned += int(instance.cleanup_task_temp(task.id) or 0)
                except Exception as e:
                    self._log("warning", f"清理 {agent_name} 临时文件失败",
                              task_id=task.id, error=str(e))
            if cleaned:
                self._log("info", "任务临时文件已清理", task_id=task.id, removed=cleaned)
        except Exception as e:
            self._log("warning", "任务临时文件清理异常", task_id=task.id, error=str(e))

    def _cleanup_all_stale_temp(self, max_age_hours: int = 24):
        """清理所有 agent 的过期临时文件（动态遍历所有已注册 agent）。"""
        try:
            cleaned = 0
            for agent_name in self.registry.list_agent_names():
                instance = self.registry.get_instance(agent_name)
                if instance is None or not hasattr(instance, "cleanup_stale_temp"):
                    continue
                try:
                    cleaned += int(instance.cleanup_stale_temp(max_age_hours) or 0)
                except Exception as e:
                    self._log("warning", f"清理 {agent_name} 过期临时文件失败", error=str(e))
            if cleaned:
                self._log("info", "过期临时文件已清理",
                          removed=cleaned, max_age_hours=max_age_hours)
        except Exception as e:
            self._log("warning", "过期临时文件清理异常", error=str(e))

    def start_admin_api(self, host: str = "127.0.0.1", port: int = 8910,
                         serve_static: bool = False,
                         dashboard_dir: str | None = None) -> bool:
        """启动管理 API"""
        from .admin_api import AdminAPI
        self._admin_api = AdminAPI(host=host, port=port,  # type: ignore[assignment]
                                   serve_static=serve_static,
                                   dashboard_dir=dashboard_dir)
        return self._admin_api.start(self)  # type: ignore[no-any-return,attr-defined]

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
