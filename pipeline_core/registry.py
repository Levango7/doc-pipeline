"""
Registry v2 - 增强型 Agent 注册中心
================================
改进点：
  - 支持 Agent 健康检查
  - 自动故障恢复（respawn）
  - 依赖图可视化
  - Agent 性能统计
  - 热插拔支持
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Callable, Optional, Any

_logger = logging.getLogger(__name__)


class AgentStatus(Enum):
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    LOADED = "loaded"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    UNHEALTHY = "unhealthy"


class AgentPriority(Enum):
    FIRST = 1
    HIGH = 10
    NORMAL = 50
    LOW = 90
    LAST = 99


@dataclass
class AgentMeta:
    """Agent 元信息 v2"""
    name: str
    version: str = "1.0"
    description: str = ""
    author: str = ""
    priority: int = 50
    input_topics: list[str] = field(default_factory=list)
    output_topics: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    cache_ttl: int = 0
    respawn: bool = False
    respawn_max: int = 3
    health_check_interval: int = 30
    config_schema: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)  # 实例配置，用于 respawn 恢复
    extracts_queries: bool = False       # agent 从输入文件提取查询词
    supports_regeneration: bool = False  # agent 触发质量重做循环
    regeneration_target: str = ""        # 重做目标 agent 名称
    regeneration_recheck: str = ""       # 重做后重新检查的 agent 名称
    results_merge: str = ""              # 池化结果合并策略: "extend" | "first"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentStats:
    """Agent 运行统计"""
    start_count: int = 0
    error_count: int = 0
    respawn_count: int = 0
    total_runtime_ms: float = 0.0
    last_start: Optional[float] = None
    last_error: Optional[float] = None
    last_error_msg: str = ""
    avg_processing_time_ms: float = 0.0
    _processing_times: list[float] = field(default_factory=list)

    def record_start(self):
        self.start_count += 1
        self.last_start = time.time()

    def record_error(self, msg: str):
        self.error_count += 1
        self.last_error = time.time()
        self.last_error_msg = msg

    def record_finish(self, processing_time_ms: float):
        self.total_runtime_ms += processing_time_ms
        self._processing_times.append(processing_time_ms)
        if len(self._processing_times) > 100:
            self._processing_times = self._processing_times[-100:]
        self.avg_processing_time_ms = sum(self._processing_times) / len(self._processing_times)

    def record_respawn(self):
        self.respawn_count += 1

    def to_dict(self) -> dict:
        return {
            "start_count": self.start_count,
            "error_count": self.error_count,
            "respawn_count": self.respawn_count,
            "total_runtime_ms": round(self.total_runtime_ms, 2),
            "avg_processing_time_ms": round(self.avg_processing_time_ms, 2),
            "last_start": self.last_start,
            "last_error": self.last_error,
            "last_error_msg": self.last_error_msg,
        }


class Registry:
    """增强型 Agent 注册中心"""

    def __init__(self, registry_file: Optional[str] = None, enable_health_check: bool = True):
        self._agents: dict[str, dict] = {}
        self._agent_metas: dict[str, AgentMeta] = {}
        self._instances: dict[str, Any] = {}
        self._status: dict[str, AgentStatus] = {}
        self._stats: dict[str, AgentStats] = {}
        self._registry_file = registry_file
        self._lock = threading.RLock()
        self._health_check_enabled = enable_health_check
        self._health_check_thread: Optional[threading.Thread] = None
        self._running = True
        self._shutdown_event = threading.Event()

        if registry_file and os.path.exists(registry_file):
            self.load()

        if enable_health_check:
            self._start_health_checker()

    # ─── 注册 ───────────────────────────────────

    def register(self, meta: AgentMeta, instance: Any = None) -> bool:
        """注册 Agent"""
        with self._lock:
            self._agents[meta.name] = meta.to_dict()
            self._agent_metas[meta.name] = meta
            if instance:
                self._instances[meta.name] = instance
                self._status[meta.name] = AgentStatus.LOADED
            else:
                self._status[meta.name] = AgentStatus.UNREGISTERED
            return True

    def unregister(self, name: str) -> bool:
        """注销 Agent"""
        with self._lock:
            if name in self._instances:
                instance = self._instances[name]
                if hasattr(instance, 'on_stop'):
                    try:
                        instance.on_stop()
                    except Exception as e:
                        _logger.warning(f"[Registry] 停止 {name} 时出错: {e}")

            self._agents.pop(name, None)
            self._instances.pop(name, None)
            self._status.pop(name, None)
            self._stats.pop(name, None)
            return True

    # ─── 查询 ───────────────────────────────────

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            return self._agents.get(name)

    def get_instance(self, name: str) -> Any:
        with self._lock:
            return self._instances.get(name)

    def get_meta(self, name: str) -> Optional[AgentMeta]:
        """返回完整的 AgentMeta 对象（用于 respawn 等）"""
        with self._lock:
            return self._agent_metas.get(name)

    def list(self, status: Optional[AgentStatus] = None,
             tag: Optional[str] = None) -> list[dict]:
        with self._lock:
            results = []
            for name, meta in self._agents.items():
                if status and self._status.get(name) != status:
                    continue
                if tag and tag not in meta.get("tags", []):
                    continue
                result = {**meta, "status": self._status.get(name, AgentStatus.UNREGISTERED).value}
                if name in self._stats:
                    result["stats"] = self._stats[name].to_dict()
                results.append(result)
            return results

    def list_agent_names(self) -> list[str]:
        """返回所有已注册 agent 的名称列表"""
        with self._lock:
            return list(self._agents.keys())

    def find(self, role: str) -> list[dict]:
        """按角色/主题查找 Agent"""
        with self._lock:
            results = []
            for a in self._agents.values():
                if role in a.get("input_topics", []) or role in a.get("output_topics", []):
                    results.append(a)
            return results

    def deps_order(self) -> list[str]:
        """按依赖顺序排序（拓扑排序）"""
        with self._lock:
            sorted_names = []
            remaining = set(self._agents.keys())
            visited = set()
            temp_mark = set()
            cycle_path = []

            def visit(name: str, path: list[str]):
                if name in temp_mark:
                    cycle_start = path.index(name)
                    cycle = path[cycle_start:] + [name]
                    raise ValueError(f"检测到循环依赖: {' -> '.join(cycle)}")
                if name in visited:
                    return
                temp_mark.add(name)
                path.append(name)
                deps = self._agents[name].get("dependencies", [])
                for dep in deps:
                    if dep in remaining:
                        visit(dep, path)
                path.pop()
                temp_mark.remove(name)
                visited.add(name)
                sorted_names.append(name)

            for name in list(remaining):
                if name not in visited:
                    visit(name, cycle_path)

            sorted_names.sort(key=lambda n: self._agents[n].get("priority", 50))
            return sorted_names

    def get_dependency_graph(self) -> dict:
        with self._lock:
            nodes = []
            edges = []
            for name, meta in self._agents.items():
                nodes.append({
                    "id": name,
                    "label": name,
                    "priority": meta.get("priority", 50),
                    "status": self._status.get(name, AgentStatus.UNREGISTERED).value,
                })
                for dep in meta.get("dependencies", []):
                    if dep in self._agents:
                        edges.append({"from": dep, "to": name})
            return {"nodes": nodes, "edges": edges}

    # ─── 状态管理 ─────────────────────────────────

    def set_status(self, name: str, status: AgentStatus):
        need_respawn = False
        with self._lock:
            old_status = self._status.get(name)
            self._status[name] = status

            if status == AgentStatus.RUNNING:
                self._stats.setdefault(name, AgentStats()).record_start()
            elif status == AgentStatus.ERROR:
                stats = self._stats.setdefault(name, AgentStats())
                stats.record_error(f"Status changed to ERROR from {old_status}")
                # P1 修复: 标记需要在锁外执行 respawn，避免持锁调用外部代码
                need_respawn = True
        # 锁外执行 respawn（on_stop/构造新实例可能阻塞或获取其他锁）
        if need_respawn:
            self._check_respawn(name)

    def get_status(self, name: str) -> AgentStatus:
        with self._lock:
            return self._status.get(name, AgentStatus.UNREGISTERED)

    def record_processing_time(self, name: str, ms: float):
        with self._lock:
            self._stats.setdefault(name, AgentStats()).record_finish(ms)

    # ─── Respawn ─────────────────────────────────

    def _check_respawn(self, name: str):
        """检查并执行 respawn。

        P1 修复: 将外部调用（on_stop、构造新实例）移出锁外执行，避免持锁调用
        外部代码导致死锁或长时间持锁。原实现在外层锁内直接调用 on_stop/构造，
        若回调中获取其他锁或阻塞 I/O 会严重影响 registry 并发度。
        """
        # 阶段 1: 锁内决策，收集 respawn 所需信息
        with self._lock:
            meta = self._agent_metas.get(name)
            if not meta or not meta.respawn:
                return
            stats = self._stats.setdefault(name, AgentStats())
            if stats.respawn_count >= meta.respawn_max:
                _logger.warning(f"[Registry] {name} 已达最大 respawn 次数 ({meta.respawn_max})，不再重试")
                return

            _logger.info(f"[Registry] 尝试重启 Agent: {name}")
            stats.record_respawn()
            old_instance = self._instances.get(name)

        # 阶段 2: 锁外执行外部调用（on_stop、构造），避免持锁阻塞
        if old_instance and hasattr(old_instance, 'on_stop'):
            try:
                old_instance.on_stop()
            except Exception as e:
                _logger.warning(f"[Registry] 停止旧实例失败: {e}")

        new_instance = None
        construct_error = None
        if old_instance and hasattr(old_instance, '__class__'):
            try:
                new_instance = old_instance.__class__(
                    name=name,
                    meta=meta,
                    config=meta.config,
                    message_bus=getattr(old_instance, 'bus', None),
                    registry=self,
                )
            except Exception as e:
                construct_error = e

        # 阶段 3: 锁内更新实例和状态
        with self._lock:
            if new_instance is not None:
                self._instances[name] = new_instance
                self._status[name] = AgentStatus.LOADED
                _logger.info(f"[Registry] {name} 重启成功")
            elif construct_error is not None:
                _logger.error(f"[Registry] {name} 重启失败: {construct_error}")
                self._status[name] = AgentStatus.ERROR

    # ─── 健康检查 ─────────────────────────────────

    def _start_health_checker(self):
        """启动健康检查线程"""
        def health_loop():
            while self._running and not self._shutdown_event.is_set():
                if self._shutdown_event.wait(timeout=10):
                    break
                self._check_health()

        self._health_check_thread = threading.Thread(target=health_loop, daemon=True)
        self._health_check_thread.start()

    def _check_health(self):
        # P1 修复: 将 is_healthy() 外部调用移出锁外，避免持锁调用外部代码
        # 阶段 1: 锁内收集需要检查的 (name, instance) 快照
        with self._lock:
            items = [(name, instance) for name, instance in self._instances.items()
                     if self._agent_metas.get(name) is not None]

        # 阶段 2: 锁外调用 is_healthy()（外部代码，可能阻塞）
        respawn_candidates = []
        for name, instance in items:
            if hasattr(instance, 'is_healthy'):
                try:
                    if not instance.is_healthy():
                        with self._lock:
                            self._status[name] = AgentStatus.UNHEALTHY
                        respawn_candidates.append(name)
                except Exception as e:
                    with self._lock:
                        self._status[name] = AgentStatus.ERROR
                    _logger.error(f"[Registry] {name} 健康检查失败: {e}")

        # 阶段 3: 锁外执行 respawn（_check_respawn 内部自行加锁）
        for name in respawn_candidates:
            self._check_respawn(name)

    # ─── 持久化 ─────────────────────────────────

    def save(self, path: Optional[str] = None):
        with self._lock:
            path = path or self._registry_file
            if not path:
                return
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "version": "2.0",
                    "saved_at": time.time(),
                    "agents": list(self._agents.values()),
                    "metas": {k: v.to_dict() for k, v in self._agent_metas.items()},
                    "stats": {k: v.to_dict() for k, v in self._stats.items()},
                }, f, ensure_ascii=False, indent=2)

    def load(self, path: Optional[str] = None):
        with self._lock:
            path = path or self._registry_file
            if not path or not os.path.exists(path):
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for a in data.get("agents", []):
                    name = a["name"]
                    self._agents[name] = a
                    self._status[name] = AgentStatus.REGISTERED

                metas_data = data.get("metas")
                if metas_data:
                    for name, meta_dict in metas_data.items():
                        self._agent_metas[name] = AgentMeta(**meta_dict)
                else:
                    for name, a in self._agents.items():
                        self._agent_metas[name] = AgentMeta(
                            name=name,
                            version=a.get("version", "1.0"),
                            description=a.get("description", ""),
                            author=a.get("author", ""),
                            priority=a.get("priority", 50),
                            input_topics=a.get("input_topics", []),
                            output_topics=a.get("output_topics", []),
                            dependencies=a.get("dependencies", []),
                            cache_ttl=a.get("cache_ttl", 0),
                            respawn=a.get("respawn", False),
                            respawn_max=a.get("respawn_max", 3),
                            health_check_interval=a.get("health_check_interval", 30),
                            config_schema=a.get("config_schema", {}),
                            tags=a.get("tags", []),
                            config=a.get("config", {}),
                        )
                stats_data = data.get("stats", {})
                for name, stats_dict in stats_data.items():
                    stats = AgentStats()
                    stats.start_count = stats_dict.get("start_count", 0)
                    stats.error_count = stats_dict.get("error_count", 0)
                    stats.respawn_count = stats_dict.get("respawn_count", 0)
                    stats.total_runtime_ms = stats_dict.get("total_runtime_ms", 0.0)
                    stats.avg_processing_time_ms = stats_dict.get("avg_processing_time_ms", 0.0)
                    stats.last_start = stats_dict.get("last_start")
                    stats.last_error = stats_dict.get("last_error")
                    stats.last_error_msg = stats_dict.get("last_error_msg", "")
                    self._stats[name] = stats
            except Exception as e:
                _logger.error(f"[Registry] 加载失败: {e}")

    def shutdown(self):
        """关闭注册中心：先调用各 agent 的 on_stop() 生命周期钩子，再停止健康检查线程。

        on_stop() 钩子用于释放 agent 持有的资源（如 aiohttp 持久化连接池、
        文件句柄、定时器等），避免资源泄漏。
        """
        # 调用所有已注册 agent 实例的 on_stop() 生命周期钩子
        for name in self.list_agent_names():
            inst = self.get_instance(name)
            if inst:
                try:
                    inst.on_stop()
                except Exception as e:
                    _logger.warning(f"[Registry] agent {name} on_stop() 失败: {e}")

        self._running = False
        self._shutdown_event.set()
        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=2.0)

    # ─── 展示 ─────────────────────────────────

    def __repr__(self):
        with self._lock:
            lines = [f"Registry ({len(self._agents)} agents):"]
            for name, meta in self._agents.items():
                status = self._status.get(name, AgentStatus.UNREGISTERED).value
                deps = meta.get("dependencies", [])
                stats = self._stats.get(name)
                stats_str = f" runs={stats.start_count}" if stats else ""
                lines.append(f"  [{status}] {name} v{meta['version']} deps={deps or '-'}{stats_str}")
            return "\n".join(lines)
