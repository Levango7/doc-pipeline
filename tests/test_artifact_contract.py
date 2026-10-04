"""产物契约（Phase 1 解耦的第一步）。

重构前 `dag_executor` 用字面量拆上游结果（`articles` / `results` / `content` /
`spec`），并靠一份写死的优先级表 `["layout","quality_gate","writer","fact_checker"]`
决定谁覆盖谁——那些键名和名单都属于 docgen 这一具体领域，任何新任务类型都得回core改。

现在：Agent 声明 PRODUCES/CONSUMES，引擎按声明合并，"谁更新"由 DAG 层级决定。
本文件既测机制，也测**core 里不许再出现领域名**这条退出门。
"""
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pipeline_core import artifacts
from pipeline_core.dag_executor import DAGExecutor
from pipeline_core.registry import AgentMeta


def _ex_with(metas: dict[str, AgentMeta]) -> DAGExecutor:
    ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
    ex.registry.get_meta.side_effect = lambda name: metas.get(name)
    return ex


def _dag(**nodes: tuple) -> dict:
    """nodes: name -> (level_未用, result, dependencies)"""
    out = {}
    for name, (_lvl, result, deps) in nodes.items():
        out[name] = SimpleNamespace(result=result, dependencies=list(deps),
                                    status="success", attempts=0)
    return out


# ─── 1. 声明的规范化 ─────────────────────────────────────

class TestDeclarationNormalization:
    def test_bare_list_and_dict_both_work(self):
        assert artifacts.normalize_declaration(["content", "articles"]) == {
            "content": "", "articles": ""}
        assert artifacts.normalize_declaration({"content": "last"}) == {"content": "last"}

    def test_empty_declaration(self):
        assert artifacts.normalize_declaration(None) == {}
        assert artifacts.normalize_declaration({}) == {}

    def test_invalid_strategy_raises_at_load_time(self):
        """写错策略要在加载期炸，而不是运行时静默按默认走。"""
        with pytest.raises(ValueError, match="合并策略无效"):
            artifacts.normalize_declaration({"content": "latest"})

    def test_strategy_inference_matches_legacy(self):
        """没写策略时按值类型推断：列表拼接、标量取最近——与旧行为一致。"""
        merged = {}
        artifacts.merge_artifact(merged, "articles", [1, 2],
                                 artifacts._strategy_for("", [1, 2]))
        artifacts.merge_artifact(merged, "articles", [3],
                                 artifacts._strategy_for("", [3]))
        assert merged["articles"] == [1, 2, 3]
        artifacts.merge_artifact(merged, "content", "草稿", artifacts._strategy_for("", "草稿"))
        artifacts.merge_artifact(merged, "content", "终稿", artifacts._strategy_for("", "终稿"))
        assert merged["content"] == "终稿"


# ─── 2. 合并语义 ─────────────────────────────────────────

class TestMergeSemantics:
    def test_first_keeps_earliest_producer(self):
        merged = {}
        artifacts.merge_artifact(merged, "spec", {"genre": "tutorial"}, artifacts.MERGE_FIRST)
        artifacts.merge_artifact(merged, "spec", {"genre": "api-ref"}, artifacts.MERGE_FIRST)
        assert merged["spec"] == {"genre": "tutorial"}

    def test_empty_values_never_shadow_real_ones(self):
        merged = {"content": "正文"}
        for empty in ("", None, [], {}):
            artifacts.merge_artifact(merged, "content", empty, artifacts.MERGE_LAST)
        assert merged["content"] == "正文"

    def test_zero_is_a_real_value_not_an_empty_one(self):
        """0 / False 是有效产物，不能当"空"丢掉（计数类 artifact 的首值就是 0）。"""
        merged = {}
        artifacts.merge_artifact(merged, "hits", 0, artifacts.MERGE_LAST)
        assert merged == {"hits": 0}

    def test_engine_owned_keys_are_never_overwritten(self):
        """上游若返回同名键（如检索节点也返回 queries），不得冲掉引擎算好的值。"""
        merged = {}
        artifacts.merge_artifact(merged, "queries", ["垃圾"], artifacts.MERGE_LAST)
        artifacts.merge_artifact(merged, "task_id", "别的任务", artifacts.MERGE_LAST)
        assert merged == {}

    def test_list_strategy_does_not_corrupt_scalar(self):
        merged = {"spec": {"a": 1}}
        artifacts.merge_artifact(merged, "spec", [{"b": 2}], artifacts.MERGE_LIST)
        assert merged["spec"] == {"a": 1}  # 已有非列表值时保持类型稳定


# ─── 3. 按声明收集上游产物 ───────────────────────────────

class TestCollectUpstreamArtifacts:
    def test_only_declared_artifacts_are_hoisted(self):
        metas = {
            "researcher": AgentMeta(name="researcher",
                                    produces={"results": "list"}),
            "gate": AgentMeta(name="gate"),  # 不声明 → 它的分数不外泄成顶层键
        }
        ex = _ex_with(metas)
        task = SimpleNamespace(dag_nodes=_dag(
            researcher=(0, {"status": "ok", "results": [{"t": 1}], "queries": ["q"]}, []),
            gate=(1, {"status": "ok", "overall_score": 42, "violations": []}, ["researcher"]),
        ))
        node = SimpleNamespace(agent_name="consumer", dependencies=["gate"],
                               agent_config=SimpleNamespace(config={}, pool_size=1,
                                                            rate_limit={},
                                                            circuit_breaker={}))
        plan = SimpleNamespace(levels=[[SimpleNamespace(agent_name="researcher")],
                                       [SimpleNamespace(agent_name="gate")],
                                       [SimpleNamespace(agent_name="consumer")]],
                               pipeline_name="p", raw={})
        got = ex._collect_upstream_artifacts(task, node, plan)
        assert got == {"results": [{"t": 1}]}     # 只有 researcher 声明过的才提升
        assert "queries" not in got               # 引擎自有键
        assert "overall_score" not in got         # 未声明

    def test_nearest_producer_wins_by_dag_level_not_by_name(self):
        """旧实现靠 content_priority 名单；现在层级决定先后。"""
        metas = {
            "writer": AgentMeta(name="writer", produces={"content": "last"}),
            "polisher": AgentMeta(name="polisher", produces={"content": "last"}),
        }
        ex = _ex_with(metas)
        task = SimpleNamespace(dag_nodes=_dag(
            writer=(0, {"content": "初稿"}, []),
            polisher=(1, {"content": "润色稿"}, ["writer"]),
        ))
        node = SimpleNamespace(agent_name="sink", dependencies=["polisher"],
                               agent_config=SimpleNamespace(config={}, pool_size=1,
                                                            rate_limit={},
                                                            circuit_breaker={}))
        plan = SimpleNamespace(levels=[[SimpleNamespace(agent_name="writer")],
                                       [SimpleNamespace(agent_name="polisher")],
                                       [SimpleNamespace(agent_name="sink")]],
                               pipeline_name="p", raw={})
        assert ex._collect_upstream_artifacts(task, node, plan) == {"content": "润色稿"}

    def test_closure_bridges_nodes_that_re_export_nothing(self):
        """中间节点（如只做结构检查）不重新导出正文，也不能断链。"""
        metas = {"gen": AgentMeta(name="gen", produces={"content": "last"})}
        ex = _ex_with(metas)
        task = SimpleNamespace(dag_nodes=_dag(
            gen=(0, {"content": "正文"}, []),
            middle=(1, {"status": "ok", "P0": 0}, ["gen"]),
        ))
        node = SimpleNamespace(agent_name="sink", dependencies=["middle"],
                               agent_config=SimpleNamespace(config={}, pool_size=1,
                                                            rate_limit={},
                                                            circuit_breaker={}))
        plan = SimpleNamespace(levels=[[SimpleNamespace(agent_name="gen")],
                                       [SimpleNamespace(agent_name="middle")],
                                       [SimpleNamespace(agent_name="sink")]],
                               pipeline_name="p", raw={})
        assert ex._collect_upstream_artifacts(task, node, plan) == {"content": "正文"}

    def test_pool_siblings_are_merged(self):
        metas = {"worker": AgentMeta(name="worker", produces={"items": "list"})}
        ex = _ex_with(metas)
        task = SimpleNamespace(dag_nodes=_dag(
            worker_pool_0=(0, {"items": [1, 2]}, []),
            worker_pool_1=(0, {"items": [3]}, []),
        ))
        node = SimpleNamespace(agent_name="sink", dependencies=["worker_pool_0", "worker_pool_1"],
                               agent_config=SimpleNamespace(config={}, pool_size=1,
                                                            rate_limit={},
                                                            circuit_breaker={}))
        plan = SimpleNamespace(levels=[
            [SimpleNamespace(agent_name="worker_pool_0"),
             SimpleNamespace(agent_name="worker_pool_1")],
            [SimpleNamespace(agent_name="sink")]], pipeline_name="p", raw={})
        assert sorted(ex._collect_upstream_artifacts(task, node, plan)["items"]) == [1, 2, 3]

    def test_artifacts_from_respects_declaration(self):
        ex = _ex_with({"gen": AgentMeta(name="gen", produces={"content": "last"})})
        got = ex._artifacts_from("gen", {"content": "x", "status": "ok", "stats": {}})
        assert got == {"content": "x"}


# ─── 4. 领域名不得回流 core（Phase 1 退出门）─────────────

class TestCoreStaysDomainNeutral:
    """core 里出现 agent 名或领域键字面量，就意味着新任务类型要回来改引擎。"""

    DOMAIN_NAMES = ("docgen", "requirements_analyzer", "safe_writer", "quality_gate",
                    "fact_checker", "researcher", "fetcher", "writer", "layout")

    def _core_source(self, mod) -> str:
        return Path(inspect.getfile(mod)).read_text(encoding="utf-8")

    def _code_strings(self, mod) -> list[str]:
        """只取代码里的字符串字面量，docstring 不计。

        core 的注释里**应该**留着"以前写死了哪些名字"的说明（那是本次重构的
        证据），把它们一起算进来会让护栏变成噪音。
        """
        import ast as _ast

        src = Path(inspect.getfile(mod)).read_text(encoding="utf-8")
        tree = _ast.parse(src)
        doc_nodes = set()
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef,
                                 _ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], _ast.Expr) and isinstance(
                        body[0].value, _ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    doc_nodes.add(id(body[0].value))
        return [n.value for n in _ast.walk(tree)
                if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                and id(n) not in doc_nodes]

    @pytest.mark.parametrize("mod_name", [
        "pipeline_core.dag_executor", "pipeline_core.artifacts",
        "pipeline_core.registry", "pipeline_core.message_bus_v3",
        "pipeline_core.pipeline", "pipeline_core.agent_loader",
    ])
    def test_no_agent_names_in_orchestration_core(self, mod_name):
        import importlib
        strings = self._code_strings(importlib.import_module(mod_name))
        hits = sorted({n for n in self.DOMAIN_NAMES if n in strings})
        assert not hits, f"{mod_name} 仍把领域名写进代码: {hits}"

    def test_content_priority_table_is_gone(self):
        src = self._core_source(DAGExecutor)
        assert "content_priority" not in src
        assert '"articles"' not in src and '"spec"' not in src

    def test_scheduler_no_longer_holds_agent_roster(self):
        """AGENT_SCHEMAS 已下沉为各 Agent 的 CONFIG_SCHEMA（P1-2）。"""
        from pipeline_core import scheduler
        assert not hasattr(scheduler, "AGENT_SCHEMAS"), "配置表又回到 core 了"
        strings = self._code_strings(scheduler)
        hits = sorted({n for n in self.DOMAIN_NAMES if n in strings})
        assert not hits, f"scheduler 仍写死领域名: {hits}"

    def test_schema_is_read_without_executing_agent_code(self, tmp_path):
        """AST 读取：Agent 模块里抛异常/有副作用也不该影响解析期校验。"""
        from pipeline_core.config_schema import parse_config_schema
        src = ("CONFIG_SCHEMA = {'threshold': (['int', 'float'], 70)}\n"
               "raise RuntimeError('import 时不该执行我')\n")
        assert parse_config_schema(src, origin="fake.py") == {
            "threshold": (["int", "float"], 70)}

    def test_schema_declaration_is_validated(self):
        from pipeline_core.config_schema import parse_config_schema
        with pytest.raises(ValueError, match="不支持的类型名"):
            parse_config_schema("CONFIG_SCHEMA = {'x': ('integer', 1)}", origin="a.py")
        with pytest.raises(ValueError, match="二元组"):
            parse_config_schema("CONFIG_SCHEMA = {'x': 'int'}", origin="b.py")

    def test_bool_is_not_accepted_where_int_expected(self):
        """isinstance(True, int) 为真，配错成 bool 必须被拦下。"""
        from pipeline_core.config_schema import type_ok
        assert type_ok(3, "int") is True
        assert type_ok(True, "int") is False
        assert type_ok(True, "bool") is True
        assert type_ok(2.5, ["int", "float"]) is True

    def test_injected_defaults_are_not_shared_objects(self, tmp_path):
        """默认值必须是每节点独立副本，否则同池实例会互相改配置。"""
        from pipeline_core.scheduler import Scheduler
        sched = Scheduler()
        raw = {
            "name": "schemademo",
            "agents": [
                {"name": "researcher", "version": "1.0", "dependencies": [], "config": {}},
                {"name": "fetcher", "version": "1.0", "dependencies": ["researcher"],
                 "config": {}},
            ],
            "topology": {"type": "dag", "levels": [["researcher"], ["fetcher"]]},
        }
        import yaml
        target = tmp_path / "schemademo.yaml"
        target.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        plan = sched.parse_file(str(target), verify_lock=False)
        a = plan.levels[0][0].agent_config.config["search_engines"]
        b = plan.levels[1][0].agent_config.config.get("search_engines")
        assert a == ["bing"]
        assert b is None or a is not b
