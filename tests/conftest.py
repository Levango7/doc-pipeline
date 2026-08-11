"""Doc-Pipeline 集成测试共享 fixtures"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


import contextlib

from pipeline_core import PipelineOrchestrator
from pipeline_core.circuit_breaker import CircuitBreakerRegistry
from pipeline_core.message_bus_v3 import MessageBus
from pipeline_core.rate_limiter import RateLimiterRegistry
from pipeline_core.scheduler import Scheduler

# ── 共享路径 ──
HERE = Path(__file__).parent
PROJECT = HERE.parent
AGENTS_DIR = str(PROJECT / "agents")
PIPELINES_DIR = str(PROJECT / "pipelines")
CHECKPOINT_DIR = PROJECT / ".test_checkpoints"
OUTPUT_DIR = PROJECT / ".test_outputs"


# ── 覆盖 pytest 内置 tmp_path，避免 Windows Temp 权限问题 ──
_LOCAL_TMP = PROJECT / ".pytest_tmp"
_tmp_counter = [0]


@pytest.fixture
def tmp_path():
    """使用项目本地 .pytest_tmp 目录替代系统 Temp，规避 WinError 5 权限拒绝"""
    _tmp_counter[0] += 1
    d = _LOCAL_TMP / f"tmp_{os.getpid()}_{_tmp_counter[0]}"
    d.mkdir(parents=True, exist_ok=True)
    yield d
    # 清理：best-effort
    with contextlib.suppress(Exception):
        shutil.rmtree(str(d), ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_checkpoints():
    """每个测试前清理检查点和输出"""
    for d in [CHECKPOINT_DIR, OUTPUT_DIR]:
        if d.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(str(d))  # Windows 文件锁时忽略
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
