"""执行器工厂 —— 根据配置创建 ThreadPoolExecutor 或 ProcessPoolExecutor。

配置方式：
  pipeline.yaml 中:
    execution:
      executor_type: thread   # "thread"（默认）/ "process" / "auto"

选择建议：
  - thread (默认): I/O 密集型场景（HTTP 请求、LLM 调用、文件读写）
    优势：零序列化开销、共享内存、向后兼容
  - process: CPU 密集型场景（大规模 TF-IDF、并行 LLM 生成、数值计算）
    优势：绕过 GIL、真正并行、水平扩展
    注意：提交的函数必须可 pickle（模块级函数或用 functools.partial）
  - auto: 智能切换，按任务估算成本自动选择
    短任务（< 50ms 估算）用 ThreadPool，长 CPU 密集任务用 ProcessPool
    ProcessPool 实例复用，避免重复启动开销
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

logger = logging.getLogger(__name__)

# 进程模式告警只发一次（run_plan 每层都会调用 create_executor）
_process_mode_warned = False


def create_executor(max_workers: int = 4, executor_type: str = "thread") -> ThreadPoolExecutor | ProcessPoolExecutor:
    """根据类型创建执行器。

    Args:
        max_workers: 最大 worker 数量
        executor_type: "thread"、"process" 或 "auto"

    Returns:
        ThreadPoolExecutor 或 ProcessPoolExecutor 实例
    """
    global _process_mode_warned
    if executor_type == "process":
        if not _process_mode_warned:
            logger.warning(
                "executor_type='process' 为实验性：子进程上下文重建"
                "（DAGExecutor.from_config）未在任何生产路径接线，节点会在子进程"
                "中失败并回落到父进程重试；取消信号也不会跨进程传播。"
                "建议使用 thread（默认）。"
            )
            _process_mode_warned = True
        return ProcessPoolExecutor(max_workers=max_workers)
    if executor_type == "auto":
        return SmartExecutor(max_workers=max_workers)  # type: ignore[return-value]
    return ThreadPoolExecutor(max_workers=max_workers)


def is_process_executor(executor) -> bool:
    """判断是否为 ProcessPoolExecutor"""
    return isinstance(executor, ProcessPoolExecutor)


# ── SmartExecutor ──────────────────────────────────────────────

# 估算阈值：单任务估算耗时超过此值才用 ProcessPool
# 进程启动开销约 0.3-0.8s，需任务耗时 > 0.5s 才能摊薄
PROCESS_COST_THRESHOLD = 0.5  # 秒

# 进程池复用：全局单例，避免每次创建/销毁
_process_pool_singleton: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_ref_count = 0


def _get_or_create_process_pool(max_workers: int = 4) -> ProcessPoolExecutor:
    """获取或创建全局复用的 ProcessPoolExecutor。"""
    global _process_pool_singleton
    with _pool_lock:
        if _process_pool_singleton is None:
            _process_pool_singleton = ProcessPoolExecutor(max_workers=max_workers)
        return _process_pool_singleton


def _estimate_task_cost(func: Callable, args: tuple, kwargs: dict) -> float:
    """粗略估算单任务执行耗时（秒）。

    启发式规则：
    1. 函数有 __cost_estimate__ 属性 → 直接使用
    2. 函数名含已知 CPU 密集关键词 → 估算 1.0s
    3. 函数名含已知 I/O 关键词 → 估算 0.01s
    4. 默认 → 0.1s（中性估计）
    """
    if hasattr(func, "__cost_estimate__"):
        return float(func.__cost_estimate__)

    name = getattr(func, "__name__", "")
    name_lower = name.lower()

    # 已知 CPU 密集型函数
    cpu_keywords = ("tfidf", "tf_idf", "score", "compute", "calculate",
                    "transform", "encode", "decode", "parse", "analyze",
                    "_cpu_work", "cpu_intensive", "vectorize", "normalize")
    if any(kw in name_lower for kw in cpu_keywords):
        return 1.0

    # 已知 I/O 密集型函数
    io_keywords = ("fetch", "read", "write", "download", "upload",
                   "request", "query", "search", "load", "save", "send")
    if any(kw in name_lower for kw in io_keywords):
        return 0.01

    # 默认中性估计
    return 0.1


class SmartExecutor:
    """智能执行器 —— 根据任务特性自动选择 ThreadPool 或 ProcessPool。

    核心机制：
    1. submit() 时估算单任务耗时
    2. 若 max(估算) * n_tasks > PROCESS_COST_THRESHOLD 且函数可 pickle → 用 ProcessPool
    3. 否则用 ThreadPool（零启动开销）
    4. ProcessPool 全局复用，避免重复创建/销毁
    5. 上下文管理器退出时不关闭复用的 ProcessPool

    用法:
        with SmartExecutor(max_workers=4) as pool:
            futures = [pool.submit(fn, arg) for arg in args]
            results = [f.result() for f in futures]
    """

    def __init__(self, max_workers: int = 4, cost_threshold: float = PROCESS_COST_THRESHOLD):
        self.max_workers = max_workers
        self.cost_threshold = cost_threshold
        self._thread_pool: ThreadPoolExecutor | None = None
        self._owns_process_pool = False
        self._submitted_count = 0
        self._used_process = False

    def _select_executor(self, func: Callable, n_tasks: int = 1) -> ThreadPoolExecutor | ProcessPoolExecutor:
        """根据任务特性选择执行器。"""
        est_cost = _estimate_task_cost(func, (), {})
        total_est = est_cost * n_tasks

        if total_est >= self.cost_threshold and _is_picklable(func):
            pool = _get_or_create_process_pool(self.max_workers)
            self._used_process = True
            return pool

        if self._thread_pool is None:
            self._thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)
        return self._thread_pool

    def submit(self, fn: Callable, *args, **kwargs):
        """提交任务，自动选择执行器。"""
        executor = self._select_executor(fn, n_tasks=1)
        self._submitted_count += 1
        return executor.submit(fn, *args, **kwargs)

    def map(self, fn: Callable, *iterables, timeout=None, chunksize=1):
        """批量提交，自动选择执行器。"""
        n_tasks = min(len(it) for it in iterables) if iterables else 1
        executor = self._select_executor(fn, n_tasks=n_tasks)
        self._submitted_count += n_tasks
        return executor.map(fn, *iterables, timeout=timeout, chunksize=chunksize)

    def shutdown(self, wait: bool = True):
        """关闭线程池。ProcessPool 全局复用不关闭。"""
        if self._thread_pool:
            self._thread_pool.shutdown(wait=wait)
            self._thread_pool = None

    @property
    def used_process(self) -> bool:
        """是否使用了 ProcessPool。"""
        return self._used_process

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(wait=True)
        return False


def _is_picklable(func: Callable) -> bool:
    """检查函数是否可 pickle（ProcessPool 需要）。"""
    try:
        import pickle
        pickle.dumps(func)
        return True
    except Exception:
        return False
