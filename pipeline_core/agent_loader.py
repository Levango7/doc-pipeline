"""Agent 加载器 —— 负责 Agent 发现、注册和生命周期管理

两条发现路径并存（product-spec §2.1 验收线第 2 条：外部 pip 包提供 Agent、
本仓零改动即被发现/注册/执行）：
1. 本仓 `agents/*.py`（glob，传统路径）；
2. **entry_points 插件**：group `doc_pipeline.agents`，entry point 的**值须是
   模块路径**（与内置 agents/*.py 同构：模块级 `AGENT_NAME` + 一个 BaseAgent
   子类）。同名时本仓文件优先——插件不能悄悄顶掉内置 Agent。

安全策略两条路径一致：文件先过 `declares_sandbox_trust`（模块顶层显式声明
`SANDBOX_TRUSTED = True` 才跳过），否则走 `_check_safety` 的 AST 扫描。
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
import types
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

from .artifacts import normalize_declaration

if TYPE_CHECKING:
    from .registry import AgentMeta

logger = logging.getLogger(__name__)

#: 第三方 Agent 插件的 entry_points group（值 = 模块路径，如 "my_pkg.my_agent"）
ENTRY_POINT_GROUP = "doc_pipeline.agents"

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


def declares_sandbox_trust(file_path: Path) -> bool:
    """源码是否在模块顶层显式声明 `SANDBOX_TRUSTED = True`。

    信任由 Agent 自己声明、由加载器核实，而不是 core 维护一份名单——
    旧的 `_TRUSTED_AGENTS` 把 13 个内置 Agent 的名字写死在引擎里（还留着
    `fast_pool_0` 这种测试遗留项），任何新增内置 Agent 都得回来改 core。

    必须在 exec_module **之前**用 AST 判断：安全检查的意义就在于先于执行。
    """
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"),
                         filename=str(file_path))
    except (OSError, SyntaxError, ValueError):
        return False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "SANDBOX_TRUSTED" in names and isinstance(node.value, ast.Constant) \
                and node.value.value is True:
            return True
    return False


class AgentLoader:
    """Agent 发现和注册"""

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

    def _plugin_entry_points(self) -> dict:
        """entry_points(group=ENTRY_POINT_GROUP) 的插件（名字 → EntryPoint）。

        独立成方法便于测试打桩；元数据读取失败不拖垮内置发现（记 error 后返回 {}，
        内置 agents/ 照常可用）。
        """
        try:
            eps = entry_points(group=ENTRY_POINT_GROUP)
        except Exception as e:
            if self._logger:
                self._logger.log("error", "entry_points 读取失败", error=str(e))
            return {}
        return {ep.name: ep for ep in eps}

    def discover(self) -> list[str]:
        """本仓 agents/*.py + entry_points 插件；同名时本仓文件优先。"""
        names: list[str] = []
        if self.agents_dir.exists():
            for f in self.agents_dir.glob("*.py"):
                if not f.stem.startswith("_"):
                    names.append(f.stem)
        local = set(names)
        plugins = self._plugin_entry_points()
        names.extend(n for n in sorted(plugins) if n not in local)
        return names

    def register(self, agent_names: list[str] | None = None, config: dict | None = None,
                 *, reload: bool = False) -> list[str]:
        """注册 Agent 插件。

        `reload=False`（默认）时，如果 `sys.modules` 里已经有**同一个文件**加载出来的
        模块对象，就直接复用：

        - 每次新建模块对象会让同一个 Agent 存在两份类。测试里
          `patch("agents.writer.WriterAgent.handle")` 打的是先前导入的那一份，
          注册器造的却是另一份——补丁一声不响地空转，E2E 照样绿（本项目抓到过
          两次这类"假绿"，根因都在这里）。
        - 反复注册还会不断丢弃旧模块，模块级状态（缓存、正则、计数器）跟着翻倍泄漏。

        想热插拔新代码就显式传 `reload=True`；但注意复用条件是"文件路径一致"，
        不同目录下的同名 Agent（测试夹具里很常见）不会被误当成同一份。
        """
        names = agent_names or self.discover()
        loaded: list[str] = []
        plugins = self._plugin_entry_points()

        for name in names:
            try:
                agent_file = self.agents_dir / f"{name}.py"
                if not agent_file.exists() and name in plugins:
                    self._register_entry_point(name, plugins[name], config, loaded)
                    continue
                module_key = f"agents.{name}"
                cached = sys.modules.get(module_key)
                cached_file = getattr(cached, "__file__", None)
                reuse = bool(cached is not None and not reload and cached_file
                             and Path(cached_file).resolve() == agent_file.resolve())

                # 安全检查与是否复用无关：缓存在 sys.modules 里的那一份可能是
                # 别的入口（普通 import）加载的，从没走过这道 AST 扫描。
                if not declares_sandbox_trust(agent_file):
                    _check_safety(agent_file, strict=self._strict_safety)

                if reuse:
                    mod = cached
                    if self._logger:
                        self._logger.log("debug", f"复用已加载模块: {module_key}")
                else:
                    # 动态导入
                    spec = importlib.util.spec_from_file_location(
                        module_key,
                        agent_file
                    )
                    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                    # 必须先注册到 sys.modules，这样 _extract_meta 才能找到模块属性
                    sys.modules[module_key] = mod
                    spec.loader.exec_module(mod)  # type: ignore[union-attr]

                self._register_module(mod, config, loaded, source="builtin")

            except Exception as e:
                if self._logger:
                    self._logger.log("error", f"加载失败 {name}", error=str(e))

        return loaded

    def _register_module(self, mod, config: dict | None, loaded: list[str],
                         source: str) -> None:
        """模块 → 注册其中第一个 BaseAgent 子类（内置与 entry_points 两条路径共用）。"""
        from .base_agent import BaseAgent

        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (isinstance(attr, type)
                and issubclass(attr, BaseAgent)
                and attr_name != "BaseAgent"):

                # 提取元信息
                meta = self._extract_meta(attr)
                meta.source = source

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
                    self._logger.log("info",
                                     f"注册: {meta.name} v{meta.version}（{source}）")
                loaded.append(meta.name)
                break

    def _register_entry_point(self, name: str, ep, config: dict | None,
                              loaded: list[str]) -> None:
        """按 entry point 加载外部插件模块（值须是模块路径）。"""
        mod = ep.load()
        if not isinstance(mod, types.ModuleType):
            raise TypeError(
                f"entry point {name!r} 的值须是模块路径（如 'my_pkg.my_agent'），"
                f"实际加载出 {type(mod).__name__}——请对外暴露模块、把类放在模块里，"
                f"与内置 agents/*.py 同构")
        file_path = Path(str(getattr(mod, "__file__", "") or ""))
        if not (str(file_path) and file_path.exists()):
            raise ValueError(
                f"entry point {name!r} 的模块没有可读文件，无法做 AST 安全检查")
        if not declares_sandbox_trust(file_path):
            _check_safety(file_path, strict=self._strict_safety)
        self._register_module(mod, config, loaded, source=f"entry_point:{name}")

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
            produces = getattr(module, "PRODUCES", getattr(cls, "PRODUCES", {}))
            consumes = getattr(module, "CONSUMES", getattr(cls, "CONSUMES", []))
            writes_output = bool(getattr(module, "WRITES_OUTPUT",
                                     getattr(cls, "WRITES_OUTPUT", False)))
            legacy_auto = bool(getattr(module, "LEGACY_AUTO",
                                   getattr(cls, "LEGACY_AUTO", True)))
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
            produces = getattr(cls, "PRODUCES", {})
            consumes = getattr(cls, "CONSUMES", [])
            writes_output = bool(getattr(cls, "WRITES_OUTPUT", False))
            legacy_auto = bool(getattr(cls, "LEGACY_AUTO", True))

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
            produces=normalize_declaration(produces),
            consumes=list(consumes or []),
            writes_output=writes_output,
            legacy_auto=legacy_auto,
        )
