"""DAG 执行器 —— 负责 DAG 构建、节点调度、熔断器、限流、指标"""
from __future__ import annotations
import re
import copy
import time
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable

from .circuit_breaker import backoff_with_jitter
from .cache_manager import CacheManager


# ─── 模块级函数：支持 ProcessPoolExecutor pickle ──────────────────

def _execute_node_worker(dag_executor, task, node, input_file, plan):
    """模块级节点执行函数 —— 支持 ProcessPoolExecutor pickle。

    当使用 ProcessPoolExecutor 时，dag_executor 会被 pickle 传输到子进程。
    非可序列化属性（registry, bus 等）会被置为 None（通过 __getstate__），
    子进程需通过 DAGExecutor.from_config() 重建上下文。

    若 registry/bus 为 None，说明子进程未重建上下文，抛出明确错误。
    """
    if dag_executor.registry is None or dag_executor.bus is None:
        raise RuntimeError(
            "ProcessPoolExecutor 模式下 registry/bus 不可用。"
            "子进程需通过 DAGExecutor.from_config() 重建上下文，"
            "或使用 ThreadPoolExecutor（默认）模式。"
        )
    return dag_executor.execute_node_from_scheduler(task, node, input_file, plan)


class DAGExecutor:
    """DAG 构建和执行"""

    def __init__(self, registry, bus, cb_registry, rate_limiters, metrics, logger=None,
                 stop_event: Optional[threading.Event] = None,
                 checkpoint_save_fn: Optional[Callable] = None,
                 audit_log_fn: Optional[Callable] = None):
        self.registry = registry
        self.bus = bus
        self._cb_registry = cb_registry
        self._rate_limiters = rate_limiters
        self._metrics = metrics
        self._logger = logger
        self._stop_event = stop_event
        self._checkpoint_save = checkpoint_save_fn
        self._audit_log = audit_log_fn
        self._execution_stats: list[dict] = []
        self._query_cache = CacheManager(name="dag_queries", max_size=100, ttl=3600)

    # ─── pickle 支持（ProcessPoolExecutor 兼容） ─────────────

    # 非可序列化属性列表：pickle 时置 None，子进程需通过 from_config() 重建
    _NON_PICKLABLE_ATTRS = (
        "registry", "bus", "_logger", "_stop_event",
        "_checkpoint_save", "_audit_log",
    )

    def __getstate__(self):
        """pickle 时剥离非可序列化属性，其余正常序列化。"""
        state = self.__dict__.copy()
        for attr in self._NON_PICKLABLE_ATTRS:
            state[attr] = None
        return state

    def __setstate__(self, state):
        """从 pickle 恢复，非可序列化属性为 None（子进程需自行重建）。"""
        self.__dict__.update(state)

    @classmethod
    def from_config(cls, config: dict, registry, bus, cb_registry, rate_limiters, metrics,
                    logger=None, stop_event=None, checkpoint_save_fn=None, audit_log_fn=None):
        """从配置重建 DAGExecutor —— 供 ProcessPoolExecutor 子进程使用。

        在子进程中，通过此方法用从父进程 pickle 传来的 config 重建完整上下文：
            executor = DAGExecutor.from_config(config, registry, bus, ...)
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

    def _acquire_rate_limit(self, agent_name: str, rate_limit_cfg: Optional[dict] = None,
                             timeout: float = 30.0) -> bool:
        """获取限流令牌。未配置限流时直接放行。"""
        cfg = rate_limit_cfg or {}
        rate = float(cfg.get("rate", 0))
        if rate <= 0:
            return True  # 不限流
        burst = int(cfg.get("burst", rate * 2))
        limiter = self._rate_limiters.get_or_create(agent_name, rate=rate, burst=burst)
        return limiter.acquire(1, block=True, timeout=timeout)

    # ─── DAG 构建 ─────────────────────────────

    def build_dag(self, agent_order: list[str], config: Optional[dict] = None) -> tuple[dict, list[list[str]]]:
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
        dep_results = {}
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

        msg_payload = {
            "task_id": task.id,
            "input_file": input_file,
            "config": node.agent_config.config,
            "pipeline": plan.pipeline_name,
            "node": node.agent_name,
            "dependencies_results": dep_results_raw,
            "queries": queries,
            "target_file": output_file,
            "target": output_file,
            "results": research_results,
            "articles": articles,
            "content": writer_content,
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

    def execute_level(self, task, level: list, input_file: str,
                      plan, executor: ThreadPoolExecutor) -> bool:
        """执行一个 DAG 层级的所有节点，处理成功/失败/重试/熔断。
        返回 True 表示层级成功完成，False 表示需要中断（fail_fast）。"""
        from .pipeline import StepResult, TaskStatus

        if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
            task.status = TaskStatus.CANCELLED
            return False

        futures = {}
        for node in level:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
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

            from .executor_factory import is_process_executor

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

        for future in as_completed(futures):
            node, dag_node, step_result = futures[future]
            result = None
            last_error = ""
            try:
                result = future.result()

                is_business_fail = False
                if isinstance(result, dict):
                    sem_status = result.get("status")
                    if sem_status in ("blocked", "fail"):
                        is_business_fail = True
                        last_error = result.get("message", result.get("error", f"Agent returned {sem_status}"))

                if is_business_fail:
                    raise Exception(last_error or "Agent returned failure status")

                # 成功路径
                dag_node.result = result
                dag_node.status = "success"
                dag_node.finished_at = time.time()
                step_result.status = "success"
                step_result.result = result
                self._set_task_output(task, node.agent_name, result)
                self._circuit_breaker_success(node)

            except Exception as e:
                dag_node.error = str(e)
                last_error = str(e)

                # while 循环重试直到达到 max_retries
                while dag_node.attempts < node.max_retries:
                    delay = backoff_with_jitter(
                        base_delay=getattr(node, "initial_delay", 1.0),
                        attempt=dag_node.attempts,
                        strategy=getattr(node, "backoff", "exponential"),
                    )
                    self._log("warning", f"Node {node.agent_name} 失败，{delay:.1f}s 后重试 (尝试 {dag_node.attempts + 1}/{node.max_retries})",
                              task_id=task.id, error=str(e)[:100])
                    # 用 stop_event.wait 替代 time.sleep，支持取消中断
                    if task.stop_event.wait(delay):
                        # 任务被取消，中断重试
                        break
                    dag_node.attempts += 1
                    dag_node.status = "pending"
                    try:
                        retry_result = self.execute_node_from_scheduler(
                            task, node, input_file, plan
                        )
                        retry_ok = True
                        retry_err = ""
                        if not retry_result or "error" in retry_result:
                            retry_ok = False
                            retry_err = retry_result.get("error", "retry failed") if retry_result else "retry failed"
                        elif isinstance(retry_result, dict):
                            sem_status = retry_result.get("status")
                            if sem_status in ("blocked", "fail"):
                                retry_ok = False
                                retry_err = retry_result.get("message", retry_result.get("error", f"Agent returned {sem_status}"))

                        if retry_ok:
                            result = retry_result
                            dag_node.result = retry_result
                            dag_node.status = "success"
                            dag_node.finished_at = time.time()
                            dag_node.error = ""
                            step_result.status = "success"
                            step_result.result = retry_result
                            step_result.error = ""
                            self._set_task_output(task, node.agent_name, retry_result)
                            self._circuit_breaker_success(node)
                            break
                        else:
                            last_error = retry_err
                            dag_node.error = retry_err
                            step_result.error = retry_err
                    except Exception as retry_e:
                        last_error = str(retry_e)
                        dag_node.error = str(retry_e)
                        step_result.error = str(retry_e)

                # 重试循环结束，检查是否最终失败
                if dag_node.status != "success":
                    dag_node.status = "failed"
                    dag_node.finished_at = time.time()
                    step_result.status = "failed"
                    step_result.error = last_error
                    task.error = last_error

                    if self._circuit_breaker(node, task):
                        task.status = TaskStatus.FAILED
                        for f in futures:
                            f.cancel()
                        break

                    fail_fast = getattr(plan, "fail_fast", True)
                    if fail_fast:
                        task.status = TaskStatus.FAILED
                        for f in futures:
                            f.cancel()
                        break

            finally:
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

        if task.status == TaskStatus.FAILED:
            return False
        return True

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

        if task.stop_event.is_set() or (self._stop_event and self._stop_event.is_set()):
            task.status = TaskStatus.CANCELLED
            return False

        # 准备节点元数据
        node_meta = []
        for node in level:
            dag_node = task.dag_nodes.get(node.agent_name)
            if dag_node is None:
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
        for (node, dag_node, step_result), result in zip(node_meta, results):
            last_error = ""
            try:
                if isinstance(result, Exception):
                    raise result

                is_business_fail = False
                if isinstance(result, dict):
                    sem_status = result.get("status")
                    if sem_status in ("blocked", "fail"):
                        is_business_fail = True
                        last_error = result.get("message", result.get("error", f"Agent returned {sem_status}"))

                if is_business_fail:
                    raise Exception(last_error or "Agent returned failure status")

                dag_node.result = result
                dag_node.status = "success"
                dag_node.finished_at = time.time()
                step_result.status = "success"
                step_result.result = result
                self._set_task_output(task, node.agent_name, result)
                self._circuit_breaker_success(node)

            except Exception as e:
                dag_node.error = str(e)
                last_error = str(e)

                while dag_node.attempts < node.max_retries:
                    delay = backoff_with_jitter(
                        base_delay=getattr(node, "initial_delay", 1.0),
                        attempt=dag_node.attempts,
                        strategy=getattr(node, "backoff", "exponential"),
                    )
                    self._log("warning", f"Node {node.agent_name} 失败，{delay:.1f}s 后重试 (尝试 {dag_node.attempts + 1}/{node.max_retries})",
                              task_id=task.id, error=str(e)[:100])
                    # 原实现用 task.stop_event.wait(delay) 会阻塞 asyncio 事件循环；
                    # 改为 await asyncio.sleep 避免阻塞，随后再检查 stop 信号。
                    await asyncio.sleep(delay)
                    if task.stop_event.is_set():
                        break
                    dag_node.attempts += 1
                    dag_node.status = "pending"
                    try:
                        retry_result = await asyncio.to_thread(
                            self.execute_node_from_scheduler, task, node, input_file, plan
                        )
                        retry_ok = True
                        retry_err = ""
                        if not retry_result or "error" in retry_result:
                            retry_ok = False
                            retry_err = retry_result.get("error", "retry failed") if retry_result else "retry failed"
                        elif isinstance(retry_result, dict):
                            sem_status = retry_result.get("status")
                            if sem_status in ("blocked", "fail"):
                                retry_ok = False
                                retry_err = retry_result.get("message", retry_result.get("error", f"Agent returned {sem_status}"))

                        if retry_ok:
                            result = retry_result
                            dag_node.result = retry_result
                            dag_node.status = "success"
                            dag_node.finished_at = time.time()
                            dag_node.error = ""
                            step_result.status = "success"
                            step_result.result = retry_result
                            step_result.error = ""
                            self._set_task_output(task, node.agent_name, retry_result)
                            self._circuit_breaker_success(node)
                            break
                        else:
                            last_error = retry_err
                            dag_node.error = retry_err
                            step_result.error = retry_err
                    except Exception as retry_e:
                        last_error = str(retry_e)
                        dag_node.error = str(retry_e)
                        step_result.error = str(retry_e)

                if dag_node.status != "success":
                    dag_node.status = "failed"
                    dag_node.finished_at = time.time()
                    step_result.status = "failed"
                    step_result.error = last_error
                    task.error = last_error

                    if self._circuit_breaker(node, task):
                        task.status = TaskStatus.FAILED
                        break

                    fail_fast = getattr(plan, "fail_fast", True)
                    if fail_fast:
                        task.status = TaskStatus.FAILED
                        break

            finally:
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

        if task.status == TaskStatus.FAILED:
            return False
        return True

    # ─── 查询词提取 ─────────────────────────────

    def _extract_queries(self, input_file: str, node) -> list[str]:
        """从输入文件提取查询词（按行，过滤注释/空行/噪音行）。
        使用 per-file 缓存避免同一 level 内多个节点重复读取文件。"""
        cache_key = input_file
        cached = self._query_cache.get(cache_key)
        if cached is not None:
            return cached
        with open(input_file, "r", encoding="utf-8") as f:
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
        content_priority = ["layout", "quality_gate", "writer"]
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
                        return c
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