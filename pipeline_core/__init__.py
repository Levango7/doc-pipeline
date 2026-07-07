"""
pipeline_core v2 - 文档生成流水线核心框架（改进版）
================================================
改进点：
  - 消息总线支持异步广播和死信队列
  - Registry 支持健康检查和自动恢复
  - BaseAgent 支持结构化日志和性能统计
  - PipelineOrchestrator 支持断点续传和可视化
"""

from .registry import Registry, AgentMeta, AgentStatus, AgentStats, AgentPriority
from .pipeline import PipelineOrchestrator, PipelineTask, TaskStatus, StepResult
from .message_bus_v3 import MessageBus, Message, MessageType, MessagePriority, MessageMetrics
from .circuit_breaker import CircuitBreakerRegistry, backoff_with_jitter
from .scheduler import Scheduler, ExecutionPlan, ExecutionNode, AgentConfig

__all__ = [
    # Message Bus
    "MessageBus", "Message", "MessageType", "MessagePriority", "MessageMetrics",
    # Registry
    "Registry", "AgentMeta", "AgentStatus", "AgentStats", "AgentPriority",
    # Base Agent
    "BaseAgent", "AgentLogger",
    # Pipeline
    "PipelineOrchestrator", "PipelineTask", "TaskStatus", "StepResult",
    # Scheduler (v3)
    "Scheduler", "ExecutionPlan", "ExecutionNode", "AgentConfig",
]

__version__ = "3.0.0"
