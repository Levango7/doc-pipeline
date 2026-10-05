"""节点条件（`when`）——受限声明式求值，绝不 exec/eval。

流水线此前只能表达"固定拓扑"：`topology.levels` 是手写的，DAG 一旦确定就照跑，
没有任何"看结果决定"的能力。本模块给 `when` 字段定义一门极小的语言：

    agents:
      - name: renderer
        when:
          path: quality_gate.overall_score   # 点号路径，取自求值上下文
          op: ">="
          value: 70

    # 组合（只嵌套一层，避免又长成一个小表达式语言）
    when:
      all:
        - {path: doc.format, op: "==", value: docx}
        - {path: layout.warnings, op: "not in", value: [missing_image]}

判据设计上有三条硬规矩：

1. **op 白名单**。不在 `OPS` 里的写法在 Scheduler 解析期就报错，不等到运行时；
   比对用了不支持的 op 会被判"条件不成立"从而悄悄跳过节点，那是最坏的错误方向。
2. **路径取不到就抛，不返回 None**。`path` 拼错时若按"取不到=不满足"处理，
   节点会静默跳过、流水线照样 done——正是本项目一路在关的那类静默绿。
   唯一允许路径缺失的 op 是 `exists` / `truthy` / `falsy`。
3. **求值不碰任何被执行的东西**：纯函数，入参只是 dict。

比较语义：数值比较要求两侧都是数字（bool 不算数字），字符串比较只支持
`==` / `!=` / `in`（`>=` 比字符串没有业务意义，直接拒绝）。
"""
from __future__ import annotations

from typing import Any

#: 允许的比较算子
NUMERIC_OPS = ("==", "!=", "<", "<=", ">", ">=")
MEMBERSHIP_OPS = ("in", "not in")
PRESENCE_OPS = ("exists", "truthy", "falsy")
STRING_OPS = ("==", "!=", "in", "not in")
OPS = NUMERIC_OPS + MEMBERSHIP_OPS + PRESENCE_OPS

_UNRESOLVED = object()


class ConditionError(ValueError):
    """`when` 写法非法，或求值时取不到路径。"""


def _is_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _ops_for(left: Any, right: Any) -> tuple[str, ...]:
    if _is_number(left) or left is None:
        return OPS
    if isinstance(left, bool):
        return ("==", "!=")
    if isinstance(left, str):
        return STRING_OPS
    if isinstance(left, (list, tuple, set, dict)):
        return ("==", "!=", "in", "not in")
    return OPS


def validate(spec: Any) -> None:
    """解析期校验：宁可现在报错，也不要在运行时静默跳过节点。"""
    if not isinstance(spec, dict):
        raise ConditionError(
            f"when 必须是映射，实际是 {type(spec).__name__}；"
            "支持形态：{path, op, value} 或 {all: [...]} / {any: [...]}")

    if "all" in spec or "any" in spec:
        key = "all" if "all" in spec else "any"
        others = set(spec) - {key}
        if others:
            raise ConditionError(f"when.{key} 不能与其他键并存: {sorted(others)}")
        items = spec[key]
        if not isinstance(items, list) or not items:
            raise ConditionError(f"when.{key} 必须是非空列表，实际: {items!r}")
        for item in items:
            if isinstance(item, dict) and ("all" in item or "any" in item):
                raise ConditionError(
                    f"when.{key} 里不再嵌套 all/any：只支持一层组合，"
                    "否则条件会变成一门没人能读懂的小语言")
            validate(item)
        if len(spec[key]) == 1:
            raise ConditionError(
                f"when.{key} 只有一个条件，直接写那条条件即可，别套 {key}")
        return

    path = spec.get("path")
    op = spec.get("op")
    if not isinstance(path, str) or not path.strip():
        raise ConditionError(f"when.path 必须是非空字符串，实际: {path!r}")
    if not isinstance(op, str) or op not in OPS:
        raise ConditionError(
            f"when.op 非法: {op!r}（可用: {', '.join(OPS)}）")
    if op in PRESENCE_OPS:
        if "value" in spec:
            raise ConditionError(f"when.op={op} 不接受 value 键")
    else:
        if "value" not in spec:
            raise ConditionError(f"when.op={op} 必须给出 value 键")
    extra = set(spec) - {"path", "op", "value"}
    if extra:
        raise ConditionError(f"when 含未知键: {sorted(extra)}")


def resolve(path: str, ctx: dict) -> Any:
    """按点号路径取值；取不到返回哨兵 _UNRESOLVED（由调用方决定是不是报错）。"""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _UNRESOLVED
    return cur


def evaluate(spec: Any, ctx: dict) -> bool:
    """求一条 when（含 all/any 组合）。ctx 是普通 dict，不执行任何回调。"""
    validate(spec)

    if "all" in spec:
        return all(evaluate(item, ctx) for item in spec["all"])
    if "any" in spec:
        return any(evaluate(item, ctx) for item in spec["any"])

    path, op = spec["path"], spec["op"]
    got = resolve(path, ctx)

    if op == "exists":
        return got is not _UNRESOLVED
    if op == "truthy":
        return bool(got) if got is not _UNRESOLVED else False
    if op == "falsy":
        return got is _UNRESOLVED or not bool(got)

    if got is _UNRESOLVED:
        raise ConditionError(
            f"when.path={path!r} 在上下文里取不到值（op={op} 不允许缺失路径）。"
            f" 可用顶层键: {sorted(k for k in ctx if isinstance(k, str))}")

    want = spec.get("value")
    allowed = _ops_for(got, want)
    if op not in allowed:
        raise ConditionError(
            f"when.op={op!r} 不适用于 {type(got).__name__} 类型的值 "
            f"（可用: {', '.join(allowed)}）")

    if op in ("in", "not in"):
        if not isinstance(want, (list, tuple)):
            raise ConditionError(f"when.op={op} 的 value 必须是列表，实际: {want!r}")
        return bool(got in want) if op == "in" else bool(got not in want)

    # bool 与数字严格分开：Python 里 True == 1 为真，写 `flag == 1` 这种条件
    # 不该静默成立，直接报错让人改写法。
    if isinstance(got, bool) != isinstance(want, bool):
        raise ConditionError(
            f"when 两侧类型不一致，比较没有意义: {got!r} {op} {want!r}"
            "（bool 只能与 bool 比，数字只能与数字比）")

    if op == "==":
        return bool(got == want)
    if op == "!=":
        return bool(got != want)

    # 数值比较：两侧都必须是数字（上面 _ops_for 已挡住字符串场景）
    if not _is_number(got) or not _is_number(want):
        raise ConditionError(f"数值比较 {op} 两侧都必须是数字: {got!r} {want!r}")
    if op == "<":
        return bool(got < want)
    if op == "<=":
        return bool(got <= want)
    if op == ">":
        return bool(got > want)
    return bool(got >= want)
