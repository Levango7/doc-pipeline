"""Agent 配置契约的读取与校验（core 侧只提供机制，不含任何 Agent 名单）。

重构前 `scheduler.AGENT_SCHEMAS` 把 9 个内置 Agent 的配置项与默认值集中写在引擎里：
新增一类任务就要改一次 core，而且引擎由此知道了 `researcher`/`quality_gate` 这些
领域名字。现在每个 Agent 在自己模块里声明：

    CONFIG_SCHEMA = {
        "threshold": (["int", "float"], 70),
        "quality_profile": ("str", "technical-doc"),
    }

Scheduler 用 **AST** 读取该字面量（不执行 Agent 代码，避免 import 副作用与
沙箱绕过），按 `_TYPES` 解析类型名，缺项补默认值、错型直接报错。

类型名必须是字符串：`ast.literal_eval` 处理不了 `int` 这类活对象，
用名字表反而让声明可序列化、可跨进程传递。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

# 允许的类型名 → 实际类型；bool 必须在 int 之前判（isinstance(True, int) 为真）
TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}

# 文件级缓存：{path: (mtime, schema)}；Agent 源码在运行期视为静态
_CACHE: dict[str, tuple[float, dict[str, tuple[Any, Any]]]] = {}


def resolve_types(spec: Any, *, where: str) -> tuple[type, ...]:
    """把类型名（字符串或字符串列表）解析成类型元组。"""
    names = spec if isinstance(spec, list) else [spec]
    out: list[type] = []
    for name in names:
        if not isinstance(name, str) or name not in TYPES:
            raise ValueError(
                f"{where}: 不支持的类型名 {name!r}"
                f"（可用: {', '.join(sorted(TYPES))}）")
        out.append(TYPES[name])
    return tuple(out)


def _literal(node: ast.AST) -> Any:
    return ast.literal_eval(node)  # type: ignore[no-any-return]


def parse_config_schema(source: str, *, origin: str) -> dict[str, tuple[Any, Any]]:
    """从源码里取模块顶层 `CONFIG_SCHEMA` 字面量。

    返回 {配置项: (类型名或类型名列表, 默认值)}；没有声明就返回空字典。
    """
    try:
        tree = ast.parse(source, filename=origin)
    except (SyntaxError, ValueError) as e:
        raise ValueError(f"{origin} 解析失败，无法读取 CONFIG_SCHEMA: {e}") from e

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "CONFIG_SCHEMA" not in names:
            continue
        raw = _literal(node.value)
        if not isinstance(raw, dict):
            raise ValueError(f"{origin}: CONFIG_SCHEMA 必须是字典，实为 {type(raw).__name__}")
        out: dict[str, tuple[Any, Any]] = {}
        for key, value in raw.items():
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or isinstance(value[0], (list, tuple)) and not value[0]):
                raise ValueError(
                    f"{origin}: CONFIG_SCHEMA[{key!r}] 必须是 (类型名, 默认值) 二元组，"
                    f"实为 {value!r}")
            typespec, default = value
            resolve_types(typespec, where=f"{origin}[{key}]")
            out[str(key)] = (typespec, default)
        return out
    return {}


def schema_for_file(agent_file: Path) -> dict[str, tuple[Any, Any]]:
    """读取某个 Agent 文件的 CONFIG_SCHEMA（带 mtime 缓存）。"""
    try:
        stat = agent_file.stat()
    except OSError:
        return {}
    cache_key = str(agent_file)
    cached = _CACHE.get(cache_key)
    if cached and cached[0] == stat.st_mtime:
        return cached[1]
    try:
        schema = parse_config_schema(
            agent_file.read_text(encoding="utf-8"), origin=agent_file.name)
    except (OSError, ValueError) as e:
        # 语法坏掉的 Agent 文件不该让整条流水线解析失败——由 validate() 另行报告
        import logging
        logging.getLogger(__name__).warning("读取 CONFIG_SCHEMA 失败: %s", e)
        schema = {}
    _CACHE[cache_key] = (stat.st_mtime, schema)
    return schema


def type_ok(value: Any, typespec: Any) -> bool:
    types = resolve_types(typespec, where="check")
    if bool in types and isinstance(value, bool):
        return True
    if isinstance(value, bool) and bool not in types:
        # True 不该被当成 int/float 通过校验
        return False
    return isinstance(value, types)


def type_names(typespec: Any) -> str:
    if isinstance(typespec, list):
        return "|".join(typespec)
    return str(typespec)
