"""Cron 表达式解析与下次触发计算（5 字段，本地时间）。

为什么自研而不是装 APScheduler / croniter：本仓的引擎层坚持零新依赖
（`pipeline_core` 只依赖 artesian 与标准库），而定时触发需要的只是
"解析 + 算下一次"，这两个纯函数百来行能写透，且可以拿判据钉死语义。

语义按 POSIX cron 的常见子集实现：
- 字段：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-6，0=周日；7 也接受为周日)
- 每字段支持：`*`、`a`、`a-b`、`*/n`、`a-b/n`、`a/n`、逗号列表
- 宏：`@hourly` / `@daily` / `@weekly` / `@monthly` / `@yearly`
- **日与周的 OR 语义**（Vixie cron）：两字段都受限时命中任一即触发；
  只有一个受限时按该字段判定
- 本地时间（naive datetime），不做夏令时补偿——中国无 DST，够用；
  真要做跨时区调度属于另一件事，别在这里偷偷塞时区库

算下次触发用"按天推进"的扫描：不匹配的日期 O(1) 跳一天，匹配的日期在
24×60 个 (时,分) 组合里找第一个候选；`next_fire` 有 5 年上限，防止
`0 0 30 2 *`（2 月 30 日）这类永不触发式把调用方挂死——超限抛 CronError。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

__all__ = ["CronError", "CronSpec", "parse_cron", "next_fire", "describe"]

#: (字段名, 最小值, 最大值, 说明)——周字段最大 7（7 归一为 0）
_FIELDS = (
    ("minute", 0, 59, "分"),
    ("hour", 0, 23, "时"),
    ("dom", 1, 31, "日"),
    ("month", 1, 12, "月"),
    ("dow", 0, 7, "周"),
)

_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

#: next_fire 的扫描上限：超过即视为"永不触发"
_MAX_SCAN_DAYS = 366 * 5


class CronError(ValueError):
    """cron 表达式非法，或表达式在扫描上限内没有触发时刻。"""


@dataclass(frozen=True)
class CronSpec:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    #: None 表示 `*`（未受限）——日/周的 OR 语义要靠这个区分
    doms: frozenset[int] | None
    months: frozenset[int]
    dows: frozenset[int] | None


def _parse_field(raw: str, lo: int, hi: int, label: str) -> tuple[frozenset[int], bool]:
    """单个字段 → (取值集合, 是否未受限)。"""
    if raw == "*":
        return frozenset(range(lo, hi + 1)), True
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{label} 字段有空项: {raw!r}")
        step = 1
        had_step = "/" in part
        if had_step:
            part, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) == 0:
                raise CronError(f"{label} 字段步长非法: {part}/{step_raw}")
            step = int(step_raw)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"{label} 字段区间非法: {part}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if had_step:
                end = hi          # Vixie 语义：`a/n` = a 到字段上限、步长 n
        else:
            raise CronError(f"{label} 字段取值非法: {part!r}（只接受数字、区间与步长）")
        if start < lo or end > hi or start > end:
            raise CronError(f"{label} 字段超界: {part}（允许 {lo}-{hi}）")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError(f"{label} 字段解析为空: {raw!r}")
    return frozenset(values), False


def parse_cron(expr: str) -> CronSpec:
    """解析 5 字段 cron 表达式；非法输入抛 CronError（附可读原因）。"""
    text = (expr or "").strip()
    if not text:
        raise CronError("cron 表达式为空")
    if text.startswith("@"):
        macro = _MACROS.get(text.lower())
        if macro is None:
            raise CronError(f"不认识的宏 {text!r}（支持 {'/'.join(sorted(_MACROS))}）")
        text = macro
    parts = text.split()
    if len(parts) != 5:
        raise CronError(
            f"cron 表达式需要 5 个字段（分 时 日 月 周），收到 {len(parts)} 个: {expr!r}")

    parsed = [_parse_field(raw, lo, hi, label)
              for raw, (_, lo, hi, label) in zip(parts, _FIELDS, strict=True)]
    minutes, _ = parsed[0]
    hours, _ = parsed[1]
    doms, dom_free = parsed[2]
    months, _ = parsed[3]
    dows, dow_free = parsed[4]
    # 7 归一为 0（周日）
    norm_dows = frozenset(0 if d == 7 else d for d in dows)
    return CronSpec(
        expr=text,
        minutes=minutes,
        hours=hours,
        doms=None if dom_free else doms,
        months=months,
        dows=None if dow_free else norm_dows,
    )


def _day_matches(spec: CronSpec, day: date) -> bool:
    """日/周的 OR 语义（Vixie cron）：都受限时命中任一即匹配。"""
    if day.month not in spec.months:
        return False
    dom_ok = spec.doms is None or day.day in spec.doms
    if spec.dows is None:
        return dom_ok
    cron_dow = (day.weekday() + 1) % 7          # python: 周一=0 → cron: 周日=0
    dow_ok = cron_dow in spec.dows
    if spec.doms is None:
        return dow_ok
    return dom_ok or dow_ok


def next_fire(spec: CronSpec, after: datetime) -> datetime:
    """`after` 之后（严格大于，按分钟粒度）的第一个触发时刻。

    粒度：把 `after` 上取整到下一分钟——09:00:00 整点触发过之后，
    `next_fire(..., 09:00:00)` 不会再把 09:00 还给你，而是下一个候选。
    """
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    end_day = (after + timedelta(days=_MAX_SCAN_DAYS)).date()
    hours_sorted = sorted(spec.hours)
    minutes_sorted = sorted(spec.minutes)
    while day <= end_day:
        if _day_matches(spec, day):
            for hh in hours_sorted:
                for mm in minutes_sorted:
                    cand = datetime.combine(day, time(hh, mm))
                    if cand >= start:
                        return cand
        day += timedelta(days=1)
    raise CronError(
        f"表达式 {spec.expr!r} 在 {_MAX_SCAN_DAYS} 天内没有触发时刻"
        f"（例如 2 月 30 日这类永不匹配的日期）")


def describe(spec: CronSpec, after: datetime, count: int = 3) -> list[datetime]:
    """接下来 count 次触发时刻（供 --triggers-dry-run 预览）。"""
    fires: list[datetime] = []
    cursor = after
    for _ in range(count):
        cursor = next_fire(spec, cursor)
        fires.append(cursor)
    return fires
