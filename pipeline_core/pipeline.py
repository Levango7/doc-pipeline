"""
PipelineOrchestrator v2 - 增强型流水线编排器
=========================================
改进点：
  - DAG 并行执行
  - 断点续传支持
  - 可视化执行流程
  - 详细的执行报告
  - 性能分析
"""
from __future__ import annotations

import os
import sys
import json
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, Callable
from enum import Enum

from .circuit_breaker import CircuitBreakerRegistry, backoff_with_jitter
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

    def __init__(self, agents_dir: str = "agents", checkpoint_dir: str = "checkpoints"):
        # 延迟导入避免循环依赖
        from .message_bus_v3 import MessageBus
        from .registry import Registry

        self.bus = MessageBus()
        self.registry = Registry()
        self.agents_dir = Path(agents_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        self._running_tasks: dict[str, PipelineTask] = {}
        self._task_callbacks: dict[str, list[Callable]] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        # 熔断器注册中心
        self._cb_registry = CircuitBreakerRegistry()

        # 可观测性
        self._logger = get_logger()
        self._metrics = get_metrics()
        self._admin_api = None

        # 性能统计
        self._execution_stats: list[dict] = []

    # ─── Agent 加载 ─────────────────────────────

    def discover_agents(self) -> list[str]:
        """自动发现 agents 目录下的插件"""
        discovered = []
        if not self.agents_dir.exists():
            return discovered

        for f in self.agents_dir.glob("*.py"):
            if f.stem.startswith("_"):
                continue
            discovered.append(f.stem)
        return discovered

    def register_agents(self, agent_names: Optional[list[str]] = None, config: Optional[dict] = None) -> list[str]:
        """注册 Agent 插件"""
        from .base_agent import BaseAgent
        from .registry import AgentMeta

        names = agent_names or self.discover_agents()
        loaded = []

        for name in names:
            try:
                # 动态导入
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    f"agents.{name}",
                    self.agents_dir / f"{name}.py"
                )
                mod = importlib.util.module_from_spec(spec)
                # 必须先注册到 sys.modules，这样 _extract_meta 才能找到模块属性
                sys.modules[f"agents.{name}"] = mod
                spec.loader.exec_module(mod)

                # 找 Agent 类
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (isinstance(attr, type)
                        and issubclass(attr, BaseAgent)
                        and attr_name != "BaseAgent"):

                        # 提取元信息
                        meta = self._extract_meta(attr)

                        # 实例化
                        agent = attr(
                            name=meta.name,
                            meta=meta,
                            config=config or {},
                            message_bus=self.bus,
                            registry=self.registry
                        )
                        # 保存实例配置到 meta，用于 respawn 恢复
                        meta.config = config or {}

                        self.registry.register(meta, agent)
                        print(f"[Orchestrator] 注册: {meta.name} v{meta.version}")
                        loaded.append(meta.name)
                        break

            except Exception as e:
                print(f"[Orchestrator] 加载失败 {name}: {e}")

        return loaded

    def _extract_meta(self, cls) -> "AgentMeta":
        """从类属性提取 AgentMeta"""
        from .registry import AgentMeta

        # 模块级 AGENT_NAME 不会被 getattr(cls) 找到（它定义在模块而非类体）
        # 用 cls.__name__ 作为 fallback，并清理常见后缀
        cls_name = cls.__name__.replace("Agent", "").lower()
        
        # 优先从模块获取 AGENT_NAME（避免 BaseAgent 的 "base" 被继承）
        module = sys.modules.get(cls.__module__)
        if module and hasattr(module, "AGENT_NAME"):
            agent_name = module.AGENT_NAME
        else:
            raw_name = getattr(cls, "AGENT_NAME", cls_name)
            agent_name = raw_name if raw_name != "base" else cls_name

        # 尝试从模块获取属性（模块级定义的 INPUT_TOPICS 等）
        if module:
            input_topics = getattr(module, "INPUT_TOPICS", getattr(cls, "INPUT_TOPICS", []))
            output_topics = getattr(module, "OUTPUT_TOPICS", getattr(cls, "OUTPUT_TOPICS", []))
            dependencies = getattr(module, "DEPENDENCIES", getattr(cls, "DEPENDENCIES", []))
            cache_ttl = getattr(module, "CACHE_TTL", getattr(cls, "CACHE_TTL", 0))
            respawn = getattr(module, "RESPAWN", getattr(cls, "RESPAWN", False))
            respawn_max = getattr(module, "RESPAWN_MAX", getattr(cls, "RESPAWN_MAX", 3))
            health_check_interval = getattr(module, "HEALTH_CHECK_INTERVAL", getattr(cls, "HEALTH_CHECK_INTERVAL", 30))
            priority = getattr(module, "AGENT_PRIORITY", getattr(cls, "AGENT_PRIORITY", 50))
            version = getattr(module, "AGENT_VERSION", getattr(cls, "AGENT_VERSION", "1.0"))
            description = getattr(module, "AGENT_DESC", getattr(cls, "AGENT_DESC", cls.__doc__ or ""))
            author = getattr(module, "AGENT_AUTHOR", getattr(cls, "AGENT_AUTHOR", ""))
        else:
            input_topics = getattr(cls, "INPUT_TOPICS", [])
            output_topics = getattr(cls, "OUTPUT_TOPICS", [])
            dependencies = getattr(cls, "DEPENDENCIES", [])
            cache_ttl = getattr(cls, "CACHE_TTL", 0)
            respawn = getattr(cls, "RESPAWN", False)
            respawn_max = getattr(cls, "RESPAWN_MAX", 3)
            health_check_interval = getattr(cls, "HEALTH_CHECK_INTERVAL", 30)
            priority = getattr(cls, "AGENT_PRIORITY", 50)
            version = getattr(cls, "AGENT_VERSION", "1.0")
            description = getattr(cls, "AGENT_DESC", cls.__doc__ or "")
            author = getattr(cls, "AGENT_AUTHOR", "")

        return AgentMeta(
            name=agent_name,
            version=version,
            description=description,
            author=author,
            priority=priority,
            input_topics=input_topics,
            output_topics=output_topics,
            dependencies=dependencies,
            cache_ttl=cache_ttl,
            respawn=respawn,
            respawn_max=respawn_max,
            health_check_interval=health_check_interval,
        )

    # ─── 执行计划 ─────────────────────────────

    def plan(self, pipeline_name: str, input_file: str,
             config: Optional[dict] = None) -> list[dict]:
        """生成执行计划（预览）"""
        agent_order = self.registry.deps_order()
        plan = []

        for i, name in enumerate(agent_order):
            meta = self.registry.get(name)
            if meta:
                plan.append({
                    "step": i + 1,
                    "agent": name,
                    "priority": meta.get("priority", 50),
                    "dependencies": meta.get("dependencies", []),
                    "description": meta.get("description", "")[:50],
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

    # ─── DAG 并行执行核心 ─────────────────────────────

    def _build_dag(self, agent_order: list[str], config: Optional[dict] = None) -> tuple[dict[str, TaskNode], list[list[str]]]:
        """从 agent_order 构建 DAG 节点和拓扑层级"""
        nodes = {}
        for name in agent_order:
            meta = self.registry.get(name)
            if not meta:
                continue
            deps = meta.get("dependencies", [])
            node = TaskNode(
                name=name,
                agent_name=name,
                dependencies=[d for d in deps if d in agent_order],
                timeout=config.get("timeout", 300) if config else 300,
                max_retries=config.get("max_retries", 3) if config else 3,
            )
            nodes[name] = node

        # Kahn 算法拓扑排序，得到层级
        in_degree = {name: len(node.dependencies) for name, node in nodes.items()}
        queue = [name for name, deg in in_degree.items() if deg == 0]
        execution_order = []

        while queue:
            level = queue
            execution_order.append(level)
            queue = []
            for name in level:
                for other_name, other_node in nodes.items():
                    if name in other_node.dependencies:
                        in_degree[other_name] -= 1
                        if in_degree[other_name] == 0:
                            queue.append(other_name)

        return nodes, execution_order

    def _execute_node(self, task: PipelineTask, node: TaskNode, input_file: str, config: dict) -> dict:
        """执行单个 DAG 节点"""
        instance = self.registry.get_instance(node.agent_name)
        if not instance:
            return {"error": f"Agent {node.agent_name} 未找到"}

        from .registry import AgentStatus
        self.registry.set_status(node.agent_name, AgentStatus.RUNNING)

        # 从输入文件提取查询词（针对 researcher）
        queries = []
        if node.agent_name == "researcher":
            try:
                with open(input_file, "r", encoding="utf-8") as f:
                    content = f.read()
                # 简单提取：每行非空内容作为查询
                queries = [line.strip() for line in content.split("\n") if line.strip() and not line.startswith("#")]
                if not queries:
                    queries = ["Python 异步编程 基础概念"]
            except Exception:
                queries = ["Python 异步编程 基础概念"]

        # 确定输出文件路径（传递给 writer/safewriter）
        output_file = config.get("output", f"output/{task.id}_result.md")

        # 收集依赖节点的结果（用于 writer 等）
        dep_results = {}
        for dep in node.dependencies:
            if dep in task.dag_nodes:
                dep_node = task.dag_nodes[dep]
                if dep_node.result and "results" in dep_node.result:
                    dep_results[dep] = dep_node.result["results"]

        # 获取 writer 的生成内容（用于 safewriter）
        writer_content = ""
        if "writer" in task.result and "content" in task.result["writer"]:
            writer_content = task.result["writer"]["content"]
        # layout 优化后的内容优先（若有）
        if "layout" in task.result:
            lo = task.result["layout"]
            if isinstance(lo, dict):
                writer_content = lo.get("content") or lo.get("optimized") or writer_content

        msg_payload = {
            "task_id": task.id,
            "input_file": input_file,
            "config": config,
            "pipeline": task.pipeline_name,
            "node": node.name,
            "dependencies_results": {dep: task.dag_nodes[dep].result for dep in node.dependencies},
            "queries": queries,  # 传递查询词
            "target_file": output_file,  # 传递目标文件
            "target": output_file,  # safe_writer 兼容字段名
            "results": dep_results.get("researcher", []),  # 传递 researcher 结果给 writer
            "content": writer_content,  # 传递 writer 生成的内容给 safewriter
        }

        # 生成幂等键：task_id + node_name + attempt
        idempotency_key = f"{task.id}:{node.name}:{node.attempts}"

        result = self.bus.request(
            topic=f"{node.agent_name}.input",
            from_a="orchestrator",
            to_a=node.agent_name,
            payload=msg_payload,
            timeout=node.timeout,
            idempotency_key=idempotency_key
        )

        self.registry.set_status(node.agent_name, AgentStatus.STOPPED)
        return result or {}

    def _run_dag_parallel(self, task: PipelineTask, input_file: str, config: dict) -> bool:
        """并行执行 DAG"""
        agent_order = self.registry.deps_order()
        nodes, execution_order = self._build_dag(agent_order, config)

        task.dag_nodes = nodes
        task.execution_order = execution_order
        total_nodes = len(nodes)
        completed = 0

        # 使用单个线程池执行整个 DAG，避免重复创建/销毁
        max_workers = max(len(l) for l in execution_order) if execution_order else 1
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for level_idx, level in enumerate(execution_order):
                if self._stop_event.is_set():
                    task.status = TaskStatus.CANCELLED
                    return False

                futures = {}
                for name in level:
                    node = nodes[name]
                    node.status = "running"
                    node.started_at = time.time()
                    node.attempts += 1

                    step_start = time.time()
                    step_result = StepResult(
                        step_name=f"step_{level_idx}_{name}",
                        agent_name=node.agent_name,
                        status="running",
                        started_at=step_start,
                    )

                    # 提交任务
                    future = executor.submit(self._execute_node, task, node, input_file, config)
                    futures[future] = (node, step_result)

                # 收集结果
                for future in as_completed(futures):
                    node, step_result = futures[future]
                    try:
                        result = future.result()
                        node.result = result
                        node.status = "success"
                        node.finished_at = time.time()
                        step_result.status = "success"
                        step_result.result = result
                        task.result[node.name] = result

                        # 语义失败检测：agent 返回了非成功状态（如 checker P0 阻断）
                        if isinstance(result, dict):
                            sem_status = result.get("status")
                            if sem_status in ("blocked", "fail"):
                                node.status = "failed"
                                step_result.status = "failed"
                                msg = result.get("message", result.get("error", f"Agent returned {sem_status}"))
                                step_result.error = msg
                                task.error = msg
                                if config and config.get("fail_fast", True):
                                    task.status = TaskStatus.FAILED
                                    for f in futures:
                                        f.cancel()
                                    break
                    except Exception as e:
                        node.error = str(e)
                        node.status = "failed"
                        node.finished_at = time.time()
                        step_result.status = "failed"
                        step_result.error = str(e)
                        task.error = str(e)

                        # 重试逻辑
                        if node.attempts < node.max_retries:
                            node.status = "pending"
                            # 重新加入下一轮（简化：这里直接重试一次）
                            retry_result = self._execute_node(task, node, input_file, config)
                            if retry_result and "error" not in retry_result:
                                node.result = retry_result
                                node.status = "success"
                                node.finished_at = time.time()
                                step_result.status = "success"
                                step_result.result = retry_result
                                task.result[node.name] = retry_result
                            else:
                                node.status = "failed"
                        else:
                            node.status = "failed"

                        # fail_fast
                        if config and config.get("fail_fast", True):
                            task.status = TaskStatus.FAILED
                            # 取消剩余 future
                            for f in futures:
                                f.cancel()
                            break

                    finally:
                        step_result.finished_at = time.time()
                        task.steps.append(step_result)
                        self._save_checkpoint(task)

                completed += len(level)
                task.current_step = completed
                task.progress = int((completed / total_nodes) * 100)

                # 检查是否有失败且 fail_fast
                if task.status == TaskStatus.FAILED:
                    return False

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
                print(f"[Orchestrator] 从断点恢复任务: {task_id}")
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
                print(f"[Orchestrator] 任务执行异常: {e}")

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
                return True
        return False

    def cancel(self, task_id: str) -> bool:
        """取消任务"""
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status in [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED]:
                task.status = TaskStatus.CANCELLED
                self._stop_event.set()
                return True
        return False

    def resume(self, task_id: str) -> bool:
        """恢复暂停的任务（从断点续传）"""
        with self._lock:
            task = self._running_tasks.get(task_id)
            if task and task.status == TaskStatus.PAUSED:
                task.status = TaskStatus.RUNNING
                self._log("info", "Pipeline resumed", task_id=task_id)
                return True
        return False

    # ─── 断点续传 + 事务回滚 ─────────────────────────────

    def _save_checkpoint(self, task: PipelineTask, full_state: bool = False):
        """保存断点"""
        try:
            data = task.to_dict()
            if full_state:
                data["_result"] = {
                    k: v for k, v in task.result.items()
                }
                data["_steps"] = [
                    {
                        "step_name": s.step_name,
                        "agent_name": s.agent_name,
                        "status": s.status,
                        "started_at": s.started_at,
                        "finished_at": s.finished_at,
                        "error": s.error,
                    }
                    for s in task.steps
                ]
                data["_dag_nodes"] = {
                    name: {
                        "status": n.status,
                        "error": n.error,
                        "attempts": n.attempts,
                        "result_keys": list(n.result.keys()),
                    }
                    for name, n in (task.dag_nodes or {}).items()
                }
            with open(task.checkpoint_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            print(f"[Orchestrator] 保存断点失败: {e}")

    def _load_checkpoint(self, task_id: str) -> Optional[PipelineTask]:
        """加载断点（仅恢复基础信息）"""
        checkpoint_file = self.checkpoint_dir / f"{task_id}.json"
        if not checkpoint_file.exists():
            return None

        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 断点完整性校验
            required = ["id", "pipeline", "input"]
            missing = [k for k in required if k not in data]
            if missing:
                print(f"[Orchestrator] 断点损坏: 缺少字段 {missing}")
                return None

            # 验证关键字段不为空
            if not data.get("id") or not data.get("pipeline"):
                print(f"[Orchestrator] 断点无效: id/pipeline 为空")
                return None

            task = PipelineTask(
                id=data["id"],
                pipeline_name=data["pipeline"],
                input_file=data.get("input", ""),
                config=data.get("config", {}),
                status=TaskStatus.PAUSED,
                current_step=len(data.get("steps", [])),
            )
            task.result = data.get("_result", {})
            return task
        except Exception as e:
            print(f"[Orchestrator] 加载断点失败: {e}")
            return None

    def _rollback_task(self, task: PipelineTask, snapshot: dict) -> bool:
        """从快照回滚 task 状态（事务恢复）"""
        try:
            task.result = dict(snapshot.get("result", {}))
            task.steps = list(snapshot.get("steps", []))
            task.error = snapshot.get("error", "")
            task.status = TaskStatus.RUNNING
            task.progress = snapshot.get("progress", 0)
            task.current_step = snapshot.get("current_step", 0)
            print(f"[Orchestrator] 已回滚到 checkpoint ({task.current_step}/{len(task.steps)} steps)")
            return True
        except Exception as e:
            print(f"[Orchestrator] 回滚失败: {e}")
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

    def _remove_checkpoint(self, task_id: str):
        """移除断点文件"""
        checkpoint_file = self.checkpoint_dir / f"{task_id}.json"
        try:
            if checkpoint_file.exists():
                checkpoint_file.unlink()
        except Exception:
            pass

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
                    if self._stop_event.is_set():
                        task.status = TaskStatus.CANCELLED
                        return

                    # 暂停检查点：任务被暂停时阻塞，直到恢复或取消
                    while task.status == TaskStatus.PAUSED:
                        if self._stop_event.is_set():
                            task.status = TaskStatus.CANCELLED
                            return
                        time.sleep(0.5)
                    if task.status == TaskStatus.CANCELLED:
                        return

                    # ── 事务边界：快照当前状态 ──
                    level_snapshot = self._snapshot_state(task)

                    max_workers = max(
                        (node.agent_config.parallelism.get("max_workers", 1) for node in level),
                        default=1,
                    )

                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = {}
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

                            step_start = time.time()
                            step_result = StepResult(
                                step_name=f"plan_{plan.plan_id}_l{level_idx}_{node.agent_name}",
                                agent_name=node.agent_name,
                                status="running",
                                started_at=step_start,
                            )

                            dag_node.attempts += 1
                            fut = executor.submit(
                                self._execute_node_from_scheduler,
                                task=task,
                                node=node,
                                input_file=input_file,
                                plan=plan,
                            )
                            futures[fut] = (node, dag_node, step_result)

                        for fut in as_completed(futures):
                            node, dag_node, step_result = futures[fut]
                            try:
                                result = fut.result()
                                dag_node.result = result
                                dag_node.status = "success"
                                dag_node.finished_at = time.time()
                                step_result.status = "success"
                                step_result.result = result
                                task.result[node.agent_name] = result
                            except Exception as e:
                                dag_node.error = str(e)
                                dag_node.status = "failed"
                                dag_node.finished_at = time.time()
                                step_result.status = "failed"
                                step_result.error = str(e)
                                task.error = str(e)

                                # 按 backoff 策略重试
                                if dag_node.attempts < node.max_retries:
                                    delay = self._backoff_delay(
                                        node.backoff, node.initial_delay, dag_node.attempts
                                    )
                                    time.sleep(delay)
                                    dag_node.attempts += 1
                                    retry_result = self._execute_node_from_scheduler(
                                        task, node, input_file, plan
                                    )
                                    if retry_result and "error" not in retry_result:
                                        dag_node.result = retry_result
                                        dag_node.status = "success"
                                        dag_node.finished_at = time.time()
                                        step_result.status = "success"
                                        step_result.result = retry_result
                                        task.result[node.agent_name] = retry_result
                                    else:
                                        dag_node.status = "failed"

                                        # ── 回滚到 level 开始时的状态 ──
                                        self._rollback_task(task, level_snapshot)

                                # 熔断统计（仅在实际失败时记录）
                                if dag_node.status == "failed":
                                    if self._circuit_breaker(node, task):
                                        task.status = TaskStatus.FAILED
                                        break
                                else:
                                    # 成功：重置该 agent 熔断计数
                                    self._circuit_breaker_success(node)

                                # fail_fast
                                if plan.fail_fast:
                                    task.status = TaskStatus.FAILED
                                    for f in futures:
                                        f.cancel()
                                    break

                            finally:
                                step_result.finished_at = time.time()
                                duration_ms = (step_result.finished_at - step_result.started_at) * 1000
                                task.steps.append(step_result)
                                self._save_checkpoint(task)
                                self._audit_log(
                                    task_id=task.id,
                                    agent_name=node.agent_name,
                                    input_summary=node.agent_config.config,
                                    output_summary=step_result.result or {},
                                    duration_ms=duration_ms,
                                    status=step_result.status,
                                    error=step_result.error,
                                )
                                # 指标
                                self._metrics.observe(
                                    "step_duration_ms", duration_ms,
                                    labels={"agent": node.agent_name, "status": step_result.status},
                                )
                                self._metrics.counter(
                                    "step_total", labels={"agent": node.agent_name},
                                )
                                self._metrics.gauge(
                                    "pipeline_progress", task.progress,
                                    labels={"pipeline": plan.pipeline_name},
                                )

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
                                    task.result[k] for k in task.result
                                    if k.startswith(f"{base}_pool_") and task.result[k]
                                ]
                                if pooled_results:
                                    # 合并 researcher 的 results 列表
                                    if base == "researcher":
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
                                        task.result[base] = combined
                                    else:
                                        # 非 researcher 池：取第一个非空结果
                                        for pr in pooled_results:
                                            if pr:
                                                task.result[base] = pr
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

                if task.status == TaskStatus.DONE and not plan.checkpoint.get("keep_on_success", False):
                    self._remove_checkpoint(task.id)

        if wait:
            execute()
        else:
            threading.Thread(target=execute, daemon=True).start()

        return task

    def _execute_node_from_scheduler(self, task: PipelineTask, node: "ExecutionNode",
                                     input_file: str, plan: ExecutionPlan) -> dict:
        """由 run_plan 调度：基于 ExecutionNode 执行单个节点"""
        # 池化节点：strip `_pool_N` 后缀以找到实际 agent
        raw_agent = node.agent_name
        base_agent = raw_agent.split("_pool_")[0] if "_pool_" in raw_agent else raw_agent
        pool_idx = int(raw_agent.split("_pool_")[1]) if "_pool_" in raw_agent else 0
        pool_size = max(node.agent_config.pool_size, 1)

        instance = self.registry.get_instance(base_agent)
        if not instance:
            return {"error": f"Agent {base_agent} 未找到"}

        from .registry import AgentStatus
        self.registry.set_status(base_agent, AgentStatus.RUNNING)

        # 查询词提取（按池实例分片）
        queries = []
        if base_agent == "researcher":
            try:
                with open(input_file, "r", encoding="utf-8") as f:
                    content = f.read()
                all_queries = [line.strip() for line in content.split("\n")
                               if line.strip() and not line.startswith("#")]
                if not all_queries:
                    all_queries = [node.agent_config.config.get("default_query", "Python 异步编程")]
                # 分片分配：每个 pool 实例处理一部分 query
                if pool_size > 1 and len(all_queries) >= pool_size:
                    # 轮询分片
                    queries = all_queries[pool_idx::pool_size]
                else:
                    # 池数多于查询数：全部复制，或 pool_0 独占
                    queries = all_queries if pool_idx == 0 else []
            except Exception:
                queries = [node.agent_config.config.get("default_query", "Python 异步编程")]

        output_file = (
            node.agent_config.config.get("output")
            or plan.raw.get("pipeline", {}).get("output")
            or f"output/{task.id}_result.md"
        )

        dep_results = {}
        for dep in node.dependencies:
                if dep in task.dag_nodes:
                    dep_node = task.dag_nodes[dep]
                    if dep_node.result and "results" in dep_node.result:
                        dep_results[dep] = dep_node.result["results"]
                elif dep in task.result and isinstance(task.result[dep], dict):
                    # 支持 pool 合并后的结果查找
                    if "results" in task.result[dep]:
                        dep_results[dep] = task.result[dep]["results"]

        writer_content = ""
        if "writer" in task.result and "content" in task.result["writer"]:
            writer_content = task.result["writer"]["content"]
        # layout 优化后的内容优先（若有）
        if "layout" in task.result:
            lo = task.result["layout"]
            if isinstance(lo, dict):
                writer_content = lo.get("content") or lo.get("optimized") or writer_content

        msg_payload = {
            "task_id": task.id,
            "input_file": input_file,
            "config": node.agent_config.config,
            "pipeline": plan.pipeline_name,
            "node": node.agent_name,
            "dependencies_results": {
                dep: task.dag_nodes[dep].result for dep in node.dependencies if dep in task.dag_nodes
            },
            "queries": queries,
            "target_file": output_file,
            "results": dep_results.get("researcher", []),
                        "content": writer_content,
                        # fetcher 传递的文章内容（先检查依赖结果，再检查 task.result）
                        "articles": dep_results.get("fetcher", {}).get("articles", [])
                                    if isinstance(dep_results.get("fetcher"), dict)
                                    else task.result.get("fetcher", {}).get("articles", []),
        }

        idempotency_key = f"{task.id}:{node.agent_name}:{task.dag_nodes[node.agent_name].attempts}"

        result = self.bus.request(
            topic=f"{base_agent}.input",
            from_a="orchestrator",
            to_a=base_agent,
            payload=msg_payload,
            timeout=node.timeout,
            idempotency_key=idempotency_key,
        )

        # ── QualityGate 自动重做循环 ──
        if node.agent_name == "quality_gate" and isinstance(result, dict):
            try:
                generation = 0
                max_gen = node.agent_config.config.get("max_regenerations", 3)
                while result.get("needs_regenerate") and result.get("can_regenerate") and generation < max_gen:
                    generation += 1
                    feedback = {
                        "quality_scores": result.get("scores", {}),
                        "overall_score": result.get("overall_score", 0),
                        "style_issues": result.get("style_issues", []),
                        "generation_count": result.get("generation_count", 0) + 1,
                    }
                    print(f"[Orchestrator] 质量分 {result.get('overall_score', 0)} < 70，第 {generation} 次重做", flush=True)
                    # 重新调用 writer
                    writer_payload = {**msg_payload}
                    writer_payload.update(feedback)
                    writer_result = self.bus.request(
                        topic="writer.input", from_a="orchestrator", to_a="writer",
                        payload=writer_payload, timeout=node.timeout,
                        idempotency_key=f"regenerate_{task.id}_writer_g{generation}",
                    )
                    if writer_result and writer_result.get("content"):
                        msg_payload["content"] = writer_result["content"]
                        msg_payload["generation_count"] = feedback["generation_count"]
                        task.result["writer"] = writer_result

                    # 重新调用 quality_gate
                    qg_payload = {**msg_payload}
                    qg_payload.update(feedback)
                    result = self.bus.request(
                        topic="quality_gate.input", from_a="orchestrator", to_a="quality_gate",
                        payload=qg_payload, timeout=node.timeout,
                        idempotency_key=f"regenerate_{task.id}_quality_g{generation}",
                    )

                final_status = "accepted_with_warnings" if result.get("needs_regenerate") else "pass"
                result["status"] = final_status
                print(f"[Orchestrator] 重做 {generation} 次后质量分 {result.get('overall_score', 0)} → {final_status}", flush=True)
            except Exception as e:
                print(f"[Orchestrator] 质量重做异常: {e}", flush=True)
                result = result or {"status": "error", "error": str(e)}

        self.registry.set_status(node.agent_name, AgentStatus.STOPPED)
        return result or {}

    def _backoff_delay(self, backoff: str, initial_delay: float, attempt: int) -> float:
        """计算重试退避时间（带 jitter）"""
        return backoff_with_jitter(
            base_delay=initial_delay,
            attempt=attempt,
            strategy=backoff,
        )

    def _circuit_breaker(self, node: ExecutionNode, task: PipelineTask) -> bool:
        """Per-agent 熔断器：失败达到阈值后返回 True（表示应熔断/跳过）"""
        cb_cfg = node.agent_config.circuit_breaker
        if not cb_cfg or not cb_cfg.get("enabled", False):
            return False

        agent_name = node.agent_name
        breaker = self._cb_registry.get_or_create(
            name=agent_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            recovery_timeout=cb_cfg.get("recovery_timeout", 60),
        )

        # 记录一次失败（调用方已确认 node 失败）
        breaker.record_failure()
        if not breaker.allow_request():
            print(f"[CircuitBreaker] {agent_name} 已熔断，跳过")
            return True

        return False

    def _circuit_breaker_success(self, node: ExecutionNode):
        """node 成功时重置该 agent 的熔断计数"""
        cb_cfg = node.agent_config.circuit_breaker
        if not cb_cfg or not cb_cfg.get("enabled", False):
            return
        breaker = self._cb_registry.get_or_create(
            name=node.agent_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            recovery_timeout=cb_cfg.get("recovery_timeout", 60),
        )
        breaker.record_success()

    # ─── 报告和回调 ─────────────────────────────

    def _generate_report(self, task: PipelineTask):
        """生成执行报告"""
        report_file = self.checkpoint_dir / f"report_{task.id}.json"
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Orchestrator] 生成报告失败: {e}")

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
                print(f"[Orchestrator] 回调执行失败: {e}")

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
            print(f"[Orchestrator] Audit log 失败: {e}")

    # ─── 查询 ─────────────────────────────

    def get_task(self, task_id: str) -> Optional[PipelineTask]:
        return self._running_tasks.get(task_id)

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

        print(f"[Orchestrator] 重新执行流水线: {plan.pipeline_name} (input={input_file})")
        return self.run_plan(plan, input_file=input_file, task_id=task_id, wait=False)

    def shutdown(self):
        """关闭编排器"""
        self._stop_event.set()
        self.bus.shutdown()
        self.registry.shutdown()
        self.stop_admin_api()
        # 清理 7 天前的 checkpoint 文件
        self._cleanup_old_checkpoints(max_age_days=7)

    def _cleanup_old_checkpoints(self, max_age_days: int = 7):
        """清理过期的 checkpoint 和报告文件"""
        import shutil
        cutoff = time.time() - max_age_days * 86400
        for f in self.checkpoint_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass
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