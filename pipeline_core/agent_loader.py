"""Agent 加载器 —— 负责 Agent 发现、注册和生命周期管理"""
from __future__ import annotations
import sys
import ast
import importlib.util
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DANGEROUS_CALLS = {
    "os.system", "os.popen", "os.execv", "os.execvp", "os.execve", "os.execvpe",
    "subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output",
    "eval", "exec", "__import__",
    "shutil.rmtree",
}


def _check_safety(file_path: Path, strict: bool = False) -> list[str]:
    """AST 安全检查：扫描危险调用

    Args:
        file_path: Agent .py 文件路径
        strict: True 时抛异常，False 时仅 log warning
    Returns:
        发现的危险调用列表
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return []

    dangers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        full_name = ""
        if isinstance(func, ast.Attribute):
            parts = []
            n = func
            while isinstance(n, ast.Attribute):
                parts.append(n.attr)
                n = n.value
            if isinstance(n, ast.Name):
                parts.append(n.id)
            full_name = ".".join(reversed(parts))
        elif isinstance(func, ast.Name):
            full_name = func.id

        if full_name in _DANGEROUS_CALLS:
            dangers.append(f"{full_name} (line {node.lineno})")

    if dangers:
        msg = f"Agent {file_path.name} 包含危险调用: {', '.join(dangers)}"
        if strict:
            raise SecurityError(msg)
        logger.warning(msg)
    return dangers


class SecurityError(Exception):
    """Agent 安全检查失败"""
    pass


class AgentLoader:
    """Agent 发现和注册"""

    _TRUSTED_AGENTS = {
        "researcher", "fetcher", "writer", "quality_gate",
        "checker", "layout", "safe_writer_agent", "fast_pool_0",
    }

    def __init__(self, registry, bus, agents_dir: str = "agents", logger=None,
                 strict_safety: bool = True):
        self.registry = registry
        self.bus = bus
        self.agents_dir = Path(agents_dir)
        self._logger = logger
        self._strict_safety = strict_safety
        # 统一将项目根目录加入 sys.path，替代各 agent 文件中的 sys.path.insert hack
        project_root = str(self.agents_dir.parent.resolve())
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

    def discover(self) -> list[str]:
        """自动发现 agents 目录下的插件"""
        discovered = []
        if not self.agents_dir.exists():
            return discovered

        for f in self.agents_dir.glob("*.py"):
            if f.stem.startswith("_"):
                continue
            discovered.append(f.stem)
        return discovered

    def register(self, agent_names: Optional[list[str]] = None, config: Optional[dict] = None) -> list[str]:
        """注册 Agent 插件"""
        from .base_agent import BaseAgent
        from .registry import AgentMeta

        names = agent_names or self.discover()
        loaded = []

        for name in names:
            try:
                # 动态导入
                spec = importlib.util.spec_from_file_location(
                    f"agents.{name}",
                    self.agents_dir / f"{name}.py"
                )
                mod = importlib.util.module_from_spec(spec)
                # 必须先注册到 sys.modules，这样 _extract_meta 才能找到模块属性
                sys.modules[f"agents.{name}"] = mod

                if name not in self._TRUSTED_AGENTS:
                    _check_safety(self.agents_dir / f"{name}.py", strict=self._strict_safety)

                spec.loader.exec_module(mod)

                # 找 Agent 类
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (isinstance(attr, type)
                        and issubclass(attr, BaseAgent)
                        and attr_name != "BaseAgent"):

                        # 提取元信息
                        meta = self._extract_meta(attr)

                        # 实例化
                        agent = attr(
                            name=meta.name,
                            meta=meta,
                            config=config or {},
                            message_bus=self.bus,
                            registry=self.registry
                        )
                        # 保存实例配置到 meta，用于 respawn 恢复
                        meta.config = config or {}

                        self.registry.register(meta, agent)
                        if self._logger:
                            self._logger.log("info", f"注册: {meta.name} v{meta.version}")
                        loaded.append(meta.name)
                        break

            except Exception as e:
                if self._logger:
                    self._logger.log("error", f"加载失败 {name}", error=str(e))

        return loaded

    def _extract_meta(self, cls) -> "AgentMeta":
        """从类属性提取 AgentMeta"""
        from .registry import AgentMeta

        # 模块级 AGENT_NAME 不会被 getattr(cls) 找到（它定义在模块而非类体）
        # 用 cls.__name__ 作为 fallback，并清理常见后缀
        cls_name = cls.__name__.replace("Agent", "").lower()

        # 优先从模块获取 AGENT_NAME（避免 BaseAgent 的 "base" 被继承）
        module = sys.modules.get(cls.__module__)
        if module and hasattr(module, "AGENT_NAME"):
            agent_name = module.AGENT_NAME
        else:
            raw_name = getattr(cls, "AGENT_NAME", cls_name)
            agent_name = raw_name if raw_name != "base" else cls_name

        # 尝试从模块获取属性（模块级定义的 INPUT_TOPICS 等）
        if module:
            input_topics = getattr(module, "INPUT_TOPICS", getattr(cls, "INPUT_TOPICS", []))
            output_topics = getattr(module, "OUTPUT_TOPICS", getattr(cls, "OUTPUT_TOPICS", []))
            dependencies = getattr(module, "DEPENDENCIES", getattr(cls, "DEPENDENCIES", []))
            cache_ttl = getattr(module, "CACHE_TTL", getattr(cls, "CACHE_TTL", 0))
            respawn = getattr(module, "RESPAWN", getattr(cls, "RESPAWN", False))
            respawn_max = getattr(module, "RESPAWN_MAX", getattr(cls, "RESPAWN_MAX", 3))
            health_check_interval = getattr(module, "HEALTH_CHECK_INTERVAL", getattr(cls, "HEALTH_CHECK_INTERVAL", 30))
            priority = getattr(module, "AGENT_PRIORITY", getattr(cls, "AGENT_PRIORITY", 50))
            version = getattr(module, "AGENT_VERSION", getattr(cls, "AGENT_VERSION", "1.0"))
            description = getattr(module, "AGENT_DESC", getattr(cls, "AGENT_DESC", cls.__doc__ or ""))
            author = getattr(module, "AGENT_AUTHOR", getattr(cls, "AGENT_AUTHOR", ""))
            extracts_queries = getattr(module, "EXTRACTS_QUERIES", getattr(cls, "EXTRACTS_QUERIES", False))
            supports_regeneration = getattr(module, "SUPPORTS_REGENERATION", getattr(cls, "SUPPORTS_REGENERATION", False))
            regeneration_target = getattr(module, "REGENERATION_TARGET", getattr(cls, "REGENERATION_TARGET", ""))
            regeneration_recheck = getattr(module, "REGENERATION_RECHECK", getattr(cls, "REGENERATION_RECHECK", ""))
            results_merge = getattr(module, "RESULTS_MERGE", getattr(cls, "RESULTS_MERGE", ""))
        else:
            input_topics = getattr(cls, "INPUT_TOPICS", [])
            output_topics = getattr(cls, "OUTPUT_TOPICS", [])
            dependencies = getattr(cls, "DEPENDENCIES", [])
            cache_ttl = getattr(cls, "CACHE_TTL", 0)
            respawn = getattr(cls, "RESPAWN", False)
            respawn_max = getattr(cls, "RESPAWN_MAX", 3)
            health_check_interval = getattr(cls, "HEALTH_CHECK_INTERVAL", 30)
            priority = getattr(cls, "AGENT_PRIORITY", 50)
            version = getattr(cls, "AGENT_VERSION", "1.0")
            description = getattr(cls, "AGENT_DESC", cls.__doc__ or "")
            author = getattr(cls, "AGENT_AUTHOR", "")
            extracts_queries = getattr(cls, "EXTRACTS_QUERIES", False)
            supports_regeneration = getattr(cls, "SUPPORTS_REGENERATION", False)
            regeneration_target = getattr(cls, "REGENERATION_TARGET", "")
            regeneration_recheck = getattr(cls, "REGENERATION_RECHECK", "")
            results_merge = getattr(cls, "RESULTS_MERGE", "")

        return AgentMeta(
            name=agent_name,
            version=version,
            description=description,
            author=author,
            priority=priority,
            input_topics=input_topics,
            output_topics=output_topics,
            dependencies=dependencies,
            cache_ttl=cache_ttl,
            respawn=respawn,
            respawn_max=respawn_max,
            health_check_interval=health_check_interval,
            extracts_queries=extracts_queries,
            supports_regeneration=supports_regeneration,
            regeneration_target=regeneration_target,
            regeneration_recheck=regeneration_recheck,
            results_merge=results_merge,
        )