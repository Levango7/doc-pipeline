"""DAG 执行器 —— 负责 DAG 构建、节点调度、熔断器、限流、指标"""
from __future__ import annotations

import asyncio
import copy
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cache_manager import CacheManager
from .circuit_breaker import backoff_with_jitter

# ─── 模块级函数：支持 ProcessPoolExecutor pickle ──────────────────

# 子进程上下文缓存（每个 worker 进程仅重建一次；Windows spawn 下模块级状态按进程隔离）
_CHILD_CONTEXT_LOCK = threading.Lock()
_CHILD_CONTEXT = None
_CHILD_CONTEXT_KEY = None


def _build_child_context(ctx_cfg: dict):
    """在子进程内重建最小可执行上下文：(registry, bus)。

    - Registry 关闭健康检查线程（子进程无需活性探测）
    - MessageBus 使用非持久化模式：节点执行所需输入来自任务载荷而非总线，
      子进程内的 publish 事件不回传父进程（已知限制，见 docs/architecture.md §5）
    """
    from .agent_loader import AgentLoader
    from .message_bus_v3 import MessageBus
    from .registry import Registry

    registry = Registry(enable_health_check=False)
    bus = MessageBus(enable_persistence=False)
    loader = AgentLoader(registry, bus, ctx_cfg.get("agents_dir", "agents"))
    loaded = loader.register(
        ctx_cfg.get("agent_names"),
        ctx_cfg.get("config") or {},
    )
    if not loaded:
        raise RuntimeError(
            f"子进程上下文重建失败：{ctx_cfg.get('agents_dir')} 下未加载到任何 Agent"
        )
    return registry, bus


def _get_child_context(ctx_cfg: dict):
    """获取（或构建）本 worker 进程的缓存上下文"""
    global _CHILD_CONTEXT, _CHILD_CONTEXT_KEY
    key = (
        str(ctx_cfg.get("agents_dir")),
        repr(sorted((ctx_cfg.get("config") or {}).items()))[:512],
        repr(ctx_cfg.get("agent_names")),
    )
    with _CHILD_CONTEXT_LOCK:
        if _CHILD_CONTEXT is None or key != _CHILD_CONTEXT_KEY:
            _CHILD_CONTEXT = _build_child_context(ctx_cfg)
            _CHILD_CONTEXT_KEY = key
    return _CHILD_CONTEXT


def _execute_node_worker(dag_executor, task, node, input_file, plan):
    """模块级节点执行函数 —— ProcessPoolExecutor 子进程入口。

    dag_executor 经 pickle 传入时非序列化属性为 None；若构造时携带
    child_context 配置，则在本进程内一次性重建 registry/bus 后执行节点，
    结果 dict 作为返回值 pickle 回父进程。
    """
    if dag_executor.registry is None or dag_executor.bus is None:
        ctx_cfg = getattr(dag_executor, "child_context", None)
        if not ctx_cfg:
            raise RuntimeError(
                "ProcessPoolExecutor 模式下 registry/bus 不可用，且 DAGExecutor "
                "未携带 child_context 配置（无法在子进程重建上下文）。"
                "请先通过 PipelineOrchestrator.register_agents() 注册 Agent，"
                "或使用 ThreadPoolExecutor（默认）模式。"
            )
        registry, bus = _get_child_context(ctx_cfg)
        dag_executor.registry = registry
        dag_executor.bus = bus
    return dag_executor.execute_node_from_scheduler(task, node, input_file, plan)


class DAGExecutor:
    """DAG 构建和执行"""

    def __init__(self, registry, bus, cb_registry, rate_limiters, metrics, logger=None,
                 stop_event: threading.Event | None = None,
                 checkpoint_save_fn: Callable | None = None,
                 audit_log_fn: Callable | None = None,
                 child_context: dict | None = None):
        self.registry = registry
        self.bus = bus
        self._cb_registry = cb_registry
        self._rate_limiters = rate_limiters
        self._metrics = metrics
        self._logger = logger
        self._stop_event = stop_event
        self._checkpoint_save = checkpoint_save_fn
        self._audit_log = audit_log_fn
        # process 模式子进程上下文重建配置（可 pickle 的纯数据）：
        # {"agents_dir": str, "agent_names": list[str] | None, "config": dict}
        self.child_context = child_context
        self._execution_stats: list[dict] = []
        self._query_cache = CacheManager(name="dag_queries", max_size=100, ttl=3600)

    # ─── pickle 支持（ProcessPoolExecutor 兼容） ─────────────

    # 非可序列化属性列表：pickle 时置 None，子进程在 _execute_node_worker 中重建
    _NON_PICKLABLE_ATTRS = (
        "registry", "bus", "_logger", "_stop_event",
        "_checkpoint_save", "_audit_log",
        "_cb_registry", "_rate_limiters", "_metrics", "_query_cache",
    )

    def __getstate__(self):
        """pickle 时剥离非可序列化属性，其余正常序列化。"""
        state = self.__dict__.copy()
        for attr in self._NON_PICKLABLE_ATTRS:
            state[attr] = None
        return state

    def __setstate__(self, state):
        """从 pickle 恢复：非序列化组件按需重建（子进程独立实例）。"""
        self.__dict__.update(state)
        if self.__dict__.get("_cb_registry") is None:
            from .circuit_breaker import CircuitBreakerRegistry
            self._cb_registry = CircuitBreakerRegistry()
        if self.__dict__.get("_rate_limiters") is None:
            from .rate_limiter import RateLimiterRegistry
            self._rate_limiters = RateLimiterRegistry()
        if self.__dict__.get("_metrics") is None:
            from .observability import get_metrics
            self._metrics = get_metrics()
        if self.__dict__.get("_query_cache") is None:
            self._query_cache = CacheManager(name="dag_queries", max_size=100, ttl=3600)

    @classmethod
    def from_config(cls, config: dict, registry, bus, cb_registry, rate_limiters, metrics,
                    logger=None, stop_event=None, checkpoint_save_fn=None, audit_log_fn=None):
        """手动构建 DAGExecutor（低层入口）。

        常规 process 模式无需调用它：_execute_node_worker 会依据 child_context
        自动在子进程内重建 registry/bus。此方法保留给需要完全手工装配的高级场景。
        """
        return cls(
            registry=registry, bus=bus, cb_registry=cb_registry,
            rate_limiters=rate_limiters, metrics=metrics,
            logger=logger, stop_event=stop_event,
            checkpoint_save_fn=checkpoint_save_fn, audit_log_fn=audit_log_fn,
        )

    def _log(self, level: str, msg: str, **kw):
        """结构化日志记录"""
        if self._logger:
            self._logger.log(level, msg, **kw)

    def _acquire_rate_limit(self, agent_name: str, rate_limit_cfg: dict | None = None,
                             timeout: float = 30.0) -> bool:
        """获取限流令牌。未配置限流时直接放行。"""
        cfg = rate_limit_cfg or {}
        rate = float(cfg.get("rate", 0))
        if rate <= 0:
            return True  # 不限流
        burst = int(cfg.get("burst", rate * 2))
        limiter = self._rate_limiters.get_or_create(agent_name, rate=rate, burst=burst)
        return limiter.acquire(1, block=True, timeout=timeout)  # type: ignore[no-any-return]

    # ─── DAG 构建 ─────────────────────────────

    def build_dag(self, agent_order: list[str], config: dict | None = None) -> tuple[dict, list[list[str]]]:
        """从 agent_order 构建 DAG 节点和拓扑层级"""
        from .pipeline import TaskNode

        nodes = {}
        for name in agent_order:
            meta = self.registry.get_meta(name)
            if not meta:
                continue
            deps = meta.dependencies
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

        # 环检测：检查是否所有节点都被排序
        sorted_count = sum(len(level) for level in execution_order)
        if sorted_count != len(nodes):
            cycle_nodes = [name for name, deg in in_degree.items() if deg > 0]
            raise ValueError(f"DAG 中存在环，无法拓扑排序。环中节点: {cycle_nodes}")

        return nodes, execution_order

    # ─── DAG 执行 ─────────────────────────────

    def execute_node(self, task, node, input_file: str, config: dict) -> dict:
        """执行单个 DAG 节点（统一节点模型，TaskNode 已兼容 ExecutionNode 接口）"""
        from types import SimpleNamespace

        # 确保 node 有 agent_config（统一模型下 TaskNode 自带）
        if not hasattr(node, "agent_config") or node.agent_config is None:
            from .pipeline import NodeConfig
            node.agent_config = NodeConfig(
                config=config,
                pool_size=1,
                rate_limit=getattr(node, "rate_limit", {}),
                circuit_breaker=config.get("circuit_breaker", {}),
            )
        elif hasattr(node.agent_config, "config") and not node.agent_config.config:
            node.agent_config.config = config

        plan = SimpleNamespace(
            pipeline_name=task.pipeline_name,
            checkpoint=config if isinstance(config, dict) else {},
            fail_fast=config.get("fail_fast", True) if isinstance(config, dict) else True,
            raw=config if isinstance(config, dict) else {},
        )
        return self.execute_node_from_scheduler(task, node, input_file, plan)

    def execute_node_from_scheduler(self, task, node, input_file: str, plan) -> dict:
        """由 run_plan 调度：基于 ExecutionNode 执行单个节点"""
        from .registry import AgentStatus

        # 池化节点：strip `_pool_N` 后缀以找到实际 agent
        raw_agent = node.agent_name
        base_agent = raw_agent.split("_pool_")[0] if "_pool_" in raw_agent else raw_agent
        # pool_idx 解析容错
        pool_idx = 0
        if "_pool_" in raw_agent:
            try:
                pool_idx = int(raw_agent.split("_pool_")[1])
            except (ValueError, IndexError):
                pool_idx = 0
        pool_size = max(node.agent_config.pool_size, 1)

        instance = self.registry.get_instance(base_agent)
        if not instance:
            return {"error": f"Agent {base_agent} 未找到"}

        # 熔断器检查：执行前先检查是否允许请求
        cb_cfg = node.agent_config.circuit_breaker if hasattr(node, "agent_config") and node.agent_config else None
        if cb_cfg and cb_cfg.get("enabled", False):
            breaker = self._cb_registry.get_or_create(
                name=base_agent,
                failure_threshold=cb_cfg.get("failure_threshold", 5),
                recovery_timeout=cb_cfg.get("recovery_timeout", 60),
            )
            if not breaker.allow_request():
                self._log("warning", f"{base_agent} 已熔断", reason="circuit_open")
                return {"error": "circuit open", "status": "blocked"}

        self.registry.set_status(base_agent, AgentStatus.RUNNING)

        # 限流：获取令牌（按 agent 级 rate_limit 配置）
        if not self._acquire_rate_limit(base_agent, node.agent_config.rate_limit,
                                        timeout=node.timeout or 30):
            self.registry.set_status(base_agent, AgentStatus.STOPPED)
            return {"error": f"Agent {base_agent} 被限流"}

        try:
            # 查询词提取（所有节点共享完整 queries；researcher 按池分片）
            try:
                all_queries = self._extract_queries(input_file, node)
            except Exception:
                all_queries = [node.agent_config.config.get("default_query", "Python 异步编程")]
            meta2 = self.registry.get_meta(base_agent)
            if getattr(meta2, "extracts_queries", False) and pool_size > 1 and len(all_queries) >= pool_size:
                queries = all_queries[pool_idx::pool_size]
            else:
                queries = all_queries

            output_file = (
                node.agent_config.config.get("output")
                or plan.raw.get("pipeline", {}).get("output")
                or f"output/{task.id}_result.md"
            )

            # 合并依赖结果：对池化 agent 自动合并所有 pool 实例的结果
            dep_results_raw = {
                dep: task.dag_nodes[dep].result for dep in node.dependencies if dep in task.dag_nodes
            }
            research_results = self._get_dep_list_results(task, node.dependencies, "results")
            articles = self._get_dep_list_results(task, node.dependencies, "articles")
            writer_content = self._get_latest_content(task, node.dependencies)

            # 保留 _raw 结构供需要完整 dict 的场景，同时正确填充 dep_results[base]
            dep_results = {}  # type: ignore[var-annotated]
            for dep in node.dependencies:
                if dep in task.dag_nodes:
                    dep_node = task.dag_nodes[dep]
                    if dep_node.result and isinstance(dep_node.result, dict):
                        base = dep.split("_pool_")[0] if "_pool_" in dep else dep
                        dep_results.setdefault(f"_{dep}_raw", {}).update(dep_node.result)
                        if base not in dep_results:
                            dep_results[base] = []
                        # 将 dep_node.result 的内容合并到 dep_results[base]
                        for key, val in dep_node.result.items():
                            if isinstance(val, list):
                                dep_results[base].extend(val)
                            else:
                                dep_results[base].append({key: val})

            # 三层配置合并：agent 构造配置（config.json，来自 meta.config）为基底，
            # 流水线 YAML 节点 config 覆盖其上；agent 内仍可在运行时读取 payload 做最终覆盖
            ctor_config = getattr(meta2, "config", None) or {}
            merged_config = {**ctor_config, **node.agent_config.config}

            # 收集上游 requirements_analyzer 产出的 DocumentSpec（供下游消费）
            spec_result = None
            if "requirements_analyzer" in task.dag_nodes:
                ra_result = task.dag_nodes["requirements_analyzer"].result
                if isinstance(ra_result, dict):
                    spec_result = ra_result.get("spec")

            msg_payload = {
                "task_id": task.id,
                "input_file": input_file,
                "config": merged_config,
                "pipeline": plan.pipeline_name,
                "node": node.agent_name,
                "dependencies_results": dep_results_raw,
                "queries": queries,
                "target_file": output_file,
                "target": output_file,
                "results": research_results,
                "articles": articles,
                "content": writer_content,
                "spec": spec_result,
            }

            node_state = task.dag_nodes[node.agent_name]
            idempotency_key = f"{task.id}:{node.agent_name}:{node_state.attempts}"
            if getattr(node_state, "_bypass_idempotency", False):
                idempotency_key = f"{idempotency_key}:r{uuid.uuid4().hex[:8]}"

            result = self.bus.request(
                topic=f"{base_agent}.input",
                from_a="orchestrator",
                to_a=base_agent,
                payload=msg_payload,
                timeout=node.timeout,
                idempotency_key=idempotency_key,
            )

            # ── QualityGate 自动重做循环（外提为独立方法）──
            meta = self.registry.get_meta(node.agent_name)
            if getattr(meta, "supports_regeneration", False) and isinstance(result, dict):
                result = self._handle_regeneration(
                    task, node, result, msg_payload,
                    regenerate_agent=getattr(meta, "regeneration_target", "writer"),
                    recheck_agent=getattr(meta, "regeneration_recheck", "quality_gate"),
                    max_gen=node.agent_config.config.get("max_regenerations", 3),
                )

            self.registry.set_status(base_agent, AgentStatus.STOPPED)
            return result or {}
        except Exception:
            self.registry.set_status(base_agent, AgentStatus.ERROR)
            raise

    def _handle_regeneration(self, task, node, result: dict,
                             msg_payload: dict,
                             regenerate_agent: str = "writer",
                             recheck_agent: str = "quality_gate",
                             max_gen: int = 3) -> dict:
        """QualityGate 自动重做循环 —— 独立方法，只重跑 affected node。

        与原内嵌逻辑相比：
        - 不重新调度整个 level，减少线程池空转
        - 避免重复检查点写入
        - 使用 deepcopy 防止 feedback 污染原 payload
        """
        try:
            generation = 0
            while (result.get("needs_regenerate") and result.get("can_regenerate")
                   and generation < max_gen and not task.stop_event.is_set()):
                generation += 1
                feedback = {
                    "quality_scores": result.get("scores", {}),
                    "overall_score": result.get("overall_score", 0),
                    "style_issues": result.get("style_issues", []),
                    "citation_report": result.get("citation_report", {}),
                    "generation_count": result.get("generation_count", 0) + 1,
                }
                self._log("info", "质量门控重做",
                          score=result.get('overall_score', 0), generation=generation)

                # deepcopy 防止 feedback 污染原 msg_payload
                writer_payload = copy.deepcopy(msg_payload)
                writer_payload.update(feedback)
                writer_result = self.bus.request(
                    topic=f"{regenerate_agent}.input", from_a="orchestrator", to_a=regenerate_agent,
                    payload=writer_payload, timeout=node.timeout,
                    idempotency_key=f"regenerate_{task.id}_{regenerate_agent}_g{generation}",
                )
                if writer_result and writer_result.get("content"):
                    msg_payload["content"] = writer_result["content"]
                    msg_payload["generation_count"] = feedback["generation_count"]
                    self._set_task_output(task, regenerate_agent, writer_result)

                qg_payload = copy.deepcopy(msg_payload)
                qg_payload.update(feedback)
                result = self.bus.request(
                    topic=f"{recheck_agent}.input", from_a="orchestrator", to_a=recheck_agent,
                    payload=qg_payload, timeout=node.timeout,
                    idempotency_key=f"regenerate_{task.id}_{recheck_agent}_g{generation}",
                )
                if result:
                    self._set_task_output(task, recheck_agent, result)

            final_status = "accepted_with_warnings" if result.get("needs_regenerate") else "pass"
            result["status"] = final_status
            if result.get("needs_regenerate"):
                self._log("warning", "质量分仍不达标",
                          generation=generation, score=result.get('overall_score', 0))
            else:
                self._log("info", "质量分通过",
                          generation=generation, score=result.get('overall_score', 0))
        except Exception as e:
            self._log("error", "质量重做异常", error=str(e))
            result = result or {"status": "error", "error": str(e)}

        return result

    # ─── 层级执行：共享辅助方法 ─────────────────────

    @staticmethod
    def _business_failure(result) -> tuple[bool, str]:
        """Agent 返回业务失败（status ∈ blocked/fail 或携带 error 键）时返回 (True, 错误消息)"""
        if isinstance(result, dict):
            sem_status = result.get("status")
            if sem_status in ("blocked", "fail"):
                raw = result.get("message", result.get("error", f"Agent returned {sem_status}"))
                return True, "" if raw is None else str(raw)
            if "error" in result:
                raw = result.get("error")
                return True, "" if raw is None else str(raw)
        return False, ""

    @staticmethod
    def _evaluate_retry_result(retry_result) -> tuple[bool, str]:
        """判定重试结果是否成功，返回 (是否成功, 失败原因)"""
        retry_ok = True
        retry_err = ""
        if not retry_result or "error" in retry_result:
            retry_ok = False
            raw = retry_result.get("error", "retry failed") if retry_result else "retry failed"
            retry_err = "" if raw is None else str(raw)
        elif isinstance(retry_result, dict):
            sem_status = retry_result.get("status")
            if sem_status in ("blocked", "fail"):
                raw = retry_result.get("message", retry_result.get("error", f"Agent returned {sem_status}"))
                retry_err = "" if raw is None else str(raw)
        return retry_ok, retry_err

    def _apply_node_success(self, task, node, dag_node, step_result, result) -> None:
        """节点成功：写入 dag_node/step_result/任务输出 + 熔断成功计数"""
        dag_node.result = result
        dag_node.status = "success"
        dag_node.finished_at = time.time()
        dag_node.error = ""
        step_result.status = "success"
        step_result.result = result
        step_result.error = ""
        self._set_task_output(task, node.agent_name, result)
        self._circuit_breaker_success(node)

    def _merge_resumed_nodes(self, task) -> None:
        """断点续传：把 checkpoint 恢复的节点状态合并进重建后的 DAG（每任务一次）"""
        if getattr(task, "_resume_merge_done", False):
            return
        snaps = getattr(task, "_resumed_node_snapshots", None)
        task._resume_merge_done = True
        if snaps is None:
            return
        task._resumed_from_checkpoint = True
        for name, snap in (snaps or {}).items():
            dag_node = task.dag_nodes.get(name)
            if dag_node is None:
                continue
            dag_node.attempts = int(snap.get("attempts", 0) or 0)
            dag_node.error = str(snap.get("error", "") or "")
            dag_node.result = snap.get("result") or {}
            finished_at = snap.get("finished_at", 0) or 0
            if finished_at:
                dag_node.finished_at = float(finished_at)
            if snap.get("status") == "success" and dag_node.result:
                dag_node.status = "success"
            else:
                dag_node.status = "pending"
        for dag_node in task.dag_nodes.values():
            if dag_node.status != "success" and dag_node.attempts == 0:
                dag_node._bypass_idempotency = True

    def _reuse_completed_node(self, task, node, dag_node, plan) -> bool:
        """断点续传：已完成且结果非空的节点直接复用结果注入下游，不提交 bus.request"""
        if dag_node.status != "success" or not dag_node.result:
            return False
        from .pipeline import StepResult

        started_at = dag_node.started_at or time.time()
        step_result = StepResult(
            step_name=node.agent_name,
            agent_name=node.agent_name,
            status="success",
            started_at=started_at,
            finished_at=time.time(),
            result=dict(dag_node.result),
        )
        self._set_task_output(task, node.agent_name, dag_node.result)
        self._record_step_result(task, plan, node, step_result)
        self._log("info", "断点续传：复用已完成节点", task_id=task.id, node=node.agent_name)
        return True

    def _cancel_unstarted_siblings(self, task, plan, futures: dict, processed: set) -> None:
        """fail_fast/熔断中断后：未启动的兄弟节点置 cancelled 并记录 step_result"""
        for future, (node, dag_node, step_result) in futures.items():
            if future in processed:
                continue
            if not future.cancel():
                continue
            dag_node.status = "cancelled"
            dag_node.error = "cancelled"
            dag_node.finished_at = time.time()
            step_result.status = "cancelled"
            step_result.error = "cancelled"
            self._record_step_result(task, plan, node, step_result)

    def _submit_level_futures(self, task, level: list, input_file: str,
                              plan, executor: ThreadPoolExecutor) -> dict:
        """提交层级内所有节点到执行器，返回 {future: (node, dag_node, step_result)}"""
        from .executor_factory import is_process_executor
        from .pipeline import StepResult

        futures = {}
        for node in level:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
                continue

            if self._reuse_completed_node(task, node, dag_node, plan):
                continue

            dag_node.status = "running"
            dag_node.started_at = time.time()
            dag_node.attempts += 1

            step_result = StepResult(
                step_name=node.agent_name,
                agent_name=node.agent_name,
                status="running",
                started_at=time.time(),
            )

            if is_process_executor(executor):
                # ProcessPoolExecutor 模式：使用模块级函数（可 pickle）
                future = executor.submit(
                    _execute_node_worker, self, task, node, input_file, plan,
                )
            else:
                # ThreadPoolExecutor 模式：直接提交 bound method（零开销）
                future = executor.submit(
                    self.execute_node_from_scheduler,
                    task=task, node=node, input_file=input_file, plan=plan,
                )
            futures[future] = (node, dag_node, step_result)
        return futures

    def _retry_node_sync(self, task, node, dag_node, step_result,
                         input_file: str, plan) -> tuple[object, str]:
        """失败节点重试循环（线程版：stop_event.wait 可中断退避）。
        返回 (重试成功的结果或 None, 最后错误)。"""
        last_error = dag_node.error or ""
        while dag_node.attempts < node.max_retries:
            delay = backoff_with_jitter(
                base_delay=getattr(node, "initial_delay", 1.0),
                attempt=dag_node.attempts,
                strategy=getattr(node, "backoff", "exponential"),
            )
            self._log("warning", f"Node {node.agent_name} 失败，{delay:.1f}s 后重试 (尝试 {dag_node.attempts + 1}/{node.max_retries})",
                      task_id=task.id, error=str(dag_node.error)[:100])
            # 用 stop_event.wait 替代 time.sleep，支持取消中断
            if task.stop_event.wait(delay):
                # 任务被取消，中断重试
                break
            # 全局停止信号（shutdown）补充检查：上面的 wait 只监听 per-task
            # 事件，全局停止的感知最多延迟一个退避周期
            if self._stop_event is not None and self._stop_event.is_set():
                break
            # 重试执行前再查一次取消信号，避免取消后仍发起下一次节点调用
            if task.stop_event.is_set():
                break
            dag_node.attempts += 1
            dag_node.status = "pending"
            try:
                retry_result = self.execute_node_from_scheduler(
                    task, node, input_file, plan
                )
                retry_ok, retry_err = self._evaluate_retry_result(retry_result)

                if retry_ok:
                    self._apply_node_success(task, node, dag_node, step_result, retry_result)
                    return retry_result, ""
                last_error = retry_err
                dag_node.error = retry_err
                step_result.error = retry_err
            except Exception as retry_e:
                last_error = str(retry_e)
                dag_node.error = str(retry_e)
                step_result.error = str(retry_e)
        return None, last_error

    async def _retry_node_async(self, task, node, dag_node, step_result,
                                input_file: str, plan) -> tuple[object, str]:
        """失败节点重试循环（async 版：asyncio.sleep 避免阻塞事件循环）。
        返回 (重试成功的结果或 None, 最后错误)。"""
        last_error = dag_node.error or ""
        while dag_node.attempts < node.max_retries:
            delay = backoff_with_jitter(
                base_delay=getattr(node, "initial_delay", 1.0),
                attempt=dag_node.attempts,
                strategy=getattr(node, "backoff", "exponential"),
            )
            self._log("warning", f"Node {node.agent_name} 失败，{delay:.1f}s 后重试 (尝试 {dag_node.attempts + 1}/{node.max_retries})",
                      task_id=task.id, error=str(dag_node.error)[:100])
            # 原实现用 task.stop_event.wait(delay) 会阻塞 asyncio 事件循环；
            # 改为 await asyncio.sleep 避免阻塞，随后再检查 stop 信号。
            await asyncio.sleep(delay)
            if task.stop_event.is_set():
                break
            # 全局停止信号补充检查（与线程版对齐）
            if self._stop_event is not None and self._stop_event.is_set():
                break
            dag_node.attempts += 1
            dag_node.status = "pending"
            try:
                retry_result = await asyncio.to_thread(
                    self.execute_node_from_scheduler, task, node, input_file, plan
                )
                retry_ok, retry_err = self._evaluate_retry_result(retry_result)

                if retry_ok:
                    self._apply_node_success(task, node, dag_node, step_result, retry_result)
                    return retry_result, ""
                last_error = retry_err
                dag_node.error = retry_err
                step_result.error = retry_err
            except Exception as retry_e:
                last_error = str(retry_e)
                dag_node.error = str(retry_e)
                step_result.error = str(retry_e)
        return None, last_error

    def _record_step_result(self, task, plan, node, step_result) -> None:
        """记录单节点步骤：checkpoint、审计日志、指标（as_completed 每节点 finally）"""
        step_result.finished_at = time.time()
        duration_ms = (step_result.finished_at - step_result.started_at) * 1000
        task.steps.append(step_result)
        if self._checkpoint_save:
            self._checkpoint_save(task)
        if self._audit_log:
            self._audit_log(
                task_id=task.id,
                agent_name=node.agent_name,
                input_summary=getattr(getattr(node, "agent_config", None), "config", {}),
                output_summary=step_result.result or {},
                duration_ms=duration_ms,
                status=step_result.status,
                error=step_result.error,
            )
        self._metrics.observe(
            "step_duration_ms", duration_ms,
            labels={"agent": node.agent_name, "status": step_result.status},
        )
        self._metrics.counter(
            "step_total", labels={"agent": node.agent_name},
        )
        if step_result.status != "success":
            self._metrics.counter(
                "step_failures", labels={"agent": node.agent_name, "status": step_result.status},
            )
        self._metrics.gauge(
            "pipeline_progress", task.progress,
            labels={"pipeline": getattr(plan, "pipeline_name", "")},
        )

    def execute_level(self, task, level: list, input_file: str,
                      plan, executor: ThreadPoolExecutor) -> bool:
        """执行一个 DAG 层级的所有节点，处理成功/失败/重试/熔断。
        返回 True 表示层级成功完成，False 表示需要中断（fail_fast）。"""
        from .pipeline import TaskStatus

        self._merge_resumed_nodes(task)

        if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
            task.status = TaskStatus.CANCELLED
            return False

        futures = self._submit_level_futures(task, level, input_file, plan, executor)
        processed: set = set()

        for future in as_completed(futures):
            node, dag_node, step_result = futures[future]
            processed.add(future)
            try:
                result = future.result()

                is_business_fail, biz_err = self._business_failure(result)
                if is_business_fail:
                    raise Exception(biz_err or "Agent returned failure status")

                # 成功路径
                self._apply_node_success(task, node, dag_node, step_result, result)

            except Exception as e:
                dag_node.error = str(e)

                # while 循环重试直到达到 max_retries（取消/全局停止时提前中断）
                _, last_error = self._retry_node_sync(task, node, dag_node, step_result,
                                                      input_file, plan)

                # 重试循环结束，检查是否最终失败
                if dag_node.status != "success":
                    dag_node.status = "failed"
                    dag_node.finished_at = time.time()
                    step_result.status = "failed"
                    step_result.error = last_error
                    task.error = last_error

                    if self._circuit_breaker(node, task):
                        task.status = TaskStatus.FAILED
                        # 修复 P0：f.cancel() 只能取消尚未启动的 future，
                        # 已在运行的节点不会被中断。设置 task.stop_event 软中断，
                        # 让运行中的节点在下次检查 stop_event 时主动退出。
                        task.stop_event.set()
                        self._cancel_unstarted_siblings(task, plan, futures, processed)
                        break

                    fail_fast = getattr(plan, "fail_fast", True)
                    if fail_fast:
                        task.status = TaskStatus.FAILED
                        # 修复 P0：同上，设置 stop_event 软中断已运行节点。
                        task.stop_event.set()
                        self._cancel_unstarted_siblings(task, plan, futures, processed)
                        break

            finally:
                self._record_step_result(task, plan, node, step_result)

        return task.status != TaskStatus.FAILED  # type: ignore[no-any-return]

    async def execute_level_async(self, task, level: list, input_file: str,
                                  plan) -> bool:
        """async 版 execute_level：用 asyncio.gather + asyncio.to_thread 并发执行节点。

        与 execute_level 的区别：
        - 不需要 ThreadPoolExecutor，直接用 asyncio 事件循环调度
        - 每个节点的同步 execute_node_from_scheduler 通过 asyncio.to_thread 桥接
        - 消除 asyncio.run 嵌套开销，适合在已有事件循环中调用
        返回 True 表示层级成功完成，False 表示需要中断（fail_fast）。
        """
        from .pipeline import StepResult, TaskStatus

        self._merge_resumed_nodes(task)

        if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
            task.status = TaskStatus.CANCELLED
            return False

        # 准备节点元数据
        node_meta = []
        for node in level:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
                continue
            if self._reuse_completed_node(task, node, dag_node, plan):
                continue
            dag_node.status = "running"
            dag_node.started_at = time.time()
            dag_node.attempts += 1
            step_result = StepResult(
                step_name=node.agent_name,
                agent_name=node.agent_name,
                status="running",
                started_at=time.time(),
            )
            node_meta.append((node, dag_node, step_result))

        # 并发执行所有节点（asyncio.to_thread 桥接同步调用）
        coros = [
            asyncio.to_thread(self.execute_node_from_scheduler,
                              task, node, input_file, plan)
            for node, _, _ in node_meta
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # 处理结果（与 execute_level 相同的逻辑）
        for (node, dag_node, step_result), result in zip(node_meta, results, strict=False):
            try:
                if isinstance(result, Exception):
                    raise result

                is_business_fail, biz_err = self._business_failure(result)
                if is_business_fail:
                    raise Exception(biz_err or "Agent returned failure status")

                self._apply_node_success(task, node, dag_node, step_result, result)

            except Exception as e:
                dag_node.error = str(e)

                _, last_error = await self._retry_node_async(task, node, dag_node, step_result,
                                                              input_file, plan)

                if dag_node.status != "success":
                    dag_node.status = "failed"
                    dag_node.finished_at = time.time()
                    step_result.status = "failed"
                    step_result.error = last_error
                    task.error = last_error

                    if self._circuit_breaker(node, task):
                        task.status = TaskStatus.FAILED
                        # 对齐线程版（execute_level 同分支）的软中断语义：设置
                        # stop_event 让后续重试/层级入口立即退出。async 下同层兄弟
                        # 已由 gather 启动，无法像线程版那样 cancel 未启动的 future。
                        task.stop_event.set()
                        break

                    fail_fast = getattr(plan, "fail_fast", True)
                    if fail_fast:
                        task.status = TaskStatus.FAILED
                        task.stop_event.set()
                        break

            finally:
                self._record_step_result(task, plan, node, step_result)

        return task.status != TaskStatus.FAILED  # type: ignore[no-any-return]

    # ─── 查询词提取 ─────────────────────────────

    def _extract_queries(self, input_file: str, node) -> list[str]:
        """从输入文件提取查询词（按行，过滤注释/空行/噪音行）。
        使用 per-file 缓存避免同一 level 内多个节点重复读取文件。"""
        cache_key = input_file
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        with open(input_file, encoding="utf-8") as f:
            content = f.read()
        noise = [r"^这是一个测试", r"^用于验证", r"验证流水线", r"是否正常工作",
                 r"^test\b", r"^测试\b", r"请生成", r"帮我写"]
        queries = []
        for line in content.split("\n"):
            q = line.strip()
            if not q or q.startswith("#"):
                continue
            if len(q) < 4:
                continue
            if any(re.search(p, q, re.I) for p in noise):
                continue
            queries.append(q)
        if not queries:
            queries = [node.agent_config.config.get("default_query", "Python 异步编程")]
        self._query_cache.set(cache_key, queries)
        return queries

    # ─── 任务输出读写 ─────────────────────────────

    def _get_task_output(self, task, key: str, default=None):
        """统一读取 task 输出（同时查 dag_nodes 和 result，加锁）。
        支持池化节点名（如 researcher_pool_0）自动解析为 base name。"""
        base = key.split("_pool_")[0] if "_pool_" in key else key
        lock = getattr(task, "result_lock", None)
        def _read():
            for lookup in (key, base):
                if hasattr(task, "dag_nodes") and lookup in task.dag_nodes:
                    val = task.dag_nodes[lookup].result
                    if val:
                        return val
            for lookup in (key, base):
                if hasattr(task, "result") and lookup in task.result:
                    val = task.result[lookup]
                    if val:
                        return val
            return default
        if lock:
            with lock:
                return _read()
        return _read()

    def _set_task_output(self, task, key: str, value, dag_node: bool = True):
        """统一写入 task 输出（同时写 dag_nodes 和 result，加锁）。"""
        lock = getattr(task, "result_lock", None)
        base = key.split("_pool_")[0] if "_pool_" in key else key
        def _write():
            if hasattr(task, "result"):
                task.result[key] = value
            if dag_node and hasattr(task, "dag_nodes"):
                if key in task.dag_nodes:
                    task.dag_nodes[key].result = value
                    task.dag_nodes[key].status = "success"
                elif base in task.dag_nodes:
                    task.dag_nodes[base].result = value
                    task.dag_nodes[base].status = "success"
        if lock:
            with lock:
                _write()
        else:
            _write()
        if self._logger:
            self._logger.log("debug", "set_task_output", key=key)

    def _get_latest_content(self, task, current_deps: list[str] = None) -> str:
        """从上游依赖链中获取最新的 content（按优先级：layout > quality_gate > writer）

        支持池化节点名（如 researcher_pool_0）自动解析为 base name。
        QualityGate 重做后新生成的 content 也能正确获取。
        """
        content_priority = ["layout", "quality_gate", "writer", "fact_checker"]
        checked = set()

        def _try_get(name: str) -> str:
            base = name.split("_pool_")[0] if "_pool_" in name else name
            if base in checked:
                return ""
            checked.add(base)
            for lookup in [name, base]:
                result = self._get_task_output(task, lookup, {})
                if isinstance(result, dict):
                    c = result.get("content") or result.get("optimized")
                    if c:
                        return c  # type: ignore[no-any-return]
            return ""

        for source in reversed(content_priority):
            c = _try_get(source)
            if c:
                return c

        if current_deps:
            for dep in reversed(current_deps):
                c = _try_get(dep)
                if c:
                    return c

        return ""

    def _get_dep_list_results(self, task, deps: list[str], key: str = "results") -> list:
        """统一收集依赖节点的列表结果，池化节点（如 researcher_pool_0/1）合并所有实例"""
        collected = []
        processed_nodes = set()

        def _extract(name: str) -> list:
            for store in (task.dag_nodes, task.result):
                if name in store:
                    obj = store[name]
                    result = obj.result if hasattr(obj, "result") else obj
                    if isinstance(result, dict):
                        val = result.get(key)
                        if isinstance(val, list):
                            return val
            return []

        for dep in deps:
            if dep in processed_nodes:
                continue
            if "_pool_" in dep:
                base = dep.split("_pool_")[0]
                pool_instances = [
                    d for d in deps
                    if d == base or d.startswith(base + "_pool_")
                ]
                for inst in pool_instances:
                    if inst not in processed_nodes:
                        collected.extend(_extract(inst))
                        processed_nodes.add(inst)
                processed_nodes.add(base)
            else:
                collected.extend(_extract(dep))
                processed_nodes.add(dep)
        return collected

    # ─── 熔断器 ─────────────────────────────

    def _circuit_breaker(self, node, task) -> bool:
        """Per-agent 熔断器：失败达到阈值后返回 True（表示应熔断/跳过）。
        池化节点（如 researcher_pool_0）共享 base agent 的熔断器。"""
        cb_cfg = node.agent_config.circuit_breaker
        if not cb_cfg or not cb_cfg.get("enabled", False):
            return False

        agent_name = node.agent_name.split("_pool_")[0] if "_pool_" in node.agent_name else node.agent_name
        breaker = self._cb_registry.get_or_create(
            name=agent_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            recovery_timeout=cb_cfg.get("recovery_timeout", 60),
        )

        breaker.record_failure()
        if not breaker.allow_request():
            self._log("warning", f"{agent_name} 已熔断")
            return True
        return False

    def _circuit_breaker_success(self, node):
        """node 成功时重置该 agent 的熔断计数（支持 ExecutionNode 和 TaskNode）"""
        agent_name = getattr(node, "agent_name", "")
        base_name = agent_name.split("_pool_")[0] if "_pool_" in agent_name else agent_name
        cb_cfg = None
        if hasattr(node, "agent_config") and node.agent_config:
            cb_cfg = node.agent_config.circuit_breaker
        if not cb_cfg:
            cb_cfg = getattr(node, "circuit_breaker", None)
        enabled = bool(cb_cfg and cb_cfg.get("enabled", False)) if cb_cfg else False
        if not enabled:
            return
        breaker = self._cb_registry.get_or_create(
            name=base_name,
            failure_threshold=cb_cfg.get("failure_threshold", 5) if cb_cfg else 5,
            recovery_timeout=cb_cfg.get("recovery_timeout", 60) if cb_cfg else 60,
        )
        breaker.record_success()

    # ─── 退避 ─────────────────────────────

    def _backoff_delay(self, backoff: str, initial_delay: float, attempt: int) -> float:
        """计算重试退避时间（带 jitter）"""
        return backoff_with_jitter(
            base_delay=initial_delay,
            attempt=attempt,
            strategy=backoff,
        )
