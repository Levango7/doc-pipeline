"""
pipeline_core v3 - 文档生成流水线核心框架
=========================================
核心特性：
  - 消息总线支持异步广播和死信队列 (v3)
  - Registry 支持健康检查和自动恢复
  - BaseAgent 支持结构化日志和性能统计
  - PipelineOrchestrator 支持 DAG 并行、断点续传、自动重做
  - Per-agent 熔断器 + 令牌桶限流
  - QualityGate 多维度评分 + 自动反馈循环
  - 临时文件自动清理
"""

from .config import ConfigCenter
from .registry import Registry, AgentMeta, AgentStatus, AgentStats, AgentPriority
from .base_agent import BaseAgent, AgentLogger
from .pipeline import PipelineOrchestrator, PipelineTask, TaskStatus, StepResult
from .message_bus_v3 import MessageBus, Message, MessageType, MessagePriority, MessageMetrics
from .message_store import PersistentStore
from .circuit_breaker import CircuitBreakerRegistry, backoff_with_jitter
from .scheduler import Scheduler, ExecutionPlan, ExecutionNode, AgentConfig
from .agent_loader import AgentLoader
from .dag_executor import DAGExecutor
from .checkpoint_manager import CheckpointManager
from .llm_router import LLMRouter, get_router, reset_router
from .search_engines import SearchEngineManager, SearchItem
from .cache_manager import CacheManager, get_cache, clear_all_caches, all_stats
from .streaming import (
    StreamEvent, StreamCallback, StreamMetrics,
    register_callback, get_callback, unregister_callback,
)
from .executor_factory import create_executor, is_process_executor, SmartExecutor
from .three_pass_pipeline import ThreePassPipeline, DocumentPlan, PassResult
from .document_enhancer import DocumentEnhancer
from .bootstrap import run_startup_check, quick_check, StartupReport

__all__ = [
    "ConfigCenter",
    "MessageBus", "Message", "MessageType", "MessagePriority", "MessageMetrics",
    "PersistentStore",
    "Registry", "AgentMeta", "AgentStatus", "AgentStats", "AgentPriority",
    "BaseAgent", "AgentLogger",
    "PipelineOrchestrator", "PipelineTask", "TaskStatus", "StepResult",
    "Scheduler", "ExecutionPlan", "ExecutionNode", "AgentConfig",
    "AgentLoader", "DAGExecutor", "CheckpointManager",
    "LLMRouter", "get_router", "reset_router",
    "SearchEngineManager", "SearchItem",
    "CacheManager", "get_cache", "clear_all_caches", "all_stats",
    "StreamEvent", "StreamCallback", "StreamMetrics",
    "register_callback", "get_callback", "unregister_callback",
    "create_executor", "is_process_executor", "SmartExecutor",
    "ThreePassPipeline", "DocumentPlan", "PassResult",
    "DocumentEnhancer",
    "run_startup_check", "quick_check", "StartupReport",
]

__version__ = "3.2.0"