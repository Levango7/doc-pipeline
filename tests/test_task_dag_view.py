"""S2 运行详情屏的后端契约（docs/product-spec.md §4.2「最关键一屏」）。

被测契约（全部走真 HTTP，随机端口真 server）：
- `GET /tasks/<id>` 返回 `dag`：逐节点四态 + `retried` 派生态 + skip_reason
  两种语义（`when` 条件跳过 vs 依赖失败级联跳过）+ duration_ms + attempts +
  dependencies + execution_order 层级——前端一次拿全，不再逐个猜；
- **四态与沿用的字段同源**：状态真来自 `TaskNode.status` / `attempts` 等运行态
  字段，不是接口层另造一套数字（防"前端显示成功、后端其实失败"）；
- **没有 DAG 信息的任务给空结构而不是省略键**（legacy 路径不炸前端）；
- 审批闸门字段随详情一起出：`awaiting_approval` + `approvals` 决策流水
  （P1-C 之后"人审"在控制台有落点，S2 同时渲染）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from loom_core.admin_api import AdminAPI, AdminHandler

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture
def orch_mock():
    o = MagicMock()
    o.list_tasks.return_value = []
    o.registry.list.return_value = []
    o.bus.health.return_value = {"status": "ok", "subscribers": 0}
    o.get_task.return_value = None
    return o


@pytest.fixture(autouse=True)
def reset_handler_state():
    yield
    AdminHandler.api_key = None
    AdminHandler.server_host = "127.0.0.1"
    AdminHandler.dashboard_dir = None
    AdminHandler.orch = None


def _start(orch):
    api = AdminAPI(host="127.0.0.1", port=0, api_key="")
    assert api.start(orch) is True
    return api._server.server_address[1], api


def _get(port, path):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, json.loads(r.read().decode())


# ── dag 视图契约（真 TaskNode，非 dict 手搓） ──────────────


class _Node:
    """最小 TaskNode 形状（字段名与 loom_core.pipeline.TaskNode 对齐）。"""

    def __init__(self, name, agent=None, status="success", attempts=1,
                 skip_reason="", error="", started_at=0.0, finished_at=0.0,
                 dependencies=None):
        self.name = name
        self.agent_name = agent or name
        self.status = status
        self.attempts = attempts
        self.skip_reason = skip_reason
        self.error = error
        self.started_at = started_at
        self.finished_at = finished_at
        self.dependencies = dependencies or []


class _Task:
    def __init__(self, nodes, layers=None, steps=None):
        self.id = "t1"
        self.pipeline_name = "demo"
        self.input_file = "in.md"
        self.config = {}
        self.result = {}
        self.status = MagicMock(value="done")
        self.owner = ""
        self.created_at = 1.0
        self.finished_at = 2.0
        self.error = ""
        self.dag_nodes = {n.name: n for n in nodes}
        self.execution_order = layers or []
        self.steps = steps or []
        self.awaiting_approval = None
        self.approvals = []

    def to_dict(self):
        return {"id": self.id}


class TestDagView:
    def test_full_shape_over_http(self, orch_mock):
        now = time.time()
        task = _Task(
            nodes=[
                _Node("fetcher", status="success", started_at=now, finished_at=now + 0.5),
                _Node("researcher", status="success", attempts=3,
                      started_at=now + 0.5, finished_at=now + 1.25,
                      dependencies=["fetcher"]),
                _Node("writer", status="success", started_at=now + 1.25,
                      finished_at=now + 3.0, dependencies=["researcher"]),
            ],
            layers=[["fetcher"], ["researcher"], ["writer"]],
        )
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            status, data = _get(port, "/tasks/t1")
            assert status == 200
            dag = data["dag"]
            assert [n["name"] for n in dag["nodes"]] == ["fetcher", "researcher", "writer"]
            assert dag["layers"] == [["fetcher"], ["researcher"], ["writer"]]
            by_name = {n["name"]: n for n in dag["nodes"]}
            assert by_name["researcher"]["retried"] is True, "attempts>1 且成功 = retried"
            assert by_name["researcher"]["duration_ms"] == 750.0
            assert by_name["researcher"]["attempts"] == 3
            assert by_name["researcher"]["dependencies"] == ["fetcher"]
            assert by_name["writer"]["retried"] is False
        finally:
            api.stop()

    def test_two_skip_semantics_are_distinguishable(self, orch_mock):
        """`when` 条件跳过（虚线分支）与依赖失败级联跳过必须可区分。"""
        task = _Task(
            nodes=[
                _Node("a", status="failed", error="boom"),
                _Node("b", status="skipped", skip_reason="condition"),
                _Node("c", status="skipped"),  # 级联跳过：无 skip_reason
            ],
            layers=[["a"], ["b", "c"]],
        )
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/tasks/t1")
            by_name = {n["name"]: n for n in data["dag"]["nodes"]}
            assert by_name["a"]["status"] == "failed"
            assert by_name["a"]["error"] == "boom"
            assert by_name["b"]["skip_reason"] == "condition"
            assert by_name["c"]["skip_reason"] == ""
        finally:
            api.stop()

    def test_task_without_dag_gets_empty_structure(self, orch_mock):
        task = _Task(nodes={}, layers=[], steps=[])
        task.dag_nodes = None
        task.execution_order = None
        task.steps = None
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/tasks/t1")
            assert data["dag"]["nodes"] == [] and data["dag"]["layers"] == []
            # 断点标记与节点/层级同级给出（S3 同源），不因无 DAG 而省略键
            assert data["dag"]["checkpoint"]["path"] == ""
            assert data["dag"]["checkpoint"]["exists"] is False
        finally:
            api.stop()

    def test_missing_layers_fall_back_to_singletons(self, orch_mock):
        task = _Task(nodes=[_Node("x"), _Node("y")], layers=[])
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/tasks/t1")
            assert data["dag"]["layers"] == [["x"], ["y"]]
        finally:
            api.stop()

    def test_steps_feed_attempts_when_node_attempts_missing(self, orch_mock):
        """TaskNode.attempts 缺失（早期/异常路径）时用 steps 条数兜底。"""
        class _Step:
            def __init__(self, agent):
                self.agent_name = agent
        task = _Task(nodes=[_Node("n1", attempts=0, status="success"),
                            _Node("n2", attempts=0, status="failed")],
                     layers=[["n1"], ["n2"]],
                     steps=[_Step("n1"), _Step("n1"), _Step("n2")])
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/tasks/t1")
            by_name = {n["name"]: n for n in data["dag"]["nodes"]}
            assert by_name["n1"]["attempts"] == 2
            assert by_name["n1"]["retried"] is True   # 两次尝试且最终成功
            assert by_name["n2"]["attempts"] == 1
            assert by_name["n2"]["retried"] is False  # 重试态只属于最终成功者
        finally:
            api.stop()


# ── 审批闸门字段（S2 的操作落点） ─────────────────────────


class TestApprovalFields:
    def test_awaiting_and_history_exposed(self, orch_mock):
        task = _Task(nodes=[_Node("approval", status="skipped")], layers=[["approval"]])
        task.awaiting_approval = {"nodes": ["approval"], "messages": {"approval": "请审"},
                                  "pipeline": "demo"}
        task.approvals = [{"nodes": ["approval"], "decision": "pending"}]
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/tasks/t1")
            assert data["awaiting_approval"]["nodes"] == ["approval"]
            assert data["approvals"][0]["decision"] == "pending"
        finally:
            api.stop()

    def test_absent_gate_gives_empty_not_none_key_missing(self, orch_mock):
        task = _Task(nodes=[_Node("a")], layers=[["a"]])
        orch_mock.get_task.return_value = task
        port, api = _start(orch_mock)
        try:
            _, data = _get(port, "/tasks/t1")
            assert data["awaiting_approval"] is None
            assert data["approvals"] == []
        finally:
            api.stop()
