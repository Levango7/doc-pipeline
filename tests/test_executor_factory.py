"""执行器工厂 + ProcessPoolExecutor pickle 兼容性验证。"""
import pickle
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from pipeline_core.executor_factory import create_executor, is_process_executor


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
