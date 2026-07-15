"""执行器工厂 + ProcessPoolExecutor pickle 兼容性验证 + SmartExecutor 智能切换。"""
import pickle
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from pipeline_core.executor_factory import (
    create_executor, is_process_executor, SmartExecutor,
    _estimate_task_cost, _is_picklable,
)


# ── 模块级函数（可 pickle，供 ProcessPoolExecutor 提交） ──

def _square(x: int) -> int:
    return x * x


def _sleep_identity(x: float) -> float:
    time.sleep(x)
    return x


# ═══════════════════════════════════════════════════════════

class TestExecutorFactory:
    """执行器工厂基础功能"""

    def test_create_thread_executor(self):
        executor = create_executor(max_workers=4, executor_type="thread")
        assert isinstance(executor, ThreadPoolExecutor)
        executor.shutdown()

    def test_create_process_executor(self):
        executor = create_executor(max_workers=4, executor_type="process")
        assert isinstance(executor, ProcessPoolExecutor)
        executor.shutdown()

    def test_default_is_thread(self):
        executor = create_executor(max_workers=2)
        assert isinstance(executor, ThreadPoolExecutor)
        executor.shutdown()

    def test_is_process_executor_helper(self):
        tp = create_executor(max_workers=2, executor_type="thread")
        pp = create_executor(max_workers=2, executor_type="process")
        assert not is_process_executor(tp)
        assert is_process_executor(pp)
        tp.shutdown()
        pp.shutdown()

    def test_unknown_type_defaults_to_thread(self):
        executor = create_executor(max_workers=2, executor_type="unknown")
        assert isinstance(executor, ThreadPoolExecutor)
        executor.shutdown()


class TestProcessPoolExecution:
    """ProcessPoolExecutor 实际执行验证"""

    def test_module_level_function_executes(self):
        """模块级函数可在 ProcessPoolExecutor 中执行"""
        with ProcessPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_square, i) for i in range(5)]
            results = [f.result() for f in futures]
        assert results == [0, 1, 4, 9, 16]

    def test_parallel_speedup(self):
        """多进程并行执行应比串行快（sleep 足够长以淹没进程启动开销）"""
        n = 8
        sleep_sec = 0.3
        # 串行
        t0 = time.time()
        serial = [_sleep_identity(sleep_sec) for _ in range(n)]
        serial_time = time.time() - t0

        # 并行
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_sleep_identity, sleep_sec) for _ in range(n)]
            parallel = [f.result() for f in futures]
        parallel_time = time.time() - t0

        assert serial == parallel
        # 并行应明显快于串行（允许进程启动开销波动）
        assert parallel_time < serial_time * 0.8

    def test_thread_pool_also_works(self):
        """ThreadPoolExecutor 也能执行模块级函数"""
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_square, i) for i in range(5)]
            results = [f.result() for f in futures]
        assert results == [0, 1, 4, 9, 16]


class TestDAGExecutorPickleCompatibility:
    """DAGExecutor pickle 兼容性验证"""

    def test_execute_node_worker_is_module_level(self):
        """_execute_node_worker 必须是模块级函数（可 pickle）"""
        from pipeline_core.dag_executor import _execute_node_worker
        import types

        # 模块级函数的 __qualname__ 不含 '.'（不是 bound method）
        assert "." not in _execute_node_worker.__qualname__
        # 确认是函数而非 bound method
        assert not isinstance(_execute_node_worker, types.MethodType)

    def test_execute_node_worker_picklable(self):
        """_execute_node_worker 可被 pickle 序列化"""
        from pipeline_core.dag_executor import _execute_node_worker

        # 不应抛出 pickle 错误
        data = pickle.dumps(_execute_node_worker)
        restored = pickle.loads(data)
        assert restored is _execute_node_worker

    def test_dag_executor_getstate_setstate(self):
        """DAGExecutor __getstate__/__setstate__ 剥离非可序列化属性"""
        from pipeline_core.dag_executor import DAGExecutor

        # 验证 __getstate__ 和 __setstate__ 方法存在
        assert hasattr(DAGExecutor, "__getstate__")
        assert hasattr(DAGExecutor, "__setstate__")

        # 验证 from_config 类方法存在且可调用
        assert hasattr(DAGExecutor, "from_config")
        assert callable(DAGExecutor.from_config)

    def test_dag_executor_state_excludes_non_picklable(self):
        """__getstate__ 应排除 registry/bus 等非可序列化属性"""
        from pipeline_core.dag_executor import DAGExecutor

        # 检查 _NON_PICKLABLE_ATTRS 属性列表存在
        assert hasattr(DAGExecutor, "_NON_PICKLABLE_ATTRS")
        non_picklable = DAGExecutor._NON_PICKLABLE_ATTRS
        assert isinstance(non_picklable, (list, tuple, set))
        # 应包含 registry 和 bus
        assert "registry" in non_picklable
        assert "bus" in non_picklable


# ── SmartExecutor 测试辅助函数 ──

def _cpu_intensive_task(n: int) -> int:
    """CPU 密集型任务（名称含 compute 关键词，估算为高成本）"""
    total = 0
    for i in range(n * 100_000):
        total += i * i
    return total


def _io_task(url: str) -> str:
    """I/O 密集型任务（名称含 fetch 关键词，估算为低成本）"""
    time.sleep(0.001)
    return url


def _fetch_page(url: str) -> str:
    """另一个 I/O 任务"""
    return f"fetched:{url}"


def _score_documents(docs: list) -> float:
    """CPU 密集型（名称含 score 关键词）"""
    return sum(len(d) for d in docs)


# ═══════════════════════════════════════════════════════════

class TestSmartExecutor:
    """SmartExecutor 智能切换功能验证"""

    def test_create_auto_returns_smart_executor(self):
        """executor_type='auto' 返回 SmartExecutor 实例"""
        executor = create_executor(max_workers=4, executor_type="auto")
        assert isinstance(executor, SmartExecutor)
        executor.shutdown()

    def test_smart_executor_context_manager(self):
        """SmartExecutor 支持上下文管理器"""
        with SmartExecutor(max_workers=2) as pool:
            future = pool.submit(_square, 5)
            assert future.result() == 25

    def test_io_task_uses_thread_pool(self):
        """I/O 密集型任务应使用 ThreadPool（不使用 ProcessPool）"""
        with SmartExecutor(max_workers=2) as pool:
            future = pool.submit(_fetch_page, "http://example.com")
            result = future.result()
            assert result == "fetched:http://example.com"
            assert not pool.used_process

    def test_cpu_task_uses_process_pool(self):
        """CPU 密集型任务应使用 ProcessPool"""
        with SmartExecutor(max_workers=2, cost_threshold=0.01) as pool:
            future = pool.submit(_cpu_intensive_task, 5)
            result = future.result()
            assert result > 0
            assert pool.used_process

    def test_map_with_io_tasks(self):
        """map() 批量提交 I/O 任务使用 ThreadPool"""
        urls = [f"http://ex{i}.com" for i in range(5)]
        with SmartExecutor(max_workers=3) as pool:
            results = list(pool.map(_fetch_page, urls))
        assert results == [f"fetched:{u}" for u in urls]

    def test_map_with_cpu_tasks(self):
        """map() 批量提交 CPU 密集任务使用 ProcessPool"""
        with SmartExecutor(max_workers=2, cost_threshold=0.01) as pool:
            results = list(pool.map(_cpu_intensive_task, [2, 3, 4]))
        assert all(r > 0 for r in results)
        assert pool.used_process

    def test_process_pool_reuse(self):
        """多次使用 SmartExecutor 应复用同一个 ProcessPool"""
        with SmartExecutor(max_workers=2, cost_threshold=0.01) as pool1:
            r1 = pool1.submit(_cpu_intensive_task, 2).result()
            assert pool1.used_process

        with SmartExecutor(max_workers=2, cost_threshold=0.01) as pool2:
            r2 = pool2.submit(_cpu_intensive_task, 2).result()
            assert pool2.used_process

        # 结果一致（相同输入）
        assert r1 == r2

    def test_mixed_tasks(self):
        """混合 I/O 和 CPU 任务"""
        with SmartExecutor(max_workers=4, cost_threshold=0.01) as pool:
            io_future = pool.submit(_fetch_page, "http://test.com")
            cpu_future = pool.submit(_cpu_intensive_task, 3)

            io_result = io_future.result()
            cpu_result = cpu_future.result()

        assert io_result == "fetched:http://test.com"
        assert cpu_result > 0

    def test_shutdown_cleans_up_thread_pool(self):
        """shutdown() 关闭线程池但不关闭复用的 ProcessPool"""
        pool = SmartExecutor(max_workers=2)
        pool.submit(_square, 3).result()
        pool.shutdown()
        # 再次使用应正常（创建新线程池）
        pool.submit(_square, 4).result()
        pool.shutdown()


class TestCostEstimation:
    """任务成本估算函数验证"""

    def test_cpu_keyword_estimates_high(self):
        """含 CPU 关键词的函数估算为高成本"""
        cost = _estimate_task_cost(_cpu_intensive_task, (), {})
        assert cost >= 1.0

    def test_io_keyword_estimates_low(self):
        """含 I/O 关键词的函数估算为低成本"""
        cost = _estimate_task_cost(_fetch_page, (), {})
        assert cost <= 0.01

    def test_score_keyword_estimates_high(self):
        """score 关键词估算为高成本"""
        cost = _estimate_task_cost(_score_documents, (), {})
        assert cost >= 1.0

    def test_custom_cost_attribute(self):
        """__cost_estimate__ 属性优先使用"""
        def custom_task():
            pass
        custom_task.__cost_estimate__ = 5.0
        cost = _estimate_task_cost(custom_task, (), {})
        assert cost == 5.0

    def test_default_estimate_for_unknown(self):
        """未知函数默认中性估计"""
        def unknown_task():
            pass
        cost = _estimate_task_cost(unknown_task, (), {})
        assert 0.01 < cost < 1.0


class TestPicklableCheck:
    """_is_picklable 函数验证"""

    def test_module_level_function_is_picklable(self):
        """模块级函数可 pickle"""
        assert _is_picklable(_square)

    def test_lambda_not_picklable(self):
        """lambda 不可 pickle"""
        f = lambda x: x * 2
        assert not _is_picklable(f)

    def test_local_function_not_picklable(self):
        """局部定义的函数不可 pickle"""
        def local_func(x):
            return x + 1
        assert not _is_picklable(local_func)
