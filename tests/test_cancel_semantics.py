"""P0-1 取消语义测试。

覆盖：
  1. PipelineTask pickle 往返后同步原语（stop_event/result_lock）重建可用；
  2. 节点重试循环响应全局停止信号（shutdown 后不再发起重试）；
  3. 节点重试循环响应 per-task 取消（wait 立即唤醒，回归保护）；
  4. execute_level_async fail_fast 后设置 stop_event（与线程版语义对齐）。
"""
import asyncio
import pickle
import threading
import time
from types import SimpleNamespace

from pipeline_core.dag_executor import DAGExecutor
from pipeline_core.executor_factory import create_executor
from pipeline_core.pipeline import PipelineTask, TaskNode, TaskStatus


class _FakeLogger:
    def log(self, level, msg, **kw):
        pass


class _FakeMetrics:
    def observe(self, *args, **kwargs):
        pass

    def counter(self, *args, **kwargs):
        pass

    def gauge(self, *args, **kwargs):
        pass


class _FakeRegistry:
    def get_instance(self, name):
        return object()

    def set_status(self, name, status):
        pass

    def get_meta(self, name):
        return None


class _FailingBus:
    """bus.request 直接抛异常 → 触发节点失败 → 重试路径"""

    def __init__(self):
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1
        raise RuntimeError("boom")


class _BusinessFailBus:
    """bus.request 返回业务失败 status → 触发 is_business_fail 分支"""

    def __init__(self):
        self.calls = 0

    def request(self, **kwargs):
        self.calls += 1
        return {"status": "fail", "message": "business fail"}


def _make_executor(global_stop=None, bus=None) -> DAGExecutor:
    return DAGExecutor(
        registry=_FakeRegistry(), bus=bus or _FailingBus(),
        cb_registry=None, rate_limiters=None, metrics=_FakeMetrics(),
        logger=_FakeLogger(), stop_event=global_stop,
    )


def _make_node(**overrides) -> TaskNode:
    kwargs = dict(name="a", agent_name="a", dependencies=[],
                  max_retries=10, initial_delay=1.0, backoff="fixed")
    kwargs.update(overrides)
    return TaskNode(**kwargs)


def _make_task(node: TaskNode) -> PipelineTask:
    task = PipelineTask(id="t1", pipeline_name="docgen", input_file="in.md", config={})
    task.dag_nodes[node.name] = node
    return task


def _make_plan():
    return SimpleNamespace(pipeline_name="docgen", checkpoint={}, fail_fast=True, raw={})


class TestPickleRoundtrip:
    def test_stop_event_and_lock_rebuilt_after_unpickle(self):
        task = PipelineTask(id="pk1", pipeline_name="p", input_file="in.md", config={})
        task.dag_nodes["a"] = _make_node()

        restored = pickle.loads(pickle.dumps(task))

        assert restored.stop_event is not None
        assert restored.result_lock is not None
        assert not restored.stop_event.is_set()
        restored.stop_event.set()
        assert restored.stop_event.wait(0)
        with restored.result_lock:
            restored.result["k"] = "v"
        assert restored.result["k"] == "v"


class TestRetryCancellation:
    def test_retry_loop_stops_on_global_shutdown(self):
        bus = _FailingBus()
        global_stop = threading.Event()
        ex = _make_executor(global_stop=global_stop, bus=bus)
        node = _make_node()
        task = _make_task(node)

        threading.Timer(0.05, global_stop.set).start()
        start = time.monotonic()
        with create_executor(max_workers=2) as executor:
            ret = ex.execute_level(task, [node], "in.md", _make_plan(), executor)
        elapsed = time.monotonic() - start

        assert ret is False
        assert task.status == TaskStatus.FAILED
        # 修复前：全局停止不被感知，会继续重试至 max_retries=10（约 9s 退避）
        assert bus.calls == 1
        assert elapsed < 4.0

    def test_retry_loop_wakes_immediately_on_task_cancel(self):
        bus = _FailingBus()
        ex = _make_executor(bus=bus)
        node = _make_node()
        task = _make_task(node)

        threading.Timer(0.2, task.stop_event.set).start()
        start = time.monotonic()
        with create_executor(max_workers=2) as executor:
            ret = ex.execute_level(task, [node], "in.md", _make_plan(), executor)
        elapsed = time.monotonic() - start

        assert ret is False
        assert bus.calls == 1
        assert elapsed < 2.0


class TestAsyncFailFastParity:
    def test_async_fail_fast_sets_stop_event(self):
        bus = _BusinessFailBus()
        ex = _make_executor(bus=bus)
        node = _make_node(max_retries=1)
        task = _make_task(node)

        ret = asyncio.run(ex.execute_level_async(task, [node], "in.md", _make_plan()))

        assert ret is False
        assert task.status == TaskStatus.FAILED
        # 修复前：async 分支不设置 stop_event（与线程版不一致）
        assert task.stop_event.is_set()
