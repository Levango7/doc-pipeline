"""cron 解析与下次触发计算（pipeline_core/cron.py）测试。

判据都写成"具体时刻"的断言——cron 的坑集中在边界（日/周 OR 语义、月/年进位、
闰年 2 月 29、永不匹配的 2 月 30），这些必须用 datetime 逐个钉死，不靠眼力。
"""
from datetime import datetime

import pytest

from pipeline_core.cron import CronError, describe, next_fire, parse_cron


class TestParseCron:
    def test_every_minute(self):
        spec = parse_cron("* * * * *")
        assert len(spec.minutes) == 60 and len(spec.hours) == 24
        assert spec.doms is None and spec.dows is None      # `*` 记成未受限

    def test_fixed_fields(self):
        spec = parse_cron("30 9 * * *")
        assert spec.minutes == frozenset({30}) and spec.hours == frozenset({9})

    def test_range_and_star_step(self):
        spec = parse_cron("*/15 8-10 * * *")
        assert sorted(spec.minutes) == [0, 15, 30, 45]
        assert sorted(spec.hours) == [8, 9, 10]

    def test_range_with_step(self):
        assert sorted(parse_cron("10-20/5 * * * *").minutes) == [10, 15, 20]

    def test_list(self):
        assert sorted(parse_cron("1,31,45 * * * *").minutes) == [1, 31, 45]

    def test_step_from_value(self):
        assert sorted(parse_cron("10/20 * * * *").minutes) == [10, 30, 50]

    def test_dow_7_means_sunday(self):
        assert parse_cron("0 0 * * 7").dows == frozenset({0})

    def test_macros(self):
        assert parse_cron("@daily").minutes == frozenset({0})
        assert parse_cron("@daily").hours == frozenset({0})
        assert parse_cron("@weekly").expr == "0 0 * * 0"
        assert parse_cron("@yearly").doms == frozenset({1})

    @pytest.mark.parametrize("bad", [
        "",                    # 空
        "* * * *",             # 少一个字段
        "* * * * * *",         # 多一个字段
        "60 * * * *",          # 分超界
        "* 24 * * *",          # 时超界
        "* * 0 * *",           # 日超界
        "* * 32 * *",          # 日超界
        "* * * 13 *",          # 月超界
        "* * * * 8",           # 周超界
        "a * * * *",           # 非数字
        "1-5/0 * * * *",       # 步长 0
        "*/0 * * * *",         # 步长 0
        "3-1 * * * *",         # 区间倒置
        "@nope",               # 未知宏
        "1,,2 * * * *",        # 空项
    ])
    def test_invalid(self, bad):
        with pytest.raises(CronError):
            parse_cron(bad)

    def test_error_message_names_the_field(self):
        with pytest.raises(CronError, match="分"):
            parse_cron("60 * * * *")
        with pytest.raises(CronError, match="周"):
            parse_cron("* * * * 8")


class TestNextFire:
    def _next(self, expr, after):
        return next_fire(parse_cron(expr), after)

    def test_basic_next_minute(self):
        assert self._next("* * * * *", datetime(2026, 10, 8, 9, 0, 0)) \
            == datetime(2026, 10, 8, 9, 1)

    def test_exact_boundary_is_exclusive(self):
        """同一分钟不重复回给你：09:00 触发过之后下一次是明天 09:00。"""
        assert self._next("0 9 * * *", datetime(2026, 10, 8, 9, 0, 0)) \
            == datetime(2026, 10, 9, 9, 0)

    def test_seconds_round_up(self):
        assert self._next("0 9 * * *", datetime(2026, 10, 8, 8, 59, 30)) \
            == datetime(2026, 10, 8, 9, 0)

    def test_same_day_later_hour(self):
        assert self._next("30 14 * * *", datetime(2026, 10, 8, 9, 0)) \
            == datetime(2026, 10, 8, 14, 30)

    def test_weekday_schedule(self):
        # 2026-10-08 是周四；下个周一 09:00 = 10-12
        assert self._next("0 9 * * 1", datetime(2026, 10, 8, 9, 0)) \
            == datetime(2026, 10, 12, 9, 0)

    def test_month_rollover(self):
        assert self._next("0 0 1 * *", datetime(2026, 10, 8, 12, 0)) \
            == datetime(2026, 11, 1, 0, 0)

    def test_year_rollover(self):
        assert self._next("0 0 1 1 *", datetime(2026, 10, 8)) \
            == datetime(2027, 1, 1)

    def test_dom_dow_or_semantics(self):
        """日与周都受限：命中任一即触发（Vixie cron 的 OR 语义）。"""
        spec = parse_cron("0 0 1 * 1")          # 每月 1 号 或 每个周一
        assert next_fire(spec, datetime(2026, 10, 8)) == datetime(2026, 10, 12)   # 周一先到
        assert next_fire(spec, datetime(2026, 10, 13)) == datetime(2026, 10, 19)  # 仍是周一
        assert next_fire(spec, datetime(2026, 10, 27)) == datetime(2026, 11, 1)   # 1 号命中

    def test_dom_only(self):
        assert self._next("0 0 15 * *", datetime(2026, 10, 8)) \
            == datetime(2026, 10, 15)
        assert self._next("0 0 15 * *", datetime(2026, 10, 16)) \
            == datetime(2026, 11, 15)

    def test_dow_only(self):
        # 周五 00:00；10-09 就是周五
        assert self._next("0 0 * * 5", datetime(2026, 10, 8, 12, 0)) \
            == datetime(2026, 10, 9, 0, 0)

    def test_leap_day(self):
        assert self._next("0 12 29 2 *", datetime(2026, 10, 8)) \
            == datetime(2028, 2, 29, 12, 0)

    def test_impossible_date_raises(self):
        with pytest.raises(CronError, match="没有触发"):
            next_fire(parse_cron("0 0 30 2 *"), datetime(2026, 10, 8))

    def test_describe_ordered(self):
        fires = describe(parse_cron("0 9 * * *"), datetime(2026, 10, 8, 9, 30), count=3)
        assert fires == [
            datetime(2026, 10, 9, 9, 0),
            datetime(2026, 10, 10, 9, 0),
            datetime(2026, 10, 11, 9, 0),
        ]
