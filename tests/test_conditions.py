"""tests/test_conditions.py — `when` 条件语言：算子、类型、缺失路径三张表。

判据要能命中，所以每个方向都配了对照：满足 / 不满足 / 非法写法。
特别注意"取不到路径"这一类——它必须**抛错**而不是返回 False，
否则拼错路径的节点会静默跳过、流水线照样 done（本项目一直在关的那类洞）。
"""
import pytest

from pipeline_core.conditions import (
    _UNRESOLVED,
    OPS,
    ConditionError,
    evaluate,
    resolve,
    validate,
)

CTX = {
    "quality_gate": {"overall_score": 82, "passed": True, "warnings": ["thin_citation"]},
    "doc": {"format": "docx", "name": "", "count": 0},
    "layout": {"optimized": False},
    "numbers": {"n": 3, "flag": True},
}


class TestCompareOps:
    @pytest.mark.parametrize(("path", "op", "value", "expected"), [
        ("quality_gate.overall_score", "==", 82, True),
        ("quality_gate.overall_score", "!=", 82, False),
        ("quality_gate.overall_score", ">=", 70, True),
        ("quality_gate.overall_score", ">=", 90, False),
        ("quality_gate.overall_score", ">", 90, False),
        ("quality_gate.overall_score", "<", 90, True),
        ("quality_gate.overall_score", "<=", 82, True),
        ("doc.format", "==", "docx", True),
        ("doc.format", "!=", "docx", False),
        ("doc.format", "in", ["docx", "pdf"], True),
        ("doc.format", "not in", ["docx", "pdf"], False),
        ("quality_gate.warnings", "in", [["thin_citation"]], True),
        ("quality_gate.overall_score", "in", [80, 82], True),
        ("numbers.flag", "==", True, True),
    ])
    def test_predicates(self, path, op, value, expected):
        spec = {"path": path, "op": op, "value": value}
        assert evaluate(spec, CTX) is expected

    def test_membership_value_must_be_a_list(self):
        with pytest.raises(ConditionError, match="必须是列表"):
            evaluate({"path": "doc.format", "op": "in", "value": "docx"}, CTX)


class TestPresenceOps:
    def test_exists_both_directions(self):
        assert evaluate({"path": "doc.format", "op": "exists"}, CTX) is True
        assert evaluate({"path": "doc.missing_key", "op": "exists"}, CTX) is False

    def test_truthy_falsy_accept_missing_path(self):
        assert evaluate({"path": "doc.name", "op": "truthy"}, CTX) is False
        assert evaluate({"path": "doc.name", "op": "falsy"}, CTX) is True
        assert evaluate({"path": "nope.nope", "op": "truthy"}, CTX) is False
        assert evaluate({"path": "nope.nope", "op": "falsy"}, CTX) is True

    def test_zero_and_empty_are_falsy_not_missing(self):
        assert evaluate({"path": "doc.count", "op": "falsy"}, CTX) is True
        assert evaluate({"path": "doc.count", "op": "==", "value": 0}, CTX) is True


class TestMissingPathMustRaise:
    """拼错路径不能被当成"条件不成立"。"""

    @pytest.mark.parametrize("op", [">=", "==", "in", "!=", ">", "<"])
    def test_comparison_on_missing_path_raises(self, op):
        with pytest.raises(ConditionError, match="取不到值"):
            evaluate({"path": "quality_gate.oversall_score", "op": op, "value": 1}, CTX)

    def test_error_message_lists_available_roots(self):
        with pytest.raises(ConditionError, match="可用顶层键") as exc:
            evaluate({"path": "typo.x", "op": "==", "value": 1}, CTX)
        assert "quality_gate" in str(exc.value)


class TestTypeGuards:
    def test_string_ordering_rejected(self):
        with pytest.raises(ConditionError, match="不适用于"):
            evaluate({"path": "doc.format", "op": ">=", "value": "a"}, CTX)

    def test_numeric_value_must_be_number(self):
        with pytest.raises(ConditionError, match="两侧都必须是数字"):
            evaluate({"path": "quality_gate.overall_score", "op": ">=", "value": "70"}, CTX)

    def test_bool_only_supports_equality(self):
        with pytest.raises(ConditionError, match="不适用于"):
            evaluate({"path": "layout.optimized", "op": ">=", "value": True}, CTX)

    def test_bool_is_not_a_number(self):
        """Python 里 `True == 1` 为真；条件语言不接受这种静默成立。"""
        with pytest.raises(ConditionError, match="类型不一致"):
            evaluate({"path": "numbers.flag", "op": "==", "value": 1}, CTX)
        with pytest.raises(ConditionError, match="类型不一致"):
            evaluate({"path": "quality_gate.overall_score", "op": "!=", "value": True}, CTX)


class TestValidationAtParseTime:
    @pytest.mark.parametrize("bad", [
        "quality_gate.passed",                        # 不是映射
        {"path": "", "op": "==", "value": 1},         # 空路径
        {"path": "a.b", "op": "gte", "value": 1},     # 未知算子
        {"path": "a.b", "op": "eval", "value": 1},    # 更不该出现的算子
        {"path": "a.b", "op": ">="},                  # 缺 value
        {"path": "a.b", "op": "exists", "value": 1},  # exists 不收 value
        {"path": "a.b", "op": "==", "value": 1, "extra": 2},  # 未知键
        {"all": []},                                   # 空列表
        {"all": "not-a-list"},
        {"all": [{"path": "doc.format", "op": "exists"}]},     # 单项还要套 all
        {"all": [{"path": "a", "op": "exists"}], "any": []},   # all/any 并存
    ])
    def test_invalid_specs_raise(self, bad):
        with pytest.raises(ConditionError):
            validate(bad)

    @pytest.mark.parametrize("op", list(OPS))
    def test_every_documented_op_is_acceptable(self, op):
        """表里列出的每个算子都得真能用——否则文档与判据不一致。"""
        if op in ("exists", "truthy", "falsy"):
            spec = {"path": "doc.format", "op": op}
        elif op in ("in", "not in"):
            spec = {"path": "doc.format", "op": op, "value": ["docx"]}
        else:
            # 数值比较走数字路径；字符串只走 ==/!=
            path = ("quality_gate.overall_score"
                    if op in ("<", "<=", ">", ">=") else "doc.format")
            value = 80 if path.startswith("quality_gate") else "docx"
            spec = {"path": path, "op": op, "value": value}
        validate(spec)
        assert isinstance(evaluate(spec, CTX), bool)

    def test_evaluate_validates_before_running(self):
        with pytest.raises(ConditionError, match="非法"):
            evaluate({"path": "doc.format", "op": "matches", "value": "x"}, CTX)


class TestComposition:
    def test_all_and_any(self):
        assert evaluate({"all": [
            {"path": "doc.format", "op": "==", "value": "docx"},
            {"path": "quality_gate.overall_score", "op": ">=", "value": 70},
        ]}, CTX) is True
        assert evaluate({"all": [
            {"path": "doc.format", "op": "==", "value": "docx"},
            {"path": "quality_gate.overall_score", "op": ">=", "value": 95},
        ]}, CTX) is False
        assert evaluate({"any": [
            {"path": "quality_gate.overall_score", "op": ">=", "value": 95},
            {"path": "doc.format", "op": "==", "value": "docx"},
        ]}, CTX) is True
        assert evaluate({"any": [
            {"path": "quality_gate.overall_score", "op": ">=", "value": 95},
            {"path": "doc.format", "op": "==", "value": "pdf"},
        ]}, CTX) is False

    def test_nested_all_inside_any_rejected(self):
        """只允许一层组合，防止又长成一个小表达式语言。"""
        with pytest.raises(ConditionError):
            validate({"any": [{"all": [{"path": "doc.format", "op": "exists"}]}]})


class TestPurity:
    def test_context_is_not_mutated(self):
        import copy

        snapshot = copy.deepcopy(CTX)
        evaluate({"path": "quality_gate.overall_score", "op": ">=", "value": 1}, CTX)
        assert snapshot == CTX

    def test_resolve_returns_sentinel_not_none_for_missing(self):
        """缺失必须返回专属哨兵：None 是合法业务值，拿它当"不存在"会误判。"""
        assert resolve("a.b", {"a": {"b": 1}}) == 1
        assert resolve("a.c", {"a": {"b": 1}}) is _UNRESOLVED
        assert resolve("z.b", {}) is _UNRESOLVED
        assert resolve("a.b", {"a": {"b": None}}) is None  # 真的 None 不是缺失
