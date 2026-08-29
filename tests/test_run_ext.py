"""run.py 补充测试 — 补齐覆盖率薄弱区（原 61%）。

与 test_run.py 互补（后者覆盖参数解析/服务路由/YAML 降级），本文件覆盖：
- _run_ascii_fix / _run_export（patch scripts 转换器）
- _load_config / _task_exit_code / _print_task_summary / _collect_steps
- _poll_task_progress（正常完成 + KeyboardInterrupt 暂停 + daemon 驻留）
- _resolve_output_path / _render_task_result（JSON/人类可读/降级警告/导出分发）
- _run_single_task（预览/dry-run/plan 缺失退出/中断返回 None）
- _run_daemon / main 的 --check/--mcp/--recover/--enhance/端口非法/启动失败/退出码

注意：test_run.py 会 `del sys.modules["run"]` 后重新 import，因此本文件
不持有模块级 `import run` 引用，所有用例通过 importlib 现取当前模块实例，
并用 patch.object 直接打补丁，避免 patch 目标与调用对象不是同一模块实例。
"""
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline_core import TaskStatus  # noqa: E402


def _run_module():
    """取当前 sys.modules 中的 run 模块实例（被删除则重新导入）"""
    return importlib.import_module("run")


def _args(argv: list[str]):
    return _run_module().build_arg_parser().parse_args(argv)


# ─── _run_ascii_fix / _run_export ────────────────────────────

class TestAsciiFixAndExport:
    def test_ascii_fix_converts_and_writes(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("ORIGINAL", encoding="utf-8")
        with patch("scripts.convert_ascii.AsciiConverter") as cls:
            cls.return_value.detect_and_convert.return_value = "CONVERTED"
            _run_module()._run_ascii_fix(str(doc))
        assert doc.read_text(encoding="utf-8") == "CONVERTED"

    def test_ascii_fix_no_change(self, tmp_path, capsys):
        doc = tmp_path / "doc.md"
        doc.write_text("SAME", encoding="utf-8")
        with patch("scripts.convert_ascii.AsciiConverter") as cls:
            cls.return_value.detect_and_convert.return_value = "SAME"
            _run_module()._run_ascii_fix(str(doc))
        assert "未检测到" in capsys.readouterr().out

    def test_ascii_fix_exception_reported(self, tmp_path, capsys):
        with patch("scripts.convert_ascii.AsciiConverter",
                   side_effect=RuntimeError("boom")):
            _run_module()._run_ascii_fix(str(tmp_path / "doc.md"))
        assert "ASCII 转换失败" in capsys.readouterr().out

    def test_export_html_default_path(self, capsys):
        with patch("scripts.format_converter.FormatConverter") as cls:
            _run_module()._run_export("/tmp/doc.md", "html")
        cls.return_value.markdown_to_html.assert_called_once_with(
            "/tmp/doc.md", "/tmp/doc.html")
        assert "HTML 已导出" in capsys.readouterr().out

    def test_export_word_custom_path(self, capsys):
        with patch("scripts.format_converter.FormatConverter") as cls:
            _run_module()._run_export("/tmp/doc.md", "word", "/tmp/out.docx")
        cls.return_value.markdown_to_word.assert_called_once_with(
            "/tmp/doc.md", "/tmp/out.docx")

    def test_export_png(self, capsys):
        with patch("scripts.format_converter.FormatConverter") as cls:
            _run_module()._run_export("/tmp/doc.md", "png", "/tmp/imgs")
        cls.return_value.render_mermaid_in_markdown.assert_called_once()

    def test_export_exception_reported(self, capsys):
        with patch("scripts.format_converter.FormatConverter",
                   side_effect=RuntimeError("boom")):
            _run_module()._run_export("/tmp/doc.md", "html")
        assert "导出失败" in capsys.readouterr().out


# ─── _load_config / _task_exit_code / _print_task_summary ─────

class TestSmallHelpers:
    def test_load_config_from_explicit_path(self, tmp_path):
        cfg = tmp_path / "my.json"
        cfg.write_text(json.dumps({"k": 1}), encoding="utf-8")
        args = _args(["x.md", "--config", str(cfg)])
        assert _run_module()._load_config(args, tmp_path) == {"k": 1}

    def test_load_config_from_project_root(self, tmp_path):
        (tmp_path / "config.json").write_text('{"a": 2}', encoding="utf-8")
        args = _args(["x.md"])
        assert _run_module()._load_config(args, tmp_path) == {"a": 2}

    def test_load_config_missing_returns_empty(self, tmp_path):
        args = _args(["x.md"])
        assert _run_module()._load_config(args, tmp_path) == {}

    def test_load_config_non_dict_returns_empty(self, tmp_path):
        (tmp_path / "config.json").write_text("[1, 2]", encoding="utf-8")
        args = _args(["x.md"])
        assert _run_module()._load_config(args, tmp_path) == {}

    def test_pipeline_names_memoized(self, monkeypatch):
        """性能优化回归：_available_pipeline_names 进程内缓存一次目录扫描，
        返回副本防调用方污染"""
        import pathlib
        run = _run_module()
        glob_calls = []

        class _FakeDir:
            def exists(self):
                return True

            def __truediv__(self, other):
                return self

            def glob(self, pat):
                glob_calls.append(pat)
                return [pathlib.Path("docgen.yaml")]

        class _FakePath:
            def __init__(self, *a, **k):
                pass

            @property
            def parent(self):
                return _FakeDir()

        monkeypatch.setattr(run, "Path", _FakePath)
        monkeypatch.setattr(run, "_PIPELINE_NAMES_CACHE", None)
        assert run._available_pipeline_names() == ["docgen"]
        assert run._available_pipeline_names() == ["docgen"]
        assert glob_calls == ["*.yaml"]  # 目录只扫一次
        # 返回副本：调用方修改不影响缓存
        names = run._available_pipeline_names()
        names.append("hacked")
        assert run._available_pipeline_names() == ["docgen"]

    def test_pipeline_names_missing_dir_cached_empty(self, monkeypatch):
        run = _run_module()

        class _FakeDir:
            def exists(self):
                return False

            def __truediv__(self, other):
                return self

        class _FakePath:
            def __init__(self, *a, **k):
                pass

            @property
            def parent(self):
                return _FakeDir()

        monkeypatch.setattr(run, "Path", _FakePath)
        monkeypatch.setattr(run, "_PIPELINE_NAMES_CACHE", None)
        assert run._available_pipeline_names() == []
        assert run._PIPELINE_NAMES_CACHE == []  # 空目录结果同样缓存

    def test_task_exit_code_mapping(self):
        run = _run_module()
        assert run._task_exit_code(SimpleNamespace(status=TaskStatus.FAILED)) == 1
        assert run._task_exit_code(SimpleNamespace(status=TaskStatus.CANCELLED)) == 2
        assert run._task_exit_code(SimpleNamespace(status=TaskStatus.DONE)) == 0
        # 字符串状态
        assert run._task_exit_code(SimpleNamespace(status="failed")) == 1
        # status 访问异常 → 0
        class _BadStatus:
            @property
            def value(self):
                raise RuntimeError("no value")
        bad = SimpleNamespace(status=_BadStatus())
        assert run._task_exit_code(bad) == 0

    def test_task_exit_code_none_task(self):
        assert _run_module()._task_exit_code(SimpleNamespace(status=None)) == 0

    def test_print_task_summary(self, capsys):
        args = _args(["input.md", "-q", "查询A", "查询B", "--resume"])
        orch = MagicMock()
        orch.registry.list_agent_names.return_value = ["writer", "fetcher"]
        _run_module()._print_task_summary(args, orch, "task-1", use_legacy=False)
        out = capsys.readouterr().out
        assert "task-1" in out and "声明式 DAG" in out
        assert "writer, fetcher" in out
        assert "查询A" in out and "断点续传" in out


# ─── _poll_task_progress ────────────────────────────

class _ProgressTask:
    """按序弹出 status 的假任务"""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.progress = 42

    @property
    def status(self):
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]


class TestPollTaskProgress:
    def test_completes_normally(self):
        orch = MagicMock()
        args = _args(["x.md"])
        task = _ProgressTask([TaskStatus.RUNNING, TaskStatus.DONE])
        with patch("time.sleep"):
            assert _run_module()._poll_task_progress(orch, args, task, "t1") is True
        orch.pause.assert_not_called()

    def test_keyboard_interrupt_pauses_task(self):
        orch = MagicMock()
        args = _args(["x.md"])
        task = _ProgressTask([TaskStatus.RUNNING])
        with patch("time.sleep", side_effect=KeyboardInterrupt):
            assert _run_module()._poll_task_progress(orch, args, task, "t1") is False
        orch.pause.assert_called_once_with("t1")

    def test_keyboard_interrupt_daemon_stays_alive(self):
        orch = MagicMock()
        args = _args(["x.md", "--daemon"])
        task = _ProgressTask([TaskStatus.RUNNING])
        # 第一次中断在轮询，第二次中断退出守护等待
        with patch("time.sleep", side_effect=[KeyboardInterrupt, KeyboardInterrupt]):
            assert _run_module()._poll_task_progress(orch, args, task, "t1") is False
        orch.pause.assert_called_once_with("t1")


# ─── _collect_steps / _resolve_output_path ────────────────────────────

class TestStepsAndOutputPath:
    def test_collect_steps(self):
        step = SimpleNamespace(step_name="s1", agent_name="writer",
                               status="success", duration_ms=12.5,
                               started_at=1.0, finished_at=2.0,
                               result={"ok": True})
        task = SimpleNamespace(steps=[step])
        steps = _run_module()._collect_steps(task)
        assert steps[0]["agent_name"] == "writer"
        assert steps[0]["result"] == {"ok": True}

    def test_collect_steps_without_result_attr(self):
        class _Step:
            step_name = "s"
            agent_name = "a"
            status = "success"
            duration_ms = 1.0
            started_at = 0.0
            finished_at = 1.0
        task = SimpleNamespace(steps=[_Step()])
        assert _run_module()._collect_steps(task)[0]["result"] == {}

    def test_collect_steps_empty(self):
        assert _run_module()._collect_steps(SimpleNamespace(steps=[])) == []

    def test_resolve_output_path_cli_wins(self, tmp_path):
        out = tmp_path / "o.md"
        out.write_text("x", encoding="utf-8")
        args = _args(["x.md", "-o", str(out)])
        task = SimpleNamespace(result={"output_path": "other.md"})
        assert _run_module()._resolve_output_path(args, task) == str(out)

    def test_resolve_output_path_from_result(self):
        args = _args(["x.md"])
        task = SimpleNamespace(result={"output_path": "res.md"})
        assert _run_module()._resolve_output_path(args, task) == "res.md"

    def test_resolve_output_path_from_task_attr(self):
        args = _args(["x.md"])

        class _Task:
            result = {}
            output_path = "attr.md"
        assert _run_module()._resolve_output_path(args, _Task()) == "attr.md"

    def test_resolve_output_path_empty(self):
        args = _args(["x.md"])

        class _Task:
            result = {}
        assert _run_module()._resolve_output_path(args, _Task()) == ""


# ─── _render_task_result ────────────────────────────

def _done_task(**kw):
    base = dict(status=TaskStatus.DONE, error=None, steps=[], result={},
                finished_at=2.0, started_at=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


class TestRenderTaskResult:
    def test_json_output_branch(self, capsys):
        args = _args(["x.md", "--json-output"])
        _run_module()._render_task_result(args, _done_task(), "t1")
        out = json.loads(capsys.readouterr().out.strip())
        assert out["exit_code"] == 0 and out["status"] == "done"

    def test_human_report_with_steps_and_error(self, capsys):
        step_ok = SimpleNamespace(step_name="s1", agent_name="writer",
                                  status="success", duration_ms=10.0,
                                  started_at=0.0, finished_at=1.0, result={})
        step_bad = SimpleNamespace(step_name="s2", agent_name="fetcher",
                                   status="failed", duration_ms=5.0,
                                   started_at=0.0, finished_at=1.0, result={})
        step_skip = SimpleNamespace(step_name="s3", agent_name="checker",
                                    status="skipped", duration_ms=1.0,
                                    started_at=0.0, finished_at=1.0, result={})
        task = _done_task(steps=[step_ok, step_bad, step_skip], error="boom")
        args = _args(["x.md"])
        _run_module()._render_task_result(args, task, "t1")
        out = capsys.readouterr().out
        assert "✅" in out and "❌" in out and "⏭️" in out
        assert "boom" in out and "耗时: 1.0s" in out

    def test_degraded_and_quality_warnings(self, capsys):
        task = _done_task(result={
            "writer": {"stats": {"empty_sections": ["简介", "总结"]}},
            "quality_gate": {"status": "accepted_with_warnings",
                             "generation_count": 3, "overall_score": 55,
                             "scores": {"depth": 50}},
        })
        args = _args(["x.md"])
        _run_module()._render_task_result(args, task, "t1")
        err = capsys.readouterr().err
        assert "2 个章节内容不足" in err
        assert "质量门控" in err and "3 轮重做" in err

    def test_report_file_notice(self, capsys):
        # report 文件位于 <项目根>/checkpoints/
        run = _run_module()
        ckpt = Path(run.__file__).parent / "checkpoints"
        report = ckpt / "report_t9.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}", encoding="utf-8")
        try:
            args = _args(["x.md", "--report"])
            run._render_task_result(args, _done_task(), "t9")
            assert "详细报告已保存" in capsys.readouterr().out
        finally:
            report.unlink(missing_ok=True)

    def test_fix_ascii_and_export_dispatch(self, tmp_path):
        run = _run_module()
        doc = tmp_path / "doc.md"
        doc.write_text("body", encoding="utf-8")
        args = _args(["x.md", "-o", str(doc), "--fix-ascii",
                      "--export", "html", "--export-output", "out.html"])
        with patch.object(run, "_run_ascii_fix") as fix, \
                patch.object(run, "_run_export") as exp:
            run._render_task_result(args, _done_task(), "t1")
        fix.assert_called_once_with(str(doc))
        exp.assert_called_once_with(str(doc), "html", "out.html")


# ─── _run_single_task ────────────────────────────

class TestRunSingleTask:
    def test_plan_preview_with_plan(self, capsys):
        run = _run_module()
        args = _args(["x.md", "--plan"])
        plan = MagicMock()
        with patch.object(run, "_resolve_pipeline_plan", return_value=(plan, True)), \
                patch("pipeline_core.scheduler.Scheduler.visualize",
                      return_value="PREVIEW"):
            assert run._run_single_task(args, MagicMock(), {}) is None
        assert "PREVIEW" in capsys.readouterr().out

    def test_plan_preview_legacy(self, capsys):
        run = _run_module()
        args = _args(["x.md", "--plan"])
        orch = MagicMock()
        orch.visualize_plan.return_value = "LEGACY-PREVIEW"
        with patch.object(run, "_resolve_pipeline_plan", return_value=(None, False)):
            run._run_single_task(args, orch, {})
        assert "LEGACY-PREVIEW" in capsys.readouterr().out
        orch.plan.assert_called_once()

    def test_plan_preview_legacy_error_exits(self):
        run = _run_module()
        args = _args(["x.md", "--plan"])
        orch = MagicMock()
        orch.plan.side_effect = RuntimeError("no plan")
        with patch.object(run, "_resolve_pipeline_plan", return_value=(None, False)), \
                pytest.raises(SystemExit) as ei:
            run._run_single_task(args, orch, {})
        assert ei.value.code == 1

    def test_dry_run_returns_none(self, capsys):
        run = _run_module()
        args = _args(["x.md", "--dry-run"])
        plan = MagicMock()
        plan.raw = {}
        orch = MagicMock()
        orch.registry.list_agent_names.return_value = ["a"]
        with patch.object(run, "_resolve_pipeline_plan", return_value=(plan, True)):
            assert run._run_single_task(args, orch, {}) is None
        orch.run_plan.assert_called_once()
        assert orch.run_plan.call_args.kwargs["wait"] is False
        assert "dry-run" in capsys.readouterr().out

    def test_output_injected_into_plan_raw(self):
        run = _run_module()
        args = _args(["x.md", "--dry-run", "-o", "out.md"])
        plan = MagicMock()
        plan.raw = {}
        orch = MagicMock()
        orch.registry.list_agent_names.return_value = ["a"]
        with patch.object(run, "_resolve_pipeline_plan", return_value=(plan, True)):
            run._run_single_task(args, orch, {})
        assert plan.raw["pipeline"]["output"] == "out.md"

    def test_plan_missing_exits(self):
        run = _run_module()
        args = _args(["x.md"])
        orch = MagicMock()
        orch.registry.list_agent_names.return_value = ["a"]
        with patch.object(run, "_resolve_pipeline_plan", return_value=(None, True)), \
                pytest.raises(SystemExit) as ei:
            run._run_single_task(args, orch, {})
        assert ei.value.code == 1

    def test_interrupted_poll_returns_none(self):
        run = _run_module()
        args = _args(["x.md"])
        plan = MagicMock()
        plan.raw = {}
        orch = MagicMock()
        orch.registry.list_agent_names.return_value = ["a"]
        with patch.object(run, "_resolve_pipeline_plan", return_value=(plan, True)), \
                patch.object(run, "_poll_task_progress", return_value=False):
            assert run._run_single_task(args, orch, {}) is None

    def test_legacy_path_runs_and_renders(self):
        run = _run_module()
        args = _args(["x.md", "--legacy"])
        orch = MagicMock()
        orch.registry.list_agent_names.return_value = ["a"]
        task = _done_task()
        orch.run.return_value = task
        with patch.object(run, "_resolve_pipeline_plan", return_value=(None, False)), \
                patch.object(run, "_poll_task_progress", return_value=True), \
                patch.object(run, "_render_task_result") as render:
            result = run._run_single_task(args, orch, {"k": "v"})
        assert result is task
        orch.run.assert_called_once()
        render.assert_called_once()


# ─── _run_daemon ────────────────────────────

class TestRunDaemon:
    def test_daemon_exits_on_interrupt(self):
        with patch("time.sleep", side_effect=KeyboardInterrupt):
            _run_module()._run_daemon(MagicMock())  # 不应抛异常


# ─── main() 分支 ────────────────────────────

class TestMainBranches:
    def test_check_with_errors_exits_1(self):
        with patch("sys.argv", ["run.py", "--check"]), \
                patch("pipeline_core.bootstrap.run_startup_check") as chk:
            chk.return_value = SimpleNamespace(has_errors=True,
                                               summary=lambda: "BAD")
            with pytest.raises(SystemExit) as ei:
                _run_module().main()
            assert ei.value.code == 1

    def test_mcp_mode(self):
        with patch("sys.argv", ["run.py", "--mcp"]), \
                patch("pipeline_core.mcp_server.run_mcp_server") as mcp:
            _run_module().main()
        mcp.assert_called_once()

    def test_recover_no_tasks(self):
        orch = MagicMock()
        orch.recover_tasks.return_value = []
        with patch("sys.argv", ["run.py", "--recover"]), \
                patch.object(_run_module(), "_get_orchestrator", return_value=orch), \
                pytest.raises(SystemExit) as ei:
            _run_module().main()
        assert ei.value.code == 0

    def test_recover_with_tasks(self, capsys):
        orch = MagicMock()
        orch.recover_tasks.return_value = [
            {"task_id": "t1", "pipeline_name": "docgen", "input_file": "a.md"},
            {"task_id": "t2", "pipeline_name": "docgen", "input_file": "b.md"},
        ]
        task = MagicMock()
        task.status.value = "running"
        orch.run_plan.side_effect = [task, RuntimeError("bad task")]
        with patch("sys.argv", ["run.py", "--recover"]), \
                patch.object(_run_module(), "_get_orchestrator", return_value=orch), \
                patch("pipeline_core.scheduler.Scheduler.parse") as parse:
            parse.return_value = MagicMock()
            _run_module().main()
        out = capsys.readouterr().out
        assert "恢复任务 t1" in out and "恢复失败" in out
        orch.shutdown.assert_called_once()

    def test_enhance_mode(self, capsys):
        run = _run_module()
        result = {"status": "ok", "duration": 1.5,
                  "stats": {"sections": 4, "enhanced": 3, "searched": 2,
                            "ascii_fixed": 1},
                  "output_path": "out/enhanced.md"}
        with patch("sys.argv", ["run.py", "doc.md", "--enhance",
                                "--enhance-output", "out", "--no-search",
                                "--fix-ascii", "--export", "html"]), \
                patch("pipeline_core.bootstrap.quick_check", return_value=True), \
                patch("pipeline_core.document_enhancer.DocumentEnhancer") as cls, \
                patch.object(run, "_run_ascii_fix") as fix, \
                patch.object(run, "_run_export") as exp:
            cls.return_value.enhance.return_value = result
            run.main()
        # --no-search 传递 with_search=False
        assert cls.return_value.enhance.call_args.kwargs["with_search"] is False
        fix.assert_called_once_with("out/enhanced.md")
        exp.assert_called_once()
        assert "文档增强完成" in capsys.readouterr().out

    def test_admin_bad_port_exits_1(self):
        run = _run_module()
        orch = MagicMock()
        orch.register_agents.return_value = ["a"]
        with patch("sys.argv", ["run.py", "--admin"]), \
                patch.object(run, "_get_orchestrator", return_value=orch), \
                patch.object(run, "_load_config",
                             return_value={"admin_api": {"port": "not-a-port"}}), \
                patch("pipeline_core.bootstrap.quick_check", return_value=True), \
                pytest.raises(SystemExit) as ei:
            run.main()
        assert ei.value.code == 1

    def test_admin_start_failure_exits_1(self):
        run = _run_module()
        orch = MagicMock()
        orch.register_agents.return_value = ["a"]
        orch.start_admin_api.return_value = False
        with patch("sys.argv", ["run.py", "--admin"]), \
                patch.object(run, "_get_orchestrator", return_value=orch), \
                patch.object(run, "_load_config", return_value={}), \
                patch("pipeline_core.bootstrap.quick_check", return_value=True), \
                pytest.raises(SystemExit) as ei:
            run.main()
        assert ei.value.code == 1

    def test_failed_task_maps_to_exit_code_1(self, tmp_path):
        run = _run_module()
        input_file = tmp_path / "in.md"
        input_file.write_text("# t", encoding="utf-8")
        orch = MagicMock()
        orch.register_agents.return_value = ["a"]
        orch.registry.list_agent_names.return_value = ["a"]
        task = _done_task(status=TaskStatus.FAILED, error="boom")
        orch.run.return_value = task
        with patch("sys.argv", ["run.py", str(input_file), "--legacy"]), \
                patch.object(run, "_get_orchestrator", return_value=orch), \
                patch.object(run, "_load_config", return_value={}), \
                patch("pipeline_core.bootstrap.quick_check", return_value=True), \
                patch("builtins.print"), \
                pytest.raises(SystemExit) as ei:
            run.main()
        assert ei.value.code == 1
        orch.shutdown.assert_called_once()

    def test_no_agents_loaded_returns_early(self, capsys):
        run = _run_module()
        orch = MagicMock()
        orch.register_agents.return_value = []
        with patch("sys.argv", ["run.py", "in.md"]), \
                patch.object(run, "_get_orchestrator", return_value=orch), \
                patch.object(run, "_load_config", return_value={}), \
                patch("pipeline_core.bootstrap.quick_check", return_value=True):
            run.main()
        assert "没有加载任何 Agent" in capsys.readouterr().out
        # 注意：此分支直接 return，未调用 shutdown（既有行为）
        orch.shutdown.assert_not_called()
