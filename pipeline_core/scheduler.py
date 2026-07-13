"""
Scheduler - 读取 pipeline.yaml 并生成可执行计划
===============================================
新增：
  - Schema 校验：运行时验证 agent config 类型合法性
  - Lockfile：pipeline 版本锁定 + 一致性验证
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any
import yaml


@dataclass
class AgentConfig:
    """Agent 配置"""
    name: str = ""
    version: str = "1.0"
    parallelism: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    timeout: float = 300.0
    retry: dict = field(default_factory=dict)
    circuit_breaker: dict = field(default_factory=dict)
    dependencies: list = field(default_factory=list)
    pool_size: int = 1
    rate_limit: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.pool_size < 1:
            self.pool_size = 1


@dataclass
class ExecutionNode:
    """执行节点"""
    agent_name: str
    agent_config: AgentConfig
    dependencies: list = field(default_factory=list)
    timeout: float = 300.0
    max_retries: int = 3
    backoff: str = "exponential"
    initial_delay: float = 1.0


@dataclass
class ExecutionPlan:
    """可执行计划"""
    plan_id: str = ""
    pipeline_name: str = ""
    node_count: int = 0
    levels: list[list[ExecutionNode]] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    fail_fast: bool = False
    checkpoint: dict = field(default_factory=dict)

    @property
    def max_level(self) -> int:
        return len(self.levels) - 1


# ─── Agent Schema 定义 ─────────────────────

AGENT_SCHEMAS = {
    "researcher": {
        "search_engines": (list, ["bing"]),
        "max_results": (int, 10),
        "cache_size": (int, 1000),
        "max_workers": (int, 3),
        "min_score": (float, 0.3),
        "max_history": (int, 100),
    },
    "fetcher": {
        "max_downloads": (int, 15),
        "temp_dir": (str, "tmp_fetcher"),
        "download_workers": (int, 5),
    },
    "writer": {
        "template": (str, "default"),
        "pending_expire_secs": (int, 300),
        "polish_cache_ttl": (int, 3600),
    },
    "quality_gate": {
        "quality_profile": (str, "technical-doc"),
        "threshold": ((int, float), 70),
        "max_regenerations": (int, 3),
    },
    "checker": {
        "fail_fast": (bool, False),
    },
    "layout": {
        "style": (str, "markdown"),
    },
    "safewriter": {
        "backup_dir": (str, "backups"),
        "atomic": (bool, True),
    },
}


class Scheduler:
    """读取 pipeline.yaml 并生成可执行计划"""

    def __init__(self, pipeline_dir: str = "pipelines"):
        self.pipeline_dir = Path(pipeline_dir)
        self.pipeline_dir.mkdir(parents=True, exist_ok=True)

    def list_pipelines(self) -> list[str]:
        return [p.stem for p in self.pipeline_dir.glob("*.yaml")
                if not p.name.startswith("_")]

    def load(self, pipeline_name: str) -> dict:
        path = self.pipeline_dir / f"{pipeline_name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"pipeline 未找到: {path}")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not raw:
            raise ValueError(f"pipeline 为空: {path}")
        return raw

    def parse(self, pipeline_name: str) -> ExecutionPlan:
        raw = self.load(pipeline_name)
        return self._build_plan(raw, pipeline_name)

    def parse_file(self, filepath: str) -> ExecutionPlan:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"pipeline 文件未找到: {filepath}")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        pipeline_name = path.stem
        return self._build_plan(raw, pipeline_name)

    def _deep_merge(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _build_plan(self, raw: dict, pipeline_name: str) -> ExecutionPlan:
        import uuid
        import logging
        logger = logging.getLogger(__name__)
        plan_id = str(uuid.uuid4())[:8]

        # ── 1. 构建 agent lookup ──
        agent_map: dict[str, AgentConfig] = {}
        defaults = raw.get("defaults", {})
        for a in raw.get("agents", []):
            merged = self._deep_merge(defaults, a)

            pool_size_val = merged.get("pool_size", 1)
            try:
                pool_size = int(pool_size_val)
            except (ValueError, TypeError):
                logger.warning(f"[{merged.get('name', 'unknown')}] pool_size 值无效: {pool_size_val!r}, 使用默认值 1")
                pool_size = 1

            timeout_val = merged.get("timeout", 300)
            try:
                timeout = float(timeout_val)
            except (ValueError, TypeError):
                logger.warning(f"[{merged.get('name', 'unknown')}] timeout 值无效: {timeout_val!r}, 使用默认值 300")
                timeout = 300.0

            cfg = AgentConfig(
                name=merged.get("name", "unknown"),
                version=str(merged.get("version", "1.0")),
                parallelism=merged.get("parallelism", {}),
                config=merged.get("config", {}),
                timeout=timeout,
                retry=merged.get("retry", {}),
                circuit_breaker=merged.get("circuit_breaker", {}),
                dependencies=list(merged.get("dependencies", [])),
                pool_size=pool_size,
                rate_limit=merged.get("rate_limit", {}),
            )
            agent_map[cfg.name] = cfg

        # ── Schema 校验 ──
        self._validate_agent_schemas(agent_map)

        # ── 2. 校验 & 展开拓扑 ──
        topology = raw.get("topology", {})
        levels_raw = topology.get("levels", [])
        if not levels_raw:
            raise ValueError("topology.levels 为空，无法构建 DAG")

        flattened = [name for level in levels_raw for name in level]
        for name in flattened:
            if name not in agent_map:
                raise ValueError(f"Agent 未定义: {name}（在 topology 中引用）")

        appeared: set[str] = set()
        for lvl_idx, level in enumerate(levels_raw):
            for name in level:
                cfg = agent_map[name]
                deps = [d for d in cfg.dependencies if d in agent_map]
                valid_deps = [d for d in deps if d in appeared]
                if len(valid_deps) != len(deps):
                    missing = set(deps) - set(valid_deps)
                    raise ValueError(
                        f"Agent [{name}] 的依赖 {missing} 不在其前置层级中，"
                        f"topology levels={levels_raw}"
                    )
                appeared.add(name)

        # ── 3. 构建 ExecutionNode ──
        appeared.clear()
        levels: list[list[ExecutionNode]] = []

        for lvl_idx, level in enumerate(levels_raw):
            nodes: list[ExecutionNode] = []
            for name in level:
                cfg = agent_map[name]
                # 展开依赖中的 pool 名：某 agent 有 pool > 1 时，依赖指向所有实例
                deps = []
                for d in cfg.dependencies:
                    if d in agent_map and d in appeared:
                        dep_cfg = agent_map[d]
                        if dep_cfg.pool_size > 1:
                            for pool_idx in range(dep_cfg.pool_size):
                                deps.append(f"{d}_pool_{pool_idx}")
                        else:
                            deps.append(d)

                # 展开 pooling
                for pool_idx in range(cfg.pool_size):
                    pool_name = f"{name}_pool_{pool_idx}" if cfg.pool_size > 1 else name
                    nodes.append(ExecutionNode(
                        agent_name=pool_name,
                        agent_config=cfg,
                        dependencies=deps,
                        timeout=cfg.timeout,
                        max_retries=cfg.retry.get("max_attempts", 3),
                        backoff=cfg.retry.get("backoff", "exponential"),
                        initial_delay=cfg.retry.get("initial_delay", 1.0),
                    ))
                appeared.add(name)

            levels.append(nodes)

        # ── 4. 构建 ExecutionPlan ──
        node_count = sum(len(level) for level in levels)
        return ExecutionPlan(
            plan_id=plan_id,
            pipeline_name=pipeline_name,
            node_count=node_count,
            levels=levels,
            raw=raw,
            fail_fast=raw.get("pipeline", {}).get("fail_fast", False),
            checkpoint=raw.get("pipeline", {}).get("checkpoint", {}),
        )

    # ── Schema 校验 ─────────────────────

    def _validate_agent_schemas(self, agent_map: dict[str, AgentConfig]):
        """校验每个 agent 的 config 是否符合预设 schema"""
        for name, cfg in agent_map.items():
            base_name = name.split("_pool_")[0]
            schema = AGENT_SCHEMAS.get(base_name, {})
            if not schema:
                continue
            for key, (expected_type, default) in schema.items():
                if key not in cfg.config:
                    cfg.config[key] = default
                    continue
                val = cfg.config[key]
                if not isinstance(val, expected_type):
                    type_name = expected_type.__name__ if isinstance(expected_type, type) else \
                        "|".join(t.__name__ for t in expected_type)
                    raise TypeError(
                        f"[{name}] config.{key}: 期望 {type_name}, "
                        f"实际 {type(val).__name__}={val!r}"
                    )

    # ── Lockfile ─────────────────────

    def generate_lockfile(self, plan: ExecutionPlan, output_dir: str = "pipelines") -> str:
        """生成 pipeline lockfile（版本锁定）"""
        import json, time
        lock = {
            "pipeline": plan.pipeline_name,
            "plan_id": plan.plan_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "node_count": plan.node_count,
            "agents": {},
        }
        for level in plan.levels:
            for node in level:
                cfg = node.agent_config
                config_hash = hashlib.md5(
                    json.dumps(cfg.config, sort_keys=True).encode()
                ).hexdigest()[:12]
                lock["agents"][node.agent_name] = {
                    "version": cfg.version,
                    "dependencies": cfg.dependencies,
                    "pool_size": cfg.pool_size,
                    "config_hash": config_hash,
                }
        lockfile = Path(output_dir) / f"{plan.pipeline_name}.lock"
        with open(lockfile, "w", encoding="utf-8") as f:
            yaml.dump(lock, f, default_flow_style=False, allow_unicode=True)
        return str(lockfile)

    def verify_lockfile(self, plan: ExecutionPlan, lockfile: str = "") -> list[str]:
        """验证当前 plan 是否与 lockfile 一致"""
        if not lockfile:
            lockfile = f"pipelines/{plan.pipeline_name}.lock"
        lock_path = Path(lockfile)
        if not lock_path.exists():
            return [f"Lockfile 不存在: {lockfile}"]

        with open(lock_path, "r", encoding="utf-8") as f:
            lock = yaml.safe_load(f) or {}

        issues = []
        if lock.get("pipeline") != plan.pipeline_name:
            issues.append(f"pipeline 名称不匹配: lock={lock.get('pipeline')}, plan={plan.pipeline_name}")

        locked_agents = lock.get("agents", {})
        for level in plan.levels:
            for node in level:
                aname = node.agent_name
                locked = locked_agents.get(aname, {})
                if not locked:
                    issues.append(f"[{aname}] 不在 lockfile 中")
                    continue
                if locked.get("version") != node.agent_config.version:
                    issues.append(f"[{aname}] 版本不匹配: lock={locked.get('version')}, yaml={node.agent_config.version}")

        return issues

    # ── 校验 ─────────────────────

    def validate(self, plan: ExecutionPlan, agents_dir: str = "agents") -> list[str]:
        """校验 pipeline 的完整性"""
        issues = []
        agents_path = Path(agents_dir)
        if not agents_path.exists():
            issues.append(f"agents 目录不存在: {agents_dir}")
            return issues

        for node in [n for level in plan.levels for n in level]:
            agent_name = node.agent_name.replace("-", "_")
            candidates = [
                agents_path / f"{agent_name}.py",
                agents_path / f"{agent_name}_agent.py",
                agents_path / f"safe_writer_agent.py" if agent_name == "safewriter" else None,
            ]
            agent_file = None
            for c in candidates:
                if c and c.exists():
                    agent_file = c
                    break
            if not agent_file:
                issues.append(f"[{node.agent_name}] Agent 文件不存在: {agent_name}.py")
                continue

            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    f"_validate_{node.agent_name}", agent_file
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                mod_deps = list(getattr(mod, "DEPENDENCIES", []))
                yaml_deps = list(node.agent_config.dependencies)
                if sorted(mod_deps) != sorted(yaml_deps):
                    issues.append(
                        f"[{node.agent_name}] 依赖不一致: "
                        f"YAML={yaml_deps}, 模块={mod_deps}"
                    )
            except Exception as e:
                issues.append(f"[{node.agent_name}] 模块加载失败: {e}")

        return issues