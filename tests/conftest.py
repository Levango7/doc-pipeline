"""Doc-Pipeline 集成测试共享 fixtures"""
import sys, os, json, time, threading, uuid, shutil, tempfile
from pathlib import Path
from typing import Generator, Callable
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core.message_bus_v3 import MessageBus
from pipeline_core.circuit_breaker import CircuitBreakerRegistry
from pipeline_core.rate_limiter import RateLimiterRegistry
from pipeline_core.scheduler import Scheduler
from pipeline_core import PipelineOrchestrator
from pipeline_core.base_agent import BaseAgent, AgentMeta, AgentStatus, Message


# ── 共享路径 ──
HERE = Path(__file__).parent
PROJECT = HERE.parent
AGENTS_DIR = str(PROJECT / "agents")
PIPELINES_DIR = str(PROJECT / "pipelines")
CHECKPOINT_DIR = PROJECT / ".test_checkpoints"
OUTPUT_DIR = PROJECT / ".test_outputs"


@pytest.fixture(autouse=True)
def clean_checkpoints():
    """每个测试前清理检查点和输出"""
    for d in [CHECKPOINT_DIR, OUTPUT_DIR]:
        if d.exists():
            try:
                shutil.rmtree(str(d))
            except OSError:
                pass  # Windows 文件锁
        d.mkdir(parents=True, exist_ok=True)
    yield


# ── MessageBus fixtures ──

@pytest.fixture
def bus():
    """SQLite MessageBus v3（每个测试独立 DB 文件）"""
    db = os.path.join(tempfile.mkdtemp(), "test_bus.db")
    b = MessageBus(db_path=str(db))
    # v3 线程在 __init__ 自动启动
    yield b
    b._shutdown_event.set()


@pytest.fixture
def bus_dlq(bus):
    """带 DLQ 的 MessageBus"""
    # bus already has DLQ active via start()
    return bus


# ── 回调辅助 ──

@pytest.fixture
def collector():
    """收集所有收到的消息"""
    msgs = []

    def cb(msg):
        msgs.append(msg)
    return cb, msgs


# ── Registry fixtures ──

@pytest.fixture
def circuit_breakers():
    """熔断器注册表"""
    return CircuitBreakerRegistry()


@pytest.fixture
def rate_limiters():
    """限流器注册表"""
    return RateLimiterRegistry()


# ── Scheduler fixture ──

@pytest.fixture
def scheduler():
    return Scheduler()


@pytest.fixture
def docgen_plan(scheduler):
    """解析测试用 pipeline（mock 引擎，无网络）"""
    plan = scheduler.parse_file(str(PROJECT / "pipelines" / "test_pipeline.yaml"))
    return plan


# ── Orchestrator fixture ──

@pytest.fixture
def orch():
    """完整初始化的 PipelineOrchestrator"""
    o = PipelineOrchestrator(
        agents_dir=AGENTS_DIR,
        checkpoint_dir=str(CHECKPOINT_DIR),
    )
    o.register_agents()
    yield o