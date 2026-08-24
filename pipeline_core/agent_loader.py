"""Agent 加载器 —— 负责 Agent 发现、注册和生命周期管理"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import AgentMeta

logger = logging.getLogger(__name__)

# 危险调用黑名单（含模块属性调用，如 os.remove / socket.socket / ctypes.CDLL）
_DANGEROUS_CALLS = {
    "os.system", "os.popen", "os.execv", "os.execvp", "os.execve", "os.execvpe",
    "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.replace",
    "os.chmod", "os.chown", "os.kill", "os.fork",
    "subprocess.Popen", "subprocess.run", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output",
    "eval", "exec", "__import__", "compile",
    "shutil.rmtree", "shutil.move", "shutil.copy2",
    "open",
    "socket.socket", "socket.connect", "socket.bind",
    "ctypes.CDLL", "ctypes.cdll", "ctypes.PyDLL", "ctypes.WinDLL",
    "ctypes.CFUNCTYPE", "ctypes.pythonapi",
    "pickle.loads", "pickle.load", "marshal.loads",
}

# 危险 import 模块黑名单（用于 ImportFrom / Import 节点检查）
_DANGEROUS_MODULES = {
    "subprocess", "ctypes", "socket", "pickle", "marshal",
    "multiprocessing", "threading",  # 仍允许 import 但禁止其危险调用
}

# ImportFrom 中绝对禁止的名称（from <module> import <name>）
_DANGEROUS_IMPORT_NAMES = {
    "system", "popen", "exec", "eval", "Popen", "run",
    "rmtree", "remove", "unlink", "open",
    "CDLL", "WinDLL", "PyDLL", "cdll",
    "loads", "load",  # pickle/marshal 反序列化
}


def _check_safety(file_path: Path, strict: bool = False) -> list[str]:
    """AST 安全检查：扫描危险调用 + 危险 import

    修复 P0：原实现仅检查 ast.Call，可被 ``from os import remove`` 或
    ``from subprocess import Popen`` 绕过（后续直接调用 ``remove(...)`` /
    ``Popen(...)`` 不会被识别为 ``os.remove``）。现增加对 ``ast.ImportFrom``
    和 ``ast.Import`` 的检查，并扩展黑名单覆盖 open / os.remove / socket /
    ctypes / pickle 等危险 API。

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
        # ── 检查危险函数调用 ──
        if isinstance(node, ast.Call):
            func = node.func
            full_name = ""
            if isinstance(func, ast.Attribute):
                parts = []
                n = func
                while isinstance(n, ast.Attribute):
                    parts.append(n.attr)
                    n = n.value  # type: ignore[assignment]
                if isinstance(n, ast.Name):
                    parts.append(n.id)
                full_name = ".".join(reversed(parts))
            elif isinstance(func, ast.Name):
                full_name = func.id

            if full_name in _DANGEROUS_CALLS:
                dangers.append(f"{full_name} (line {node.lineno})")

        # ── 检查 from X import Y（可绕过 Call 检查的导入别名）──
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # from subprocess import Popen, run, ...
            if module in _DANGEROUS_MODULES:
                for alias in node.names:
                    imported_name = alias.asname or alias.name
                    if alias.name in _DANGEROUS_IMPORT_NAMES or imported_name in _DANGEROUS_IMPORT_NAMES:
                        dangers.append(
                            f"from {module} import {alias.name} (line {node.lineno})"
                        )
            # from os import remove/system/popen 等（os 不在 _DANGEROUS_MODULES
            # 因为 import os 本身无害，但其危险函数名需拦截）
            if module == "os":
                for alias in node.names:
                    if alias.name in _DANGEROUS_IMPORT_NAMES:
                        dangers.append(
                            f"from os import {alias.name} (line {node.lineno})"
                        )

        # ── 检查 import X（仅对极危险模块：subprocess/ctypes 直接 import）──
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # ctypes / pickle / marshal 直接 import 即视为危险
                # （subprocess/socket 仍允许 import，由 _DANGEROUS_CALLS 拦截调用）
                if alias.name in {"ctypes", "pickle", "marshal"}:
                    dangers.append(f"import {alias.name} (line {node.lineno})")

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
        "fact_checker",
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
        discovered = []  # type: ignore[var-annotated]
        if not self.agents_dir.exists():
            return discovered

        for f in self.agents_dir.glob("*.py"):
            if f.stem.startswith("_"):
                continue
            discovered.append(f.stem)
        return discovered

    def register(self, agent_names: list[str] | None = None, config: dict | None = None) -> list[str]:
        """注册 Agent 插件"""
        from .base_agent import BaseAgent

        names = agent_names or self.discover()
        loaded = []

        for name in names:
            try:
                # 动态导入
                spec = importlib.util.spec_from_file_location(
                    f"agents.{name}",
                    self.agents_dir / f"{name}.py"
                )
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                # 必须先注册到 sys.modules，这样 _extract_meta 才能找到模块属性
                sys.modules[f"agents.{name}"] = mod

                if name not in self._TRUSTED_AGENTS:
                    _check_safety(self.agents_dir / f"{name}.py", strict=self._strict_safety)

                spec.loader.exec_module(mod)  # type: ignore[union-attr]

                # 找 Agent 类
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (isinstance(attr, type)
                        and issubclass(attr, BaseAgent)
                        and attr_name != "BaseAgent"):

                        # 提取元信息
                        meta = self._extract_meta(attr)

                        # 实例化：优先注入按 agent 名提取的子配置，同时保留顶层全局配置
                        agent_config = (config or {}).get(meta.name, config or {})
                        agent = attr(
                            name=meta.name,
                            meta=meta,
                            config=agent_config,
                            message_bus=self.bus,
                            registry=self.registry
                        )
                        # 保存实例配置到 meta，用于 respawn 恢复
                        meta.config = agent_config

                        self.registry.register(meta, agent)
                        if self._logger:
                            self._logger.log("info", f"注册: {meta.name} v{meta.version}")
                        loaded.append(meta.name)
                        break

            except Exception as e:
                if self._logger:
                    self._logger.log("error", f"加载失败 {name}", error=str(e))

        return loaded

    def _extract_meta(self, cls) -> AgentMeta:
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
