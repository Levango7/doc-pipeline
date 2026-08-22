"""process 执行模式 —— 子进程上下文重建

覆盖三层验证：
  1. DAGExecutor pickle 往返：非序列化属性剥离 + 子进程侧按需重建
  2. worker 入口：缺 child_context 时明确报错
  3. 真实跨进程执行：ProcessPoolExecutor 子进程内重建 Agent 上下文并返回结果

测试原则：不 mock 进程池（第 3 层用真实 ProcessPoolExecutor，Windows spawn 模式）。
"""
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.circuit_breaker import CircuitBreakerRegistry
from pipeline_core.dag_executor import (
    DAGExecutor,
    _build_child_context,
    _execute_node_worker,
)
from pipeline_core.observability import get_metrics
from pipeline_core.rate_limiter import RateLimiterRegistry

# ── 1. pickle 往返 ──────────────────────────────

def _make_executor() -> DAGExecutor:
    return DAGExecutor(
        registry=object(),  # 占位：真实场景为 Registry 实例（本身含线程，不可 pickle）
        bus=object(),
        cb_registry=CircuitBreakerRegistry(),
        rate_limiters=RateLimiterRegistry(),
        metrics=get_metrics(),
        logger=None,
    )


class TestPickleRoundTrip:

    def test_dumps_succeeds_with_unpicklable_components(self):
        """熔断器/限流器等含线程锁的组件不再导致 pickle 失败"""
        executor = _make_executor()
        data = pickle.dumps(executor)  # 修复前此处抛 PicklingError
        assert isinstance(data, bytes)

    def test_roundtrip_strips_and_rebuilds_components(self):
        """往返后 registry/bus 为 None，其余组件重建为新实例"""
        restored = pickle.loads(pickle.dumps(_make_executor()))
        assert restored.registry is None
        assert restored.bus is None
        # 剥离的组件被 __setstate__ 重建为可用对象
        assert restored._cb_registry is not None
        assert restored._rate_limiters is not None
        assert restored._metrics is not None
        assert restored._query_cache is not None

    def test_child_context_survives_pickle(self):
        """child_context 配置（纯数据）经 pickle 保留"""
        executor = _make_executor()
        executor.child_context = {"agents_dir": "/tmp/x", "agent_names": ["a"], "config": {}}
        restored = pickle.loads(pickle.dumps(executor))
        assert restored.child_context == {
            "agents_dir": "/tmp/x", "agent_names": ["a"], "config": {}
        }


# ── 2. worker 入口守卫 ──────────────────────────────

class TestWorkerGuard:

    def test_worker_raises_without_child_context(self):
        """无 child_context 时给出明确指引而非静默失败"""
        stripped = pickle.loads(pickle.dumps(_make_executor()))
        with pytest.raises(RuntimeError, match="child_context"):
            _execute_node_worker(stripped, None, None, "", None)


# ── 3. 真实跨进程执行 ──────────────────────────────

PROBE_AGENT = '''"""子进程上下文验证用探针 Agent（由测试动态生成）"""
import os

from pipeline_core.base_agent import BaseAgent

AGENT_NAME = "proc_probe"
AGENT_VERSION = "1.0"
AGENT_DESC = "回显 pid 与 payload 的最小 Agent"
AGENT_AUTHOR = "test"
AGENT_PRIORITY = 99
INPUT_TOPICS = ["proc_probe.input"]
OUTPUT_TOPICS = ["proc_probe.done"]
DEPENDENCIES = []
CACHE_TTL = 0
RESPAWN = False


class ProcProbeAgent(BaseAgent):
    def handle(self, msg):
        payload = getattr(msg, "payload", {}) or {}
        return {"status": "ok", "pid": os.getpid(), "echo": payload.get("echo", "")}
'''


@pytest.fixture
def probe_env(tmp_path):
    """生成探针 agent 目录 + 携带 child_context 的已剥离 executor"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    (agents_dir / "proc_probe.py").write_text(PROBE_AGENT, encoding="utf-8")

    executor = _make_executor()
    executor.child_context = {
        "agents_dir": str(agents_dir),
        "agent_names": ["proc_probe"],
        # 重定向子进程内 agent 的缓存/日志目录，且静默日志
        "config": {
            "cache_dir": str(runtime_dir / "cache"),
            "log_dir": str(runtime_dir / "logs"),
            "quiet": True,
        },
    }
    stripped = pickle.loads(pickle.dumps(executor))
    return agents_dir, stripped


class TestRealSubprocessExecution:

    def test_build_child_context_loads_probe(self, probe_env):
        """上下文构建：registry 中能取到探针 agent 实例"""
        agents_dir, _ = probe_env
        registry, bus = _build_child_context(
            {"agents_dir": str(agents_dir), "agent_names": ["proc_probe"], "config": {}}
        )
        instance = registry.get_instance("proc_probe")
        assert instance is not None
        assert hasattr(instance, "handle")
        bus.shutdown()

    def test_node_runs_in_separate_process(self, probe_env):
        """端到端：worker 在真实子进程中执行节点并返回结果

        断言返回的 pid 与父进程不同 → 执行确实发生在子进程。
        """
        _, stripped = probe_env

        from pipeline_core.pipeline import PipelineTask, TaskNode

        task = PipelineTask(id="proc_test", pipeline_name="proc_test",
                            input_file="", config={})
        node = TaskNode(name="proc_probe", agent_name="proc_probe")
        # 生产流程在提交前会把节点注册进 task.dag_nodes（execute_node 内部按名取 attempts）
        node.attempts = 1
        task.dag_nodes["proc_probe"] = node
        plan = SimpleNamespace(pipeline_name="proc_test", raw={})

        with ProcessPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_execute_node_worker, stripped, task, node, "", plan)
            result = future.result(timeout=120)

        assert result["status"] == "ok"
        assert isinstance(result["pid"], int)
        assert result["pid"] != os.getpid()
        assert result["echo"] == ""
