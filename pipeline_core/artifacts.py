"""产物契约：节点之间传什么，由 Agent 自己声明，引擎不带领域词表。

背景（重构前的耦合）：`dag_executor._build_node_payload` 把上游结果按
字面量拆开——`articles` / `results` / `content` / `spec`，并用一份写死的
优先级表 `_get_latest_content = ["layout","quality_gate","writer","fact_checker"]`
决定谁覆盖谁。那些键名和 agent 名都属于 docgen 这一具体领域，导致
任何新任务类型都要回来改 core。

现在的规则：
  - Agent 在模块里声明 `PRODUCES = {"artifact_name": "last" | "list" | "first"}`
    （或裸列表，按值类型自动选策略）；
  - 引擎把**声明过**的产物按策略合并进下游载荷；没声明的键只能经
    `dependencies_results` 访问，不会被提升；
  - 引擎自有键（task_id/config/queries/...）永不被上游产物覆盖。

合并语义与既往一致：
  `list`   跨产出者拼接（搜索结果、文章列表）——对应旧的 `_get_dep_list_results`
  `last`   取拓扑上最近的产出者（文档正文的最终态）——对应旧的 content_priority 表，
           但顺序来自 DAG 层级而不是写死的 agent 名单
  `first`  取最早的产出者（初始规格这类只该生成一次的东西）
"""
from __future__ import annotations

from typing import Any

MERGE_LAST = "last"
MERGE_LIST = "list"
MERGE_FIRST = "first"
STRATEGIES = (MERGE_LAST, MERGE_LIST, MERGE_FIRST)

# 引擎自己拥有的载荷键：上游产物永远不得覆盖，否则一次检索就会把
# 引擎算好的查询词/任务身份冲掉（旧实现靠键名白名单回避这个问题）。
ENGINE_OWNED_KEYS = frozenset({
    "task_id", "input_file", "config", "pipeline", "node",
    "dependencies_results", "upstream", "queries", "query",
    "target_file", "target", "gate_feedback", "generation_count",
})


def normalize_declaration(produces: Any) -> dict[str, str]:
    """把 PRODUCES 声明规范化为 {产物名: 策略}。

    支持两种写法：
      PRODUCES = ["content", "articles"]                    # 按值类型自动选策略
      PRODUCES = {"content": "last", "articles": "list"}    # 显式指定
    """
    if not produces:
        return {}
    if isinstance(produces, dict):
        out: dict[str, str] = {}
        for name, strategy in produces.items():
            key = str(name)
            value = str(strategy).strip().lower() if strategy else ""
            if value not in STRATEGIES:
                raise ValueError(
                    f"PRODUCES[{key}] 合并策略无效: {strategy!r}"
                    f"（可选: {', '.join(STRATEGIES)}）")
            out[key] = value
        return out
    if isinstance(produces, str):
        return {produces: ""}
    return {str(name): "" for name in produces}


def _strategy_for(declared: str, value: Any) -> str:
    """未显式指定策略时按值类型推断，保证与旧行为一致。"""
    if declared:
        return declared
    return MERGE_LIST if isinstance(value, list) else MERGE_LAST


def merge_artifact(merged: dict[str, Any], name: str, value: Any,
                   strategy: str) -> None:
    """把单个产物按策略并入已累计的结果。"""
    if value is None or value == "" or value == [] or value == {}:
        return
    if name in ENGINE_OWNED_KEYS:
        return
    if strategy == MERGE_LIST:
        if name not in merged:
            merged[name] = []
        target = merged[name]
        if isinstance(target, list):
            target.extend(value if isinstance(value, list) else [value])
        else:
            # 先前已有同名的非列表产物：以"最近的"为准，避免类型漂移
            pass
        return
    if strategy == MERGE_FIRST:
        merged.setdefault(name, value)
        return
    merged[name] = value  # MERGE_LAST


def collect_artifacts(ordered_results: list[tuple[dict, dict[str, str]]],
                      ) -> dict[str, Any]:
    """按"近 → 远"的顺序合并上游产物。

    Args:
        ordered_results: [(agent 结果, 该 agent 的 produces 声明), ...]，
            调用方负责按拓扑距离排好序（近的在前），`last`/`first` 的胜负
            就由这个顺序决定，而不是任何写死的名单。
    """
    merged: dict[str, Any] = {}
    for result, produces in ordered_results:
        if not isinstance(result, dict):
            continue
        for name, strategy in produces.items():
            if name not in result:
                continue
            merge_artifact(merged, name, result[name], _strategy_for(strategy,
                                                                      result[name]))
    return merged
