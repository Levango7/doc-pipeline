"""
Scheduler - 读取 pipeline.yaml 并生成可执行计划
===============================================
新增：
  - Schema 校验：运行时验证 agent config 类型合法性
  - Lockfile：pipeline 版本锁定 + 一致性验证
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .conditions import ConditionError
from .conditions import validate as validate_condition
from .naming import agent_of, node_id, pool_index_of


class LockfileMismatchError(Exception):
    """当前 plan 与 lockfile 不一致（版本锁定校验失败）"""

    def __init__(self, pipeline_name: str, issues: list[str]):
        self.pipeline_name = pipeline_name
        self.issues = list(issues)
        detail = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(
            f"[{pipeline_name}] lockfile 校验失败（{len(self.issues)} 项不一致）:\n{detail}"
        )


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
    #: 可选的执行条件（受限声明式，见 pipeline_core/conditions.py）。
    #: None = 无条件，永远执行 —— 不写 when 的老流水线行为完全不变。
    when: dict | None = None
    #: 引用另一条流水线（子流水线内联）。非空时本节点不是 Agent，
    #: 展开后会被替换成被引流水线的全部节点，节点身份加 `__{本节点名}` 别名。
    call: str = ""

    def __post_init__(self):
        if self.pool_size < 1:
            self.pool_size = 1


@dataclass
class ExecutionNode:
    """执行节点

    命名约定：
      - 当 agent 的 pool_size > 1 时，节点 agent_name 格式为 ``{base_name}_pool_{index}``，
        例如 ``writer_pool_0``、``writer_pool_1``。
      - 当 pool_size == 1 时，agent_name 即原始 agent 名称，无 _pool_ 后缀。
      - 所有依赖展开、schema 校验、lockfile 生成均遵循此约定，
        反向解析统一走 `naming.agent_of()`（还包含内联别名段）。
    """
    agent_name: str
    agent_config: AgentConfig
    dependencies: list = field(default_factory=list)
    timeout: float = 300.0
    max_retries: int = 3
    backoff: str = "exponential"
    initial_delay: float = 1.0
    #: 执行条件（从 AgentConfig.when 复制，便于锁文件与执行器都只看节点）
    when: dict | None = None


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


# ─── Agent 配置契约 ─────────────────────────
#
# 引擎侧不持有 Agent 名单：配置项与默认值由各 Agent 模块的 CONFIG_SCHEMA
# 声明，Scheduler 用 AST 读取（见 pipeline_core/config_schema.py）。
# 过去这里是一张 AGENT_SCHEMAS 表，把 researcher/quality_gate 等 9 个
# 领域 Agent 的名字与默认值写死在 core 里。

from . import config_schema as _config_schema  # noqa: E402


def installed_pipelines(pipeline_dir: str | Path | None = None) -> list[str]:
    """安装目录下的流水线清单（模块级函数，不挂在 Scheduler 上）。

    两个原因：
      1. 实例的 `pipeline_dir` 是相对路径，会随进程 cwd 变化——服务端外壳
         在临时目录里跑就会得到"一条都没有"；
      2. 测试普遍 patch `scheduler.Scheduler` 类，目录查询跟着变 mock 后
         `list(MagicMock())` 是空迭代，可用清单被无声吞成空。
    """
    root = Path(pipeline_dir) if pipeline_dir else (
        Path(__file__).parent.parent / "pipelines")
    return sorted(p.stem for p in root.glob("*.yaml") if not p.name.startswith("_"))


def resolve_pipeline_name(requested: str, available: list[str],
                          configured: str = "") -> tuple[str, str]:
    """决定跑哪条流水线，返回 (名字, 错误信息)。纯函数，便于两侧外壳共用。

    引擎外壳（Admin API / MCP / OpenAPI）此前各自把 `"docgen"` 写成默认值，
    等于在通用引擎里内置了一个具体产品。现在默认值来自配置，
    只接受确实存在的流水线，歧义时不猜而是把可用清单回给调用方。
    """
    names = list(available or [])
    name = str(requested or "").strip()
    if not name:
        cand = str(configured or "").strip()
        name = cand if cand in names else ("" if cand else (names[0] if len(names) == 1 else ""))
        if cand and cand not in names:
            return "", (f"config.default_pipeline '{cand}' 不存在"
                        f"（可用: {', '.join(names) or '无'}）")
    if not name:
        return "", ("未指定 pipeline，且无法确定默认值"
                    f"（可用: {', '.join(names) or '无'}；可设 config.default_pipeline）")
    if name not in names:
        return "", f"pipeline '{name}' 不存在（可用: {', '.join(names) or '无'}）"
    return name, ""


#: 子流水线内联的最大深度：`a → b → c` 是 3。写死上限是因为递归展开一旦成环
#: 会把解析过程挂住，而深度 3 已经覆盖"复用一段核查/发布子流程"的真实用法。
MAX_CALL_DEPTH = 3

#: 内联别名分隔符（见 pipeline_core/naming.py）
_ALIAS_SEP = "__"


class Scheduler:
    """读取 pipeline.yaml 并生成可执行计划"""

    def __init__(self, pipeline_dir: str = "pipelines",
                 agents_dir: str | Path | None = None):
        self.pipeline_dir = Path(pipeline_dir)
        self.pipeline_dir.mkdir(parents=True, exist_ok=True)
        # Agent 源码目录：默认取安装目录，保证 cwd 变了也能找到声明
        self.agents_dir = Path(agents_dir) if agents_dir else (
            Path(__file__).parent.parent / "agents")

    def list_pipelines(self) -> list[str]:
        return [p.stem for p in self.pipeline_dir.glob("*.yaml")
                if not p.name.startswith("_")]

    def visualize(self, plan: ExecutionPlan) -> str:
        """可视化执行计划（ExecutionPlan -> 文本树）"""
        lines = [f"执行计划: {plan.pipeline_name}", "=" * 60]
        for i, level in enumerate(plan.levels):
            lines.append(f"  Level {i + 1}:")
            for node in level:
                deps = ", ".join(node.dependencies) if node.dependencies else "-"
                pool = f" (pool={node.agent_config.pool_size})" if node.agent_config.pool_size > 1 else ""
                lines.append(f"    {node.agent_name}{pool}")
                lines.append(f"      依赖: {deps}")
                lines.append(f"      超时: {node.timeout}s  重试: {node.max_retries}")
            lines.append("")
        lines.append(f"  总节点数: {plan.node_count}")
        return "\n".join(lines)

    # pipeline 名白名单：仅允许字母/数字/下划线/连字符。
    # 防止 MCP 等外部调度入口传入 "../xxx" 之类名称，
    # 借 f"{name}.yaml" 拼拼接读取 pipeline_dir 之外的任意 yaml
    _PIPELINE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def load(self, pipeline_name: str) -> dict:
        if not self._PIPELINE_NAME_RE.match(pipeline_name or ""):
            raise ValueError(
                f"pipeline 名称非法: {pipeline_name!r}（仅允许字母/数字/下划线/连字符）")
        path = self.pipeline_dir / f"{pipeline_name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"pipeline 未找到: {path}")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not raw:
            raise ValueError(f"pipeline 为空: {path}")
        return raw

    def parse(self, pipeline_name: str, verify_lock: bool = True) -> ExecutionPlan:
        raw = self.load(pipeline_name)
        plan = self._build_plan(raw, pipeline_name)
        if verify_lock:
            self._verify_lock_after_parse(plan)
        return plan

    def parse_file(self, filepath: str, verify_lock: bool = True) -> ExecutionPlan:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"pipeline 文件未找到: {filepath}")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        pipeline_name = path.stem
        plan = self._build_plan(raw, pipeline_name)
        if verify_lock:
            self._verify_lock_after_parse(plan)
        return plan

    def _verify_lock_after_parse(self, plan: ExecutionPlan):
        import logging
        logger = logging.getLogger(__name__)
        lock_path = self.pipeline_dir / f"{plan.pipeline_name}.lock"
        if not lock_path.exists():
            logger.debug(
                "[%s] 无 lockfile（%s），跳过版本锁定校验；可用 --write-lock 生成",
                plan.pipeline_name, lock_path,
            )
            return
        issues = self.verify_lockfile(plan, str(lock_path))
        if issues:
            raise LockfileMismatchError(plan.pipeline_name, issues)

    def _deep_merge(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _build_plan(self, raw: dict, pipeline_name: str,
                    _stack: tuple[str, ...] = ()) -> ExecutionPlan:
        # 深度检查放在入口而不是展开点：叶子流水线不含 call，只在展开点查会漏算
        # 最后一层，"上限 3"就变成了实际允许 4。
        chain = _stack + (pipeline_name,)
        if len(chain) > MAX_CALL_DEPTH:
            raise ValueError(
                f"子流水线嵌套超过上限 {MAX_CALL_DEPTH}：{' → '.join(chain)}")
        import logging
        import uuid
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
                when=merged.get("when"),
                call=str(merged.get("call", "") or "").strip(),
            )
            # 条件写法非法要在解析期炸掉：放到运行时才发现，节点会被静默跳过，
            # 流水线照样 done —— 那正是本项目一路在关的静默绿。
            if cfg.when is not None:
                try:
                    validate_condition(cfg.when)
                except ConditionError as e:
                    raise ValueError(f"[{cfg.name}] when 条件非法: {e}") from e
            if cfg.call:
                # 子流水线节点只负责"引谁"，配置属于子流水线自己。
                # 允许 config/when 会让父子两处各有一份真相，展开后行为难以推断。
                blocked = [k for k in ("config", "when", "pool_size", "rate_limit")
                           if merged.get(k)]
                if blocked:
                    raise ValueError(
                        f"[{cfg.name}] 是 call 节点（子流水线 {cfg.call!r}），"
                        f"不接受 {blocked}；请把配置写进被引用的流水线里")
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
        for _lvl_idx, level in enumerate(levels_raw):
            for name in level:
                cfg = agent_map[name]
                deps = [d for d in cfg.dependencies if d in agent_map]
                invalid_deps = [d for d in deps if d not in appeared]
                if invalid_deps:
                    raise ValueError(
                        f"Agent [{name}] 的依赖 {set(invalid_deps)} 不在其前置层级中"
                        f"（同层依赖禁止，会并行执行读到空结果），topology levels={levels_raw}"
                    )
            appeared.update(level)

        # ── 2.1 校验 edges 与 dependencies 一致 ──
        self._validate_edges(topology.get("edges", []), agent_map)

        # ── 3. 构建 ExecutionNode ──
        appeared.clear()
        levels: list[list[ExecutionNode]] = []

        for _lvl_idx, level in enumerate(levels_raw):
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
                        when=cfg.when,
                    ))
                appeared.add(name)

            levels.append(nodes)

        # ── 3.1 内联子流水线（call）──
        levels = self._expand_calls(levels, pipeline_name, _stack)

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

    # ── 子流水线内联（call） ───────────────────

    def _expand_calls(self, levels: list[list[ExecutionNode]], pipeline_name: str,
                      _stack: tuple[str, ...]) -> list[list[ExecutionNode]]:
        """把 `call: 另一条流水线` 展开成该流水线的全部节点（内联）。

        为什么在计划层展开而不是新增一种"子流程节点"执行语义：展开后执行的仍是
        普通节点，幂等键、检查点、重试、熔断、when、产物契约一律照旧生效，不必在
        引擎里再维护一套并行语义。代价是图变大——换来的是行为可预测。

        节点身份规则：内联进来的节点加别名 `__{call 节点名}`，于是同一个 Agent
        可以在一张图里出现多次而互不覆盖（`writer` 与 `writer__review`），
        `agent_of()` 仍还原成 `writer`。
        """
        if not any(n.agent_config.call for lvl in levels for n in lvl):
            return levels        # 绝大多数流水线走这条路：一字不改

        stack = _stack + (pipeline_name,)

        flat: list[ExecutionNode] = []
        exits_of: dict[str, list[str]] = {}     # call 节点名 → 子图出口节点名
        for lvl in levels:
            for node in lvl:
                target = node.agent_config.call
                if not target:
                    flat.append(node)
                    continue
                cloned, entries, exits = self._inline_call(node, target, stack)
                for sub in cloned:
                    if sub.agent_name in entries:
                        # 父图的前置搬到子图入口
                        sub.dependencies = sorted(set(sub.dependencies) | set(node.dependencies))
                flat.extend(cloned)
                exits_of[node.agent_name] = exits

        # 原本依赖 call 节点的父节点，改为依赖子图出口
        for node in flat:
            if any(d in exits_of for d in node.dependencies):
                rewritten: list[str] = []
                for d in node.dependencies:
                    rewritten.extend(exits_of.get(d, [d]))
                node.dependencies = sorted(set(rewritten))

        # 展开会引入新节点并改写依赖，层级必须按依赖重算（作者手写的 levels 已不再
        # 描述这张图）。没有 call 时不走到这里，既有流水线层级保持原样。
        return self._levels_from_deps(flat)

    def _inline_call(self, call_node: ExecutionNode, target: str,
                     stack: tuple[str, ...]) -> tuple[list[ExecutionNode], list[str], list[str]]:
        """展开一个 call 节点，返回（带别名的子节点, 子图入口, 子图出口）。"""
        alias = call_node.agent_name
        # 环要按"流水线名"判，不是按 call 节点名：两条不同名字的节点引用同一个
        # 被引方是合法复用，而 a→b→a 才是环。
        if target in stack:
            raise ValueError(f"子流水线循环引用: {' → '.join(stack)} → {target}")
        try:
            sub_raw = self.load(target)
        except FileNotFoundError as e:
            raise ValueError(
                f"[{alias}] call 指向的流水线不存在: {target!r}（{e}）") from e
        sub_plan = self._build_plan(sub_raw, target, _stack=stack)

        renamed: dict[str, str] = {}
        cloned: list[ExecutionNode] = []
        for lvl in sub_plan.levels:
            for n in lvl:
                new_id = self._alias_id(n.agent_name, alias)
                renamed[n.agent_name] = new_id
                cloned.append(ExecutionNode(
                    agent_name=new_id,
                    agent_config=n.agent_config,
                    dependencies=[renamed.get(d, d) for d in n.dependencies],
                    timeout=n.timeout, max_retries=n.max_retries,
                    backoff=n.backoff, initial_delay=n.initial_delay,
                    when=n.when,
                ))
        # 第二遍：子图内部依赖在改名后才齐备
        for n in cloned:
            n.dependencies = [renamed.get(d, d) for d in n.dependencies]

        entries = [n.agent_name for n in cloned
                   if not [d for d in n.dependencies if d in renamed]]
        depended_on = {d for n in cloned for d in n.dependencies}
        exits = [n.agent_name for n in cloned if n.agent_name not in depended_on]
        if not entries or not exits:
            raise ValueError(
                f"子流水线 {target!r} 无法内联：入口 {entries} / 出口 {exits} 为空")
        return cloned, entries, exits

    @staticmethod
    def _alias_id(node_name: str, alias: str) -> str:
        """给内联节点加别名，保留池下标：`writer_pool_0` → `writer_pool_0__review`。

        嵌套 call 每层各占一个 `__` 段（`writer__sub__inner`），不能压成单段
        `writer__sub_inner`：那样"名字叫 sub_inner 的一条子流水线"与"sub 里再调
        inner"会得出同一个节点身份，dag_nodes 互相覆盖。`agent_of()` 取第一段，
        多段别名不影响 Agent 还原。
        """
        return f"{node_name}{_ALIAS_SEP}{alias}" if _ALIAS_SEP in node_name \
            else node_id(agent_of(node_name), pool_index_of(node_name), alias)

    def _levels_from_deps(self, nodes: list[ExecutionNode]) -> list[list[ExecutionNode]]:
        """按依赖重算层级（Kahn 分层）。同层并行，因此依赖不得落在同层。"""
        by_name = {n.agent_name: n for n in nodes}
        unknown = sorted({d for n in nodes for d in n.dependencies if d not in by_name})
        if unknown:
            raise ValueError(f"展开后存在未知依赖: {unknown}")
        indeg = {n.agent_name: len(n.dependencies) for n in nodes}
        children: dict[str, list[str]] = {n.agent_name: [] for n in nodes}
        for n in nodes:
            for d in n.dependencies:
                children[d].append(n.agent_name)

        levels: list[list[ExecutionNode]] = []
        frontier = sorted(k for k, v in indeg.items() if v == 0)
        placed: set[str] = set()
        while frontier:
            levels.append([by_name[k] for k in frontier])
            placed.update(frontier)
            nxt: set[str] = set()
            for k in frontier:
                for child in children[k]:
                    indeg[child] -= 1
                    if indeg[child] == 0:
                        nxt.add(child)
            frontier = sorted(nxt)
        if len(placed) != len(nodes):
            stuck = sorted(set(by_name) - placed)
            raise ValueError(f"展开后 DAG 存在环，无法分层，涉及节点: {stuck}")
        return levels

    # ── Schema 校验 ─────────────────────

    def _validate_edges(self, edges_raw: list, agent_map: dict[str, AgentConfig]) -> None:
        """校验 `topology.edges` 与 agent.dependencies 声明的图一致。

        执行只依据 dependencies，edges 是给人看的连线图 —— 所以它一旦写错
        不会报错，只会误导人。`docgen-render.yaml` 里 `[layout, safewriter]`
        拼错多年无人发现即为此证（`safe_writer` 才是节点名）。
        """
        if not edges_raw:
            return
        declared: set[tuple[str, str]] = set()
        unknown: list[tuple[str, str]] = []
        for edge in edges_raw:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2:
                raise ValueError(
                    f"topology.edges 条目必须是 [源, 目标] 二元组，收到: {edge!r}")
            src, dst = str(edge[0]), str(edge[1])
            for name in (src, dst):
                if agent_of(name) not in agent_map:
                    unknown.append((src, dst))
            declared.add((src, dst))
        if unknown:
            bad = sorted({f"{s}→{d}" for s, d in unknown})
            raise ValueError(
                f"topology.edges 引用了未定义的 Agent: {bad}"
                f"（可用节点: {sorted(agent_map)}；注意 safewriter ≠ safe_writer）")

        actual = {(d, name) for name, cfg in agent_map.items()
                  for d in cfg.dependencies if d in agent_map}
        if declared != actual:
            missing = sorted(f"{s}→{d}" for s, d in actual - declared)
            extra = sorted(f"{s}→{d}" for s, d in declared - actual)
            raise ValueError(
                "topology.edges 与各 agent 的 dependencies 不一致："
                + (f"edges 缺少 {missing}；" if missing else "")
                + (f"edges 多出 {extra}；" if extra else "")
                + "（执行以 dependencies 为准，edges 请同步修正）")

    def _validate_agent_schemas(self, agent_map: dict[str, AgentConfig]):
        """按各 Agent 自己声明的 CONFIG_SCHEMA 校验并补默认值。

        类型漂移必须在这里报错：Agent 里 `config.get(key, 硬编码默认)` 会
        吞掉错型值（配了字符串 3 秒当成 3 秒用），只有这里有机会拒绝。
        """
        for name, cfg in agent_map.items():
            base_name = agent_of(name)
            schema = self._schema_for_agent(base_name)
            if not schema:
                continue
            for key, (typespec, default) in schema.items():
                if key not in cfg.config:
                    # 深拷贝：schema 里的默认值是共享对象，直接注入会让
                    # 两个节点改同一个 list（同池实例互相污染）
                    cfg.config[key] = copy.deepcopy(default)
                    continue
                value = cfg.config[key]
                if not _config_schema.type_ok(value, typespec):
                    raise TypeError(
                        f"[{name}] config.{key}: 期望 "
                        f"{_config_schema.type_names(typespec)}, "
                        f"实际 {type(value).__name__}={value!r}")

    def _schema_for_agent(self, agent_name: str) -> dict:
        """在 agents_dir 里找该 Agent 的文件并读取其 CONFIG_SCHEMA。"""
        for candidate in (self.agents_dir / f"{agent_name}.py",
                          self.agents_dir / f"{agent_name}_agent.py"):
            if candidate.exists():
                return _config_schema.schema_for_file(candidate)
        return {}
    # ── Lockfile ─────────────────────

    @staticmethod
    def _topology_hash(plan: ExecutionPlan) -> str:
        """拓扑指纹（W4）：锁定连线条目（node→dep 有序集合），防改 YAML 连线绕过校验

        节点上挂的 `when` 条件也算拓扑：给某节点加/改条件，执行行为就变了，
        锁文件必须察觉。没有 when 的节点不贡献条目，因此既有 7 条流水线的
        lockfile 指纹保持不变。
        """
        edges = sorted(
            f"{node.agent_name}->{dep}"
            for level in plan.levels
            for node in level
            for dep in (node.dependencies or [])
        )
        edges += sorted(
            f"{node.agent_name}?{json.dumps(node.when, ensure_ascii=False, sort_keys=True)}"
            for level in plan.levels
            for node in level
            if getattr(node, "when", None) is not None
        )
        return hashlib.sha256(
            json.dumps(edges, ensure_ascii=False).encode()
        ).hexdigest()[:12]

    def generate_lockfile(self, plan: ExecutionPlan, output_dir: str = "pipelines") -> str:
        """生成 pipeline lockfile（版本锁定）"""
        lock = {
            "pipeline": plan.pipeline_name,
            "plan_id": plan.plan_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "node_count": plan.node_count,
            "topology_hash": self._topology_hash(plan),
            "agents": {},
        }
        for level in plan.levels:
            for node in level:
                cfg = node.agent_config
                config_hash = hashlib.sha256(
                    json.dumps(cfg.config, sort_keys=True).encode()
                ).hexdigest()[:12]
                lock["agents"][node.agent_name] = {  # type: ignore[index]
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

        with open(lock_path, encoding="utf-8") as f:
            lock = yaml.safe_load(f) or {}

        issues = []
        if lock.get("pipeline") != plan.pipeline_name:
            issues.append(f"pipeline 名称不匹配: lock={lock.get('pipeline')}, plan={plan.pipeline_name}")

        # W4：拓扑完整性校验。旧格式 lockfile 无 topology_hash 时不阻断
        # （向后兼容），仅提示重锁；新格式一律严格比对。
        expected_topo = self._topology_hash(plan)
        if "topology_hash" not in lock:
            import logging
            logging.getLogger(__name__).warning(
                "lockfile 为旧格式（缺少 topology_hash），建议重新 --write-lock: %s", lockfile)
        elif lock.get("topology_hash") != expected_topo:
            issues.append(
                f"拓扑漂移: topology_hash 不匹配 lock={lock.get('topology_hash')}, "
                f"当前={expected_topo}（YAML 连线已改动，请重新 --write-lock）"
            )

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
                current_hash = hashlib.sha256(
                    json.dumps(node.agent_config.config, sort_keys=True).encode()
                ).hexdigest()[:12]
                if locked.get("config_hash") != current_hash:
                    issues.append(
                        f"[{aname}] 配置漂移: config_hash 不匹配 "
                        f"lock={locked.get('config_hash')}, 当前={current_hash}（配置已改动，请重新 --write-lock）"
                    )

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
                mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                spec.loader.exec_module(mod)  # type: ignore[union-attr]

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
