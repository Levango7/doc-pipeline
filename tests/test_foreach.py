"""tests/test_foreach.py — `foreach` 逐项展开：解析期约束 + 运行期语义 + 出厂真跑。

对齐 docs/product-spec.md 的 FP-4 四条验收判据，一条不缺：

1. **接线**：`_as_foreach` 必须有调用点（这份文件里 `test_as_foreach_is_wired` 盯着），
   解析结果要落到 `ExecutionNode.foreach` 上；
2. **`max_items` 护栏**：超限报错而不是截断；
3. **幂等键按 item 分裂**：不加 `#{i}` 后缀的话，第 2 项起会命中第 1 项的缓存 ——
   展开 N 次只执行 1 次，而产物看起来完整（`test_idempotency_keys_split_per_item`）；
4. **展开契约进 `topology_hash` 与锁文件**：改 `over`/`max_items` 必须重锁。

另外钉住三件本项目反复出事的地方：任一项失败不许把节点判成成功（静默绿）、
空列表/取不到值不许变成"零项也算跑过"、foreach 不许与落盘/质量重做同用
（两者的对象都是"整份产物"）。
"""
import ast
import json
import pathlib
from unittest.mock import MagicMock

import pytest
import yaml

from pipeline_core.artifacts import ENGINE_OWNED_KEYS, merge_artifact
from pipeline_core.dag_executor import DAGExecutor
from pipeline_core.pipeline import PipelineTask, TaskNode
from pipeline_core.registry import AgentMeta
from pipeline_core.scheduler import (
    DEFAULT_FOREACH_MAX_ITEMS,
    AgentConfig,
    ExecutionNode,
    ExecutionPlan,
    LockfileMismatchError,
    Scheduler,
)

PROJECT = pathlib.Path(__file__).resolve().parent.parent


# ─── 夹具 ────────────────────────────────────────────────

def _sched(pipeline_dir):
    return Scheduler(agents_dir=str(PROJECT / "agents"),
                     pipeline_dir=str(pipeline_dir))


def _agent(name, **kw):
    base = {"name": name, "version": "1.0", "dependencies": [], "config": {}}
    base.update(kw)
    return base


def _write(tmp_path, agents, levels, name="fp"):
    spec = {
        "_name": name,
        "_version": "1.0",
        "agents": agents,
        "topology": {"type": "dag", "levels": levels},
    }
    (tmp_path / f"{name}.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    return tmp_path


def _exec_node(name, foreach=None, deps=None, config=None):
    cfg = AgentConfig(name=name.split("_pool_")[0], config=config or {}, timeout=30)
    return ExecutionNode(agent_name=name, agent_config=cfg,
                         dependencies=deps or [], foreach=foreach, timeout=30)


def _meta(**kw):
    return AgentMeta(name="transform", produces={"text": "last", "content": "last"}, **kw)


# 上游件也必须有 meta：没有 PRODUCES 声明的话 artifacts.* 全取不到，
# "空列表被产物合并筛掉"这条真实路径就测不出来（只会测成"路径不存在"）。
_META_HTTP = AgentMeta(name="http", produces={"response": "last"})


def _executor(meta):
    ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())

    def _get(name):
        if name == "http":
            return _META_HTTP
        return meta if name == meta.name else None

    ex.registry.get_meta.side_effect = _get
    return ex


def _task_with_upstream(result):
    """上游 `http` 节点已成功的任务，供 foreach 在条件上下文里取 artifacts/upstream。"""
    task = PipelineTask(id="t-foreach", pipeline_name="p", input_file="in.md", config={})
    up = _exec_node("http")
    node = _exec_node("transform", deps=["http"])
    dn = TaskNode(name="http", agent_name="http", dependencies=[])
    dn.status = "success"
    dn.result = result
    task.dag_nodes["http"] = dn
    task.dag_nodes["transform"] = TaskNode(name="transform", agent_name="transform",
                                           dependencies=["http"])
    plan = ExecutionPlan(pipeline_name="p", levels=[[up], [node]], raw={})
    return task, node, plan


def _run(ex, task, node, plan, meta, spec, per_item, key="T:transform:0"):
    """跑一次 _execute_foreach，返回 (聚合结果, 每次投递的 (payload, 幂等键))。"""
    calls = []

    def _request(topic="", payload=None, idempotency_key="", **kw):
        calls.append((payload, idempotency_key))
        return per_item(payload, len(calls) - 1)

    ex.bus.request.side_effect = _request
    agg = ex._execute_foreach(task, node, plan, {"task_id": task.id}, "transform",
                               meta, key, spec)
    return agg, calls


class TestParseTimeContract:
    """解析期：写错就炸，绝不带着一份没生效的声明继续跑。"""

    def test_as_foreach_is_wired(self):
        """FP-4 的原始状态就是"scheduler 里有个零调用点的 _as_foreach"。

        这条断言是防回退的：将来谁把调用点删掉、助手又变成死代码，这里先红。
        """
        src = (PROJECT / "pipeline_core" / "scheduler.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        called = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and
                  getattr(n.func, "id", getattr(n.func, "attr", "")) == "_as_foreach"]
        assert called, "_as_foreach 又变成死代码了（定义存在、无人调用）"

    def test_shipped_pipeline_declares_the_expansion(self):
        plan = _sched(PROJECT / "pipelines").parse("api-digest", verify_lock=True)
        node = next(n for lvl in plan.levels for n in lvl if n.agent_name == "transform")
        assert node.foreach == {"over": "artifacts.response", "max_items": 5}
        # 展开不新增节点身份：三个节点还是三个节点
        assert plan.node_count == 3

    def test_default_max_items_applies_when_not_written(self, tmp_path):
        _write(tmp_path, [_agent("a"),
                          _agent("b", dependencies=["a"], foreach={"over": "artifacts.x"})],
               [["a"], ["b"]])
        node = _sched(tmp_path).parse_file(str(tmp_path / "fp.yaml"),
                                           verify_lock=False).levels[1][0]
        assert node.foreach["max_items"] == DEFAULT_FOREACH_MAX_ITEMS

    @pytest.mark.parametrize("spec,needle", [
        ({"overr": "artifacts.x"}, "未知键"),
        ({"over": "artifacts.x", "as": "list"}, "未知键"),
        ({"max_items": 5}, "over 必须是非空路径字符串"),
        ({"over": ""}, "over 必须是非空路径字符串"),
        ({"over": 12}, "over 必须是非空路径字符串"),
        ({"over": "artifacts.x", "max_items": 0}, "max_items 必须是"),
        ({"over": "artifacts.x", "max_items": -3}, "max_items 必须是"),
        ({"over": "artifacts.x", "max_items": "5"}, "max_items 必须是"),
        # YAML 里 `max_items: yes` 会被解析成 True；布尔当数量是错的形状
        ({"over": "artifacts.x", "max_items": True}, "max_items 必须是"),
    ], ids=["typo-key", "extra-key", "no-over", "empty-over", "over-not-str",
            "zero", "negative", "str-int", "bool"])
    def test_bad_specs_are_refused_at_parse_time(self, tmp_path, spec, needle):
        _write(tmp_path, [_agent("a"),
                          _agent("b", dependencies=["a"], foreach=spec)],
               [["a"], ["b"]])
        with pytest.raises(ValueError) as e:
            _sched(tmp_path).parse_file(str(tmp_path / "fp.yaml"), verify_lock=False)
        assert needle in str(e.value), str(e.value)

    def test_list_or_scalar_foreach_is_refused_not_coerced(self, tmp_path):
        _write(tmp_path, [_agent("a"),
                          _agent("b", dependencies=["a"], foreach=["artifacts.x"])],
               [["a"], ["b"]])
        with pytest.raises(ValueError, match="foreach 必须是映射"):
            _sched(tmp_path).parse_file(str(tmp_path / "fp.yaml"), verify_lock=False)

    def test_foreach_on_a_call_node_is_refused(self, tmp_path):
        """call 展开后"本节点"就不存在了，逐项展开没有对象——父子两处不能各有真相。"""
        _write(tmp_path, [_agent("sub", call="other", foreach={"over": "artifacts.x"})],
               [["sub"]])
        with pytest.raises(ValueError) as e:
            _sched(tmp_path).parse_file(str(tmp_path / "fp.yaml"), verify_lock=False)
        assert "call 节点" in str(e.value) and "foreach" in str(e.value)

    def test_foreach_with_pool_size_gt_1_is_refused(self, tmp_path):
        """两种"把一个 Agent 拆成多次执行"的机制叠加时，分配语义没有唯一答案。"""
        _write(tmp_path, [_agent("a"),
                          _agent("b", dependencies=["a"], pool_size=2,
                                 foreach={"over": "artifacts.x"})],
               [["a"], ["b"]])
        with pytest.raises(ValueError, match="foreach 不能与 pool_size>1 同用"):
            _sched(tmp_path).parse_file(str(tmp_path / "fp.yaml"), verify_lock=False)

    def test_expansion_contract_is_locked_in_topology_hash(self, tmp_path):
        """改 max_items 就是改"这趟要投递几次"，锁必须察觉（FP-4 判据 4）。"""
        def topo(max_items):
            _write(tmp_path, [_agent("a"),
                              _agent("b", dependencies=["a"],
                                     foreach={"over": "artifacts.x",
                                              "max_items": max_items})],
                   [["a"], ["b"]])
            plan = _sched(tmp_path).parse_file(str(tmp_path / "fp.yaml"), verify_lock=False)
            return Scheduler._topology_hash(plan)

        assert topo(5) != topo(6)

    def test_lockfile_blocks_a_silent_max_items_change(self, tmp_path):
        _write(tmp_path, [_agent("a"),
                          _agent("b", dependencies=["a"],
                                 foreach={"over": "artifacts.x", "max_items": 5})],
               [["a"], ["b"]])
        sched = _sched(tmp_path)
        plan = sched.parse_file(str(tmp_path / "fp.yaml"), verify_lock=False)
        sched.generate_lockfile(plan, output_dir=str(tmp_path))

        _write(tmp_path, [_agent("a"),
                          _agent("b", dependencies=["a"],
                                 foreach={"over": "artifacts.x", "max_items": 500})],
               [["a"], ["b"]])
        with pytest.raises(LockfileMismatchError) as e:
            sched.parse_file(str(tmp_path / "fp.yaml"), verify_lock=True)
        assert "拓扑漂移" in str(e.value)


class TestRuntimeSemantics:
    """运行期：每一项都真的被投出去，且失败与边界都如实反映。"""

    ITEMS = [{"id": 1}, {"id": 2}, {"id": 3}]

    def _spec(self, over="upstream.http.response", **kw):
        spec = {"over": over, "max_items": kw.get("max_items", 64)}
        return spec

    def test_each_item_gets_its_own_payload_with_engine_keys(self):
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()

        def per_item(payload, idx):
            return {"status": "ok",
                    "text": f"行 {payload['index']}/{payload['count']}",
                    "content": payload.get("content", "")}

        agg, calls = _run(ex, task, node, plan, meta, self._spec(), per_item)
        assert [p["item"] for p, _ in calls] == self.ITEMS
        assert [p["index"] for p, _ in calls] == [0, 1, 2]
        assert all(p["count"] == 3 for p, _ in calls)
        assert agg["count"] == 3
        # 载荷必须是深拷贝：前一项被改过就不能给后一项看到同一个对象
        assert calls[0][0] is not calls[1][0]

    def test_idempotency_keys_split_per_item(self):
        """FP-4 判据 3。共享前缀 + 每项后缀，缺后缀就退化成"只跑第一项"。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()
        agg, calls = _run(ex, task, node, plan, meta, self._spec(),
                          lambda p, i: {"status": "ok", "text": f"T{i}"})
        keys = [k for _, k in calls]
        assert keys == ["T:transform:0#0", "T:transform:0#1", "T:transform:0#2"], keys
        assert len(set(keys)) == 3

    def test_str_artifacts_join_by_line_lists_concat(self):
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        meta = AgentMeta(name="transform",
                         produces={"text": "last", "content": "last", "hits": "list"})
        ex = _executor(meta)

        def per_item(payload, idx):
            return {"status": "ok", "text": f"- 第 {payload['index']} 项",
                    "content": f"- 第 {payload['index']} 项",
                    "hits": [payload["item"]["id"]]}

        agg, _ = _run(ex, task, node, plan, meta, self._spec(), per_item)
        assert agg["text"] == "- 第 0 项\n- 第 1 项\n- 第 2 项"
        assert agg["hits"] == [1, 2, 3]

    def test_other_artifact_types_collect_into_a_list(self):
        """dict 之类的产物不猜"取哪一个"，原样收成列表交给下游。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        meta = AgentMeta(name="transform", produces={"view": "last"})
        ex = _executor(meta)
        agg, _ = _run(ex, task, node, plan, meta, self._spec(),
                      lambda p, i: {"status": "ok", "view": {"id": p["item"]["id"]}})
        assert agg["view"] == [{"id": 1}, {"id": 2}, {"id": 3}]

    def test_any_failed_item_fails_the_node(self):
        """一项失败就把整个节点判失败：宁可不交付，也不交付"少了两项"的成品。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()

        def per_item(payload, idx):
            if idx == 1:
                return {"status": "error", "error": "第 1 项渲染失败"}
            return {"status": "ok", "text": "x"}

        agg, _ = _run(ex, task, node, plan, meta, self._spec(), per_item)
        assert agg["status"] == "error"
        assert "1/3 项失败" in agg["error"] and "第 1 项渲染失败" in agg["error"]

    def test_none_response_counts_as_failed_item(self):
        """订阅者超时返回 None 不许被当成"这一项没内容但也过了"。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()

        def per_item(payload, idx):
            return None if idx == 2 else {"status": "ok", "text": "x"}

        agg, _ = _run(ex, task, node, plan, meta, self._spec(), per_item)
        assert agg["status"] == "error" and "空响应" in agg["error"]

    def test_raw_item_results_stay_available_in_items(self):
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()
        agg, _ = _run(ex, task, node, plan, meta, self._spec(),
                      lambda p, i: {"status": "ok", "text": f"T{i}"})
        assert [r["text"] for r in agg["items"]] == ["T0", "T1", "T2"]

    def test_hard_floor_from_one_item_propagates_to_node(self):
        """逐项里任何一项踩保真底线，整节点都是硬失败（fail_fast=false 也兑不成 done）。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()

        def per_item(payload, idx):
            if idx == 0:
                return {"status": "ok", "text": "x"}
            return {"status": "error", "hard_floor": True,
                    "violations": ["正文是占位稿"], "text": ""}

        agg, _ = _run(ex, task, node, plan, meta, self._spec(), per_item)
        assert agg["hard_floor"] is True
        assert "正文是占位稿" in agg["violations"]

    def test_engine_aggregate_keys_beat_same_named_artifact(self):
        """有人把 count/items 声明成产物时，引擎聚合键仍然说了算（不是反向覆盖）。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        meta = AgentMeta(name="transform", produces={"text": "last", "count": "last"})
        ex = _executor(meta)
        agg, _ = _run(ex, task, node, plan, meta, self._spec(),
                      lambda p, i: {"status": "ok", "text": "x", "count": 99})
        assert agg["count"] == 3
        assert isinstance(agg["items"], list)

    def test_item_index_count_are_engine_owned(self):
        """三项引擎键不许被上游产物顶掉，否则每一项看到的都是同一份数据。"""
        assert {"item", "index", "count"} <= ENGINE_OWNED_KEYS
        for name in ("item", "index", "count"):
            merged = {}
            merge_artifact(merged, name, {"poison": True}, "last")
            assert merged == {}, f"{name} 竟然被当成产物合并了"

    def test_stop_event_aborts_mid_expansion(self):
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()
        task.stop_event.set()
        with pytest.raises(RuntimeError, match="任务已取消"):
            _run(ex, task, node, plan, meta, self._spec(),
                 lambda p, i: {"status": "ok", "text": "x"})

    def test_rate_limited_item_is_an_error_not_a_skip(self):
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        node.agent_config.rate_limit = {"rate": 5, "burst": 1}
        ex, meta = _executor(_meta()), _meta()
        ex._rate_limiters.get_or_create.return_value.acquire.return_value = False
        with pytest.raises(RuntimeError, match="限流令牌超时"):
            _run(ex, task, node, plan, meta, self._spec(),
                 lambda p, i: {"status": "ok", "text": "x"})

    @pytest.mark.parametrize("over,result,needle", [
        ("artifacts.nope", {"response": [1]}, "取不到值"),
        ("upstream.http.response", {"response": {"a": 1}}, "不是列表"),
        ("upstream.http.response", {"response": []}, "空列表"),
        ("artifacts.response", {"response": []}, "取不到值"),
    ], ids=["missing-path", "dict-not-list", "empty-via-upstream",
            "empty-via-artifacts"])
    def test_unusable_lists_fail_loudly_with_the_right_reason(self, over, result, needle):
        """最后一条尤其重要：空列表经产物合并会被筛掉，走 artifacts 时看到的是
        "取不到值"，走 upstream 时看到的是"空列表"——两种都失败，原因各自准确。"""
        task, node, plan = _task_with_upstream(result)
        ex, meta = _executor(_meta()), _meta()
        with pytest.raises(RuntimeError) as e:
            _run(ex, task, node, plan, meta, self._spec(over=over),
                 lambda p, i: {"status": "ok", "text": "x"})
        assert needle in str(e.value)
        ex.bus.request.assert_not_called()

    def test_max_items_is_a_ceiling_not_a_truncation(self):
        task, node, plan = _task_with_upstream({"response": [{"id": i} for i in range(7)]})
        ex, meta = _executor(_meta()), _meta()
        with pytest.raises(RuntimeError) as e:
            _run(ex, task, node, plan, meta, {"over": "upstream.http.response",
                                              "max_items": 5},
                 lambda p, i: {"status": "ok", "text": "x"})
        assert "超过 max_items=5" in str(e.value)
        ex.bus.request.assert_not_called()

    def test_refused_on_regeneration_agent_before_any_dispatch(self):
        """质量重做的对象是"整份产物"，与逐项展开同用语义未定义 ⇒ 解析不出就别投。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        meta = _meta(supports_regeneration=True, regeneration_target="writer")
        ex = _executor(meta)
        with pytest.raises(RuntimeError, match="质量重做"):
            _run(ex, task, node, plan, meta, self._spec(),
                 lambda p, i: {"status": "ok", "text": "x"})
        ex.bus.request.assert_not_called()

    def test_refused_on_output_writing_agent(self):
        """N 项写同一个 target_file 只剩最后一份 —— 落盘必须排在 foreach 之后。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        meta = _meta(writes_output=True)
        ex = _executor(meta)
        with pytest.raises(RuntimeError, match="WRITES_OUTPUT"):
            _run(ex, task, node, plan, meta, self._spec(),
                 lambda p, i: {"status": "ok", "text": "x"})
        ex.bus.request.assert_not_called()

    def test_duplicate_idempotency_key_on_an_item_is_a_failure(self):
        """某项撞上历史键时不能继续当成成功：逐项与普通节点共用同一判据。"""
        task, node, plan = _task_with_upstream({"response": self.ITEMS})
        ex, meta = _executor(_meta()), _meta()

        def per_item(payload, idx):
            if idx == 1:
                return {"error": "duplicate_idempotency_key", "idempotency_key": "k#1"}
            return {"status": "ok", "text": "x"}

        with pytest.raises(RuntimeError) as e:
            _run(ex, task, node, plan, meta, self._spec(), per_item)
        assert "未执行" in str(e.value) and "k#1" in str(e.value)


class TestDuckTypedNodesStayOnTheOldPath:
    """节点在真实调用栈里是被鸭子类型喂进来的（既有测试、process 模式都传 MagicMock）。

    MagicMock 的任意属性都是真值，所以"`getattr(node,'foreach',None)` 为真就展开"
    会把每一个 mock 节点都变成 foreach 节点。`tests/test_fidelity_gate.py` 的两条
    用例就是这么被卷进来的（本轮实测）—— 这里留一条自己的守卫，不去改别人的判据。
    """

    def _mock_node(self):
        node = MagicMock()
        node.agent_name = "writer"
        node.timeout = 5
        node.dependencies = []
        node.agent_config.pool_size = 1
        node.agent_config.config = {}
        node.agent_config.rate_limit = {}
        return node

    def test_mock_node_keeps_single_dispatch_semantics(self):
        ex = DAGExecutor(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        ex._build_node_payload = lambda *a, **k: {}
        ex.bus.request.return_value = {"status": "error",
                                       "error": "duplicate_idempotency_key",
                                       "idempotency_key": "t1:writer:1"}
        task = MagicMock()
        task.id = "t1"
        task.dag_nodes = {"writer": self._mock_node()}
        with pytest.raises(RuntimeError, match="未执行：幂等键"):
            ex.execute_node_from_scheduler(task, task.dag_nodes["writer"],
                                           "in.md", MagicMock())
        assert ex.bus.request.call_count == 1, "mock 节点被当成 foreach 展开了"

    def test_only_a_mapping_counts_as_an_expansion_declaration(self):
        node = self._mock_node()
        node.foreach = "artifacts.response"      # 字符串声明：不是解析期能产出的形状
        task = MagicMock()
        task.dag_nodes = {"writer": node}
        assert not (isinstance(node.foreach, dict) and node.foreach)


class TestTransformConsumesTheEngineTriple:
    """引擎注入的三元组必须在真件的模板里可用 —— 只写进 README 不算接线。

    另一半同样重要：**没被展开时这三个键不存在**（而不是存在且为 None）。
    `payload.get("index")` 那种写法会让 `{{index}}` 渲染出字面量 "None"、
    `{{item.x}}` 渲染成空 —— 一列悄悄没了却仍然出件，正是本项目反复在关的形状。
    """

    def _agent(self, template):
        from agents.transform_agent import TransformAgent
        merged = {"items": "", "fields": [], "where": {}, "set": [], "template": template}
        return TransformAgent(name="transform", meta=MagicMock(), config=merged,
                              message_bus=MagicMock(), registry=MagicMock())

    def _msg(self, payload):
        from pipeline_core.base_agent import Message
        return Message(topic="transform.input", payload=payload, from_agent="test")

    def test_item_index_count_render_in_a_real_template(self):
        agent = self._agent("#{{item.number}}：第 {{index}}/{{count}} 条")
        out = agent.handle(self._msg(
            {"item": {"number": 7}, "index": 2, "count": 5, "upstream": {}}))
        assert out["status"] == "ok", out
        assert out["text"] == "#7：第 2/5 条", out

    def test_keys_absent_without_expansion_fails_loudly(self):
        agent = self._agent("第 {{index}}/{{count}} 条")
        out = agent.handle(self._msg({"upstream": {}}))
        assert out["status"] == "error", out
        assert "取不到值" in out["error"], out

    def test_none_valued_keys_do_not_render_as_the_literal_None(self):
        """显式 None（例如列表里混进 null 项）按"作者没给这项"处理：报错而不是出空白/None。"""
        agent = self._agent("序号 {{index}}")
        out = agent.handle(self._msg({"index": None, "upstream": {}}))
        assert out["status"] == "error", out
        assert "取不到值" in out["error"], "None 被当成有效值渲染进产物了"

    def test_falsy_but_real_item_values_still_render(self):
        """0 / False / "" 是合法项值，不能被上面那条规则一起丢掉。"""
        agent = self._agent("{{item.count}}/{{item.flag}}/{{item.name}}")
        out = agent.handle(self._msg(
            {"item": {"count": 0, "flag": False, "name": ""}, "index": 0, "count": 1,
             "upstream": {}}))
        assert out["status"] == "ok", out
        assert out["text"] == "0/False/", out


class _Resp:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = headers or {}
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=8192):
        return iter([self._body])

    def close(self):
        pass


class TestApiDigestEndToEnd:
    """出厂消费者真跑：只把 requests 换成罐头响应，其余全走真件。

    这是 FP-4 判据里"配一条真跑测试"那一项：单测直接调 `_execute_foreach`
    只能证明函数自己成立，证明不了 YAML → Scheduler → DAGExecutor → Agent
    这条链上真的有人在按项展开。
    """

    def _issues(self, n):
        return json.dumps(
            [{"number": 100 + i, "title": f"问题 {i}", "state": "open"} for i in range(n)],
            ensure_ascii=False).encode("utf-8")

    def _orch_and_plan(self, tmp_path, out_file):
        from pipeline_core import PipelineOrchestrator

        orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                    checkpoint_dir=str(tmp_path / "ck"))
        orch.register_agents()
        plan = _sched(PROJECT / "pipelines").parse("api-digest", verify_lock=False)
        plan.raw.setdefault("pipeline", {})["output"] = str(out_file)
        inp = tmp_path / "in.md"
        inp.write_text("# 主题\n\nforeach 出厂消费者\n", encoding="utf-8")
        return orch, plan, inp

    def _run_pipeline(self, tmp_path, body, out_name="digest.md"):
        from unittest.mock import patch as upatch

        out = tmp_path / out_name
        orch, plan, inp = self._orch_and_plan(tmp_path, out)
        try:
            with upatch("agents.http_request_agent.requests.request",
                        return_value=_Resp(body,
                                           headers={"Content-Type": "application/json"})):
                task = orch.run_plan(plan, input_file=str(inp), wait=True)
        finally:
            orch.shutdown()
        return task, out

    def test_three_items_render_three_lines_and_deliver(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        task, out = self._run_pipeline(tmp_path, self._issues(3))
        assert task.status.value == "done", f"{task.status.value}: {task.error}"
        assert out.exists(), "跑通了却没有交付物，就是 #13 那类假 done"
        body = out.read_text(encoding="utf-8")
        for i in range(3):
            assert f"- [#{100 + i}] 问题 {i}（open）" in body, body
        assert "{{" not in body, f"模板没被渲染：{body[:200]}"
        # 一个节点、三项展开：dag_nodes 里不许冒出 transform#1 这种新身份
        names = [k for k in task.dag_nodes]
        assert names.count("transform") == 1, names
        assert task.dag_nodes["transform"].result["count"] == 3

    def test_exceeding_max_items_fails_instead_of_dropping_the_tail(self, tmp_path,
                                                                     monkeypatch):
        """出厂件写死 max_items=5：给 6 项必须报错，而不是安静地只渲染 5 项。"""
        monkeypatch.chdir(tmp_path)
        task, out = self._run_pipeline(tmp_path, self._issues(6), out_name="over.md")
        assert task.status.value == "failed", task.status
        assert "max_items" in (task.error or ""), task.error
        assert not out.exists(), "护栏开了火，就不该同时落出一份“只有前 5 项”的成品"

    def test_empty_list_fails_instead_of_delivering_an_empty_digest(self, tmp_path,
                                                                   monkeypatch):
        monkeypatch.chdir(tmp_path)
        task, out = self._run_pipeline(tmp_path, b"[]", out_name="empty.md")
        assert task.status.value == "failed", task.status
        assert "foreach.over" in (task.error or ""), task.error
        assert not out.exists()
