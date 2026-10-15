"""`RunHandle` 五原语判据（spec §6「API 原语」：status / wait / cancel / resume / artifacts）。

这个原语要替掉的旧状态是：每个接口层各写一份"从 task 对象拼载荷"，而**产物**这一维
谁都没拼——MAOP 只能按节点名造伪 ID（`pipeline:<名>:step<n>:<agent>`）。所以本文件
除了测五个方法本身，还测两条"接线"：MCP `get_task` 与 Admin `GET /tasks/<id>` 的
产物字段必须由这个原语产出（同源），否则它只是一个没人调用的新类。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loom_core import PipelineOrchestrator
from loom_core.run_handle import RunHandle
from loom_core.scheduler import Scheduler

PROJECT = Path(__file__).resolve().parent.parent

# 离线两节点：transform 产出 → safe_writer 落盘。不碰网络、不碰 LLM。
# 落盘与备份路径都在调用方注入的 tmp 目录里——不许写进检出目录。
_OFFLINE_YAML = """\
_name: probe-run-handle
_version: "1.0"
description: RunHandle 探针（无网络、无 LLM）

agents:
  - name: transform
    version: "1.0"
    dependencies: []
    config:
      items: ""
      template: "run handle probe\\n"

  - name: safe_writer
    version: "2.0"
    dependencies: ["transform"]
    config:
      backup_dir: "{backup}"
      atomic: true

topology:
  type: dag
  levels:
    - [transform]
    - [safe_writer]
  edges:
    - [transform, safe_writer]

pipeline:
  timeout: 120
  fail_fast: false
  output: "{out}"
"""


def _offline_yaml(out: Path, backup: Path) -> str:
    return _OFFLINE_YAML.format(out=out.as_posix(), backup=backup.as_posix())


class _StubTask:
    """状态可写的最小任务替身。

    刻意不用"读一次 pop 一个值"的 property：那种写法会让用例通过与否取决于
    `status()` 内部读了几遍属性（review 时发现原写法正是这样侥幸变绿的）。
    """

    def __init__(self, status="running"):
        self.status = SimpleNamespace(value=status)
        self.pipeline_name = "probe"
        self.progress = 0
        self.result = {}
        self.error = ""
        self.output_path = ""
        self.dag_nodes = {}
        self.awaiting_approval = None
        self.approvals = []


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory):
    """真跑一次离线流水线，返回 (orchestrator, task_id, 交付物路径)。"""
    tmp = tmp_path_factory.mktemp("rh")
    out = tmp / "deliverable.md"
    plan_file = tmp / "probe.yaml"
    plan_file.write_text(_offline_yaml(out, tmp / "backups"), encoding="utf-8")

    import contextlib
    saved = {k: os.environ.get(k) for k in ("LOOM_STATE_DIR", "LOOM_VERSIONS_DIR")}
    os.environ["LOOM_STATE_DIR"] = str(tmp / "state")
    os.environ["LOOM_VERSIONS_DIR"] = str(tmp / "versions")
    orch = PipelineOrchestrator(agents_dir=str(PROJECT / "agents"),
                                checkpoint_dir=str(tmp / "ckpt"))
    try:
        orch.register_agents()
        plan = Scheduler(agents_dir=str(PROJECT / "agents"),
                         pipeline_dir=str(PROJECT / "pipelines")).parse_file(
            str(plan_file), verify_lock=False)
        task = orch.run_plan(plan, input_file=str(PROJECT / "test_input.md"),
                             task_id="rh_probe", wait=True)
        assert task.status.value == "done", (task.error, task.to_dict())
        yield orch, task.id, Path(task.output_path)
    finally:
        with contextlib.suppress(Exception):
            orch.shutdown()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ─── 1. status / is_terminal ───────────────────────────

class TestStatus:
    def test_status_after_real_run_is_terminal_done(self, offline_run):
        orch, tid, _ = offline_run
        handle = orch.run_handle(tid)
        assert handle.status() == "done", handle.to_dict()
        assert handle.is_terminal() is True

    def test_handle_reads_latest_not_a_snapshot(self):
        """同一个工作对象的状态推进了，`status()` 必须跟着变（句柄不缓存载荷）。"""
        task = _StubTask("running")
        orch = MagicMock()
        orch.get_task.return_value = task
        handle = RunHandle(orch, "t1")
        assert handle.status() == "running"
        task.status = SimpleNamespace(value="done")
        assert handle.status() == "done"

    def test_handle_reads_through_to_the_orchestrator(self):
        """引擎侧换了工作对象（重跑 / 从队列重新装载）时，旧句柄不能继续报旧状态。

        与上一条分开测：上一条只覆盖"同一对象的状态变了"，把句柄改成"构造时缓存
        task 对象"照样能过；这一条才卡住对象级缓存。
        """
        holder = {"task": _StubTask("running")}
        orch = MagicMock()
        orch.get_task.side_effect = lambda tid: holder["task"]
        handle = RunHandle(orch, "t1")
        assert handle.status() == "running"
        holder["task"] = _StubTask("done")
        assert handle.status() == "done", "句柄缓存了旧的工作对象"

    def test_status_falls_back_to_persisted_queue(self):
        """内存里没有（重启 / 别的机器跑的）时，回落到持久队列读状态——不是 unknown。"""
        orch = _StubOrch(queue_row={"status": "done"})
        assert RunHandle(orch, "t9").status() == "done"

    def test_unknown_when_neither_memory_nor_queue_has_it(self):
        assert RunHandle(_StubOrch(), "nope").status() == "unknown"

    def test_to_dict_on_unknown_task_stays_minimal(self):
        """查无此运行时只给身份与空结构：`result` 明细绝不编，账本-derived 字段给空值。"""
        payload = RunHandle(_StubOrch(), "nope").to_dict()
        assert payload["status"] == "unknown"
        assert payload["task_id"] == payload["id"] == "nope"
        assert payload["terminal"] is False
        assert "result" not in payload, "查无此任务却给了 result 明细"
        assert payload["artifacts"] == [] and payload["pipeline"] == ""


# ─── 2. wait ───────────────────────────────────────────

class TestWait:
    def test_wait_returns_when_task_reaches_done(self, offline_run):
        orch, tid, _ = offline_run
        assert orch.run_handle(tid).wait(timeout=5) == "done"

    def test_wait_honours_timeout_instead_of_hanging(self):
        """超时不抛异常，返回最后一次读到的非终态——调用方靠 is_terminal() 分辨。"""
        task = _StubTask("running")
        orch = MagicMock()
        orch.get_task.return_value = task
        started = time.monotonic()
        got = RunHandle(orch, "t1").wait(timeout=0.2, poll=0.02)
        elapsed = time.monotonic() - started
        assert got == "running"
        assert 0.15 <= elapsed < 2.0, f"wait() 没按预算收手（{elapsed:.3f}s）"

    def test_paused_counts_terminal(self):
        """停在审批闸门（PAUSED）不能算"还在跑"，否则 wait() 会替一个等人决定的任务挂死。"""
        task = _StubTask("paused")
        orch = MagicMock()
        orch.get_task.return_value = task
        started = time.monotonic()
        assert RunHandle(orch, "t1").wait(timeout=30) == "paused"
        assert time.monotonic() - started < 1.0


# ─── 3. cancel / resume 如实回传 ───────────────────────

class TestCancelResume:
    def test_cancel_returns_what_the_engine_says(self):
        orch = _StubOrch(cancel_reply=True)
        assert RunHandle(orch, "t1").cancel() is True
        assert orch.cancel_calls == ["t1"]

    def test_refused_cancel_is_not_swallowed_into_true(self):
        """已完成/不存在的任务取消会被引擎拒绝——句柄不许把它报成成功。"""
        assert RunHandle(_StubOrch(cancel_reply=False), "t1").cancel() is False

    def test_resume_refused_on_approval_gate(self):
        """停在闸门的任务 resume 被引擎拒绝（只认 approve/reject），句柄照实返回 False。"""
        assert RunHandle(_StubOrch(resume_reply=False), "t1").resume() is False


# ─── 4. artifacts ──────────────────────────────────────

class TestArtifacts:
    def test_real_run_lists_node_artifacts_and_the_deliverable(self, offline_run):
        orch, tid, out = offline_run
        items = orch.run_handle(tid).artifacts()
        kinds = {i["kind"] for i in items}
        assert "artifact" in kinds, f"节点产物没被列出：{items}"
        assert "deliverable" in kinds, f"落盘交付物没被列出：{items}"

        deliverable = next(i for i in items if i["kind"] == "deliverable")
        assert Path(deliverable["path"]).name == out.name
        assert deliverable["bytes"] == os.path.getsize(str(out)), \
            "交付物字节数必须来自磁盘真值，不是估算"

        transform_item = next(i for i in items if i["node"] == "transform")
        assert "content" in transform_item["keys"], transform_item

    def test_empty_when_neither_memory_nor_ledger_has_it(self):
        """内存与账本都没有这一运行 ⇒ 空表。

        这里空的理由是**账本里没有这一行**，不是"任务不在内存"——后者本来要由账本补上。
        半成品队列行（只有 status）也不能冒充产物清单。
        """
        assert RunHandle(_StubOrch(queue_row={"status": "done"}), "t9").artifacts() == []

    def test_does_not_invent_pseudo_ids(self):
        """旧做法是按节点名造 `pipeline:x:stepN:agent` 伪 ID；这里只报引擎记下的键。"""
        node = SimpleNamespace(result={"content": "正文"}, status="success")
        task = SimpleNamespace(dag_nodes={"transform": node}, output_path="",
                               status=SimpleNamespace(value="done"))
        orch = MagicMock()
        orch.get_task.return_value = task
        items = RunHandle(orch, "t1").artifacts()
        assert items == [{"kind": "artifact", "node": "transform",
                          "keys": ["content"], "status": "success"}]

    def test_skipped_and_failed_nodes_are_not_listed_as_artifacts(self):
        """没产出就不算产物：被跳过的节点（result 为空）与非 dict 结果都不能进清单。

        这一条盯的是"产物清单虚增"——把 `when` 跳过的节点也列出来，读的人会以为
        它交付过东西（与 FP-1"有字的垃圾算通过"是同一类假结论）。
        """
        nodes = {
            "ok": SimpleNamespace(result={"content": "x"}, status="success"),
            "skipped": SimpleNamespace(result={}, status="skipped"),
            "weird": SimpleNamespace(result="不是 dict", status="success"),
            "none": SimpleNamespace(result=None, status="failed"),
        }
        task = SimpleNamespace(dag_nodes=nodes, output_path="",
                               status=SimpleNamespace(value="done"))
        orch = MagicMock()
        orch.get_task.return_value = task
        items = RunHandle(orch, "t1").artifacts()
        assert [i["node"] for i in items] == ["ok"], items

    def test_deliverable_with_missing_file_reports_minus_one(self):
        """交付物登记了但磁盘读不到（已删/无权限）：字节数给 -1，条目不许消失。"""
        task = SimpleNamespace(dag_nodes={}, output_path=str(Path("nope") / "gone.md"),
                               status=SimpleNamespace(value="done"))
        orch = MagicMock()
        orch.get_task.return_value = task
        items = RunHandle(orch, "t1").artifacts()
        assert len(items) == 1, f"文件没了就不报交付物＝清单虚减：{items}"
        assert items[0]["kind"] == "deliverable"
        assert items[0]["bytes"] == -1, items[0]


# ─── 4b. 账本持久化：被内存窗口淘汰的老运行也查得到产物 ───

def _shape(items: list[dict]) -> list[tuple]:
    """把清单折成可比较的形状（不比顺序，比内容：kind + 节点 + 产物键）。"""
    return sorted((i["kind"], i.get("node", ""), tuple(i.get("keys", [])))
                  for i in items)


class TestDurableArtifacts:
    def test_ledger_row_carries_artifact_keys(self, offline_run):
        """写路径：运行收尾时账本那一行必须带节点产物键——没有它，回落就是空谈。"""
        from loom_core.deliveries import DeliveryLedger

        orch, tid, _ = offline_run
        rec = DeliveryLedger().get(tid)
        assert rec, "run_plan 收尾必须落一行交付账本"
        transform = next(n for n in rec["nodes"] if n["node"] == "transform")
        assert "content" in transform["artifact_keys"], transform

    def test_evicted_task_lists_the_same_artifacts(self, offline_run):
        """`get_task` 拿不到（= 被 _trim_task_history 淘汰）时，账本重建的形状必须与内存态一致。"""
        orch, tid, _ = offline_run
        from_memory = orch.run_handle(tid).artifacts()
        assert from_memory, "内存态基线自己就是空的 ⇒ 这条判据会恒真"
        from_ledger = RunHandle(_StubOrch(), tid).artifacts()   # get_task 恒 None
        assert _shape(from_ledger) == _shape(from_memory), (from_ledger, from_memory)
        deliverable = next(i for i in from_ledger if i["kind"] == "deliverable")
        assert deliverable["bytes"] > 0, deliverable

    def test_evicted_task_to_dict_uses_the_ledger(self, offline_run):
        """降级载荷能给 pipeline / artifacts，但 result 明细仍不给——账本没存那个。"""
        orch, tid, _ = offline_run
        live = orch.run_handle(tid).to_dict()
        payload = RunHandle(_StubOrch(), tid).to_dict()
        # 与在内存时同值，而不是写死一个流水线名（名字取自文件名还是 _name 由
        # Scheduler 定，写死就会跟着解析规则漂移）
        assert payload["pipeline"] == live["pipeline"], (payload, live)
        assert payload["pipeline"], "账本里既然有 pipeline 字段，降级载荷不该给空"
        assert payload["artifacts"] == orch.run_handle(tid).artifacts()
        assert "result" not in payload

    def test_missing_ledger_does_not_get_created_by_a_read(self, tmp_path, monkeypatch):
        """查一个从没跑过的 id：给空表，且不因为读一次就把 deliveries.db 建出来。"""
        state = tmp_path / "empty-state"
        monkeypatch.setenv("LOOM_STATE_DIR", str(state))
        assert RunHandle(_StubOrch(), "never-ran").artifacts() == []
        assert not (state / "deliveries.db").exists(), "只读路径不许有建库副作用"

    def test_unreadable_ledger_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        """账本读不动（文件损坏/被锁）时给空表，不能把整条接口请求打飞。

        这条盯的是降级路径本身：不测它，"读失败返回 None"就只是一句注释。
        """
        state = tmp_path / "broken-state"
        state.mkdir()
        (state / "deliveries.db").write_bytes(b"not a sqlite database file at all")
        monkeypatch.setenv("LOOM_STATE_DIR", str(state))
        assert RunHandle(_StubOrch(), "t1").artifacts() == []
        assert RunHandle(_StubOrch(), "t1").to_dict()["artifacts"] == []

    def test_nodes_without_artifacts_are_dropped_when_rebuilding(self):
        """账本重建时，没有产物键的节点不进清单（与内存态同一条规则）。"""
        rec = {"nodes": [
            {"node": "writer", "artifact_keys": ["content"], "status": "success"},
            {"node": "gate", "artifact_keys": [], "status": "skipped"},
            {"node": "no-key-field", "status": "success"},
        ], "output_path": ""}
        items = RunHandle._artifacts_from(rec)
        assert [i["node"] for i in items] == ["writer"], items


# ─── 5. 接线：两个接口层都由这个原语产出产物字段 ───────
class TestWiring:
    def test_mcp_get_task_artifacts_come_from_the_handle(self, offline_run):
        from loom_core.mcp_server import MCPServer
        orch, tid, _ = offline_run
        server = MCPServer(orch=orch)
        resp = server._handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_task", "arguments": {"task_id": tid}},
        })
        assert resp.get("result", {}).get("isError") is not True, resp
        text = json.loads(resp["result"]["content"][0]["text"])
        # 先要求"确实有东西"：两边同时为空时，等值断言会恒真（变异验证抓到过这个洞）
        assert text["artifacts"], f"MCP 的产物清单是空的：{text}"
        assert text["artifacts"] == orch.run_handle(tid).artifacts(), \
            "MCP 与句柄不同源——产物清单又变成各写一份了"
        # 旧键一个不能少：MAOP 适配器与既有客户端读的就是这几个
        for key in ("id", "status", "pipeline", "result", "error"):
            assert key in text, f"MCP get_task 丢了旧字段 {key}：{sorted(text)}"

    def test_mcp_get_task_degrades_when_history_trimmed_mid_call(self):
        """存在性检查过了、to_dict() 之前被 `_trim_task_history` 淘汰：不许抛 KeyError。

        长驻服务只留最近 100 个任务（pipeline.py:1621）。句柄的降级形态是
        "身份 + 状态 + 空产物"，接口层必须能把它原样发出去，而不是把整条
        JSON-RPC 帧打飞——那属于"读得越晚越像崩"的假故障。
        """
        from loom_core.mcp_server import MCPServer

        task = _StubTask("done")
        reads = {"n": 0}

        class _Evicting:
            def get_task(self, tid):
                reads["n"] += 1
                return task if reads["n"] == 1 else None

            def run_handle(self, tid):
                return RunHandle(self, tid)

            task_queue = SimpleNamespace(get=lambda tid: {"status": "done"})

        resp = MCPServer(orch=_Evicting())._handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_task", "arguments": {"task_id": "t1"}},
        })
        text = json.loads(resp["result"]["content"][0]["text"])
        assert text["status"] == "done"
        assert text["artifacts"] == [] and text["result"] == {} and text["pipeline"] == ""

    def test_admin_task_detail_artifacts_come_from_the_handle(self, offline_run):
        """Admin `GET /tasks/<id>` 的 artifacts 字段由原语产出（真 orch + 真 HTTP）。"""
        import urllib.request

        from loom_core.admin_api import AdminAPI, AdminHandler
        orch, tid, _ = offline_run
        api = AdminAPI(host="127.0.0.1", port=0, api_key="")
        assert api.start(orch) is True
        try:
            port = api._server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/tasks/{tid}",
                                        timeout=5) as r:
                data = json.loads(r.read().decode())
            assert data["artifacts"] == orch.run_handle(tid).artifacts(), \
                "接口层又自己拼了一份产物清单——正是要收掉的重复"
            assert {i["kind"] for i in data["artifacts"]} == {"artifact", "deliverable"}
        finally:
            AdminHandler.orch = None
            api.stop()


class _StubOrch:
    """只回答被问的那几件事的替身（不自动补全属性，防 MagicMock 造出假载荷）。"""

    def __init__(self, task=None, cancel_reply=False, resume_reply=False, queue_row=None):
        self._task = task
        self._cancel_reply = cancel_reply
        self._resume_reply = resume_reply
        self.cancel_calls: list[str] = []
        self.resume_calls: list[str] = []
        if queue_row is not None:
            self.task_queue = SimpleNamespace(get=lambda tid: dict(queue_row))

    def get_task(self, task_id):
        return self._task

    def cancel(self, task_id):
        self.cancel_calls.append(task_id)
        return self._cancel_reply

    def resume(self, task_id):
        self.resume_calls.append(task_id)
        return self._resume_reply
