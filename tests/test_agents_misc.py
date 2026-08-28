"""Layout / Checker Agent 单元测试 — 补齐覆盖率薄弱区（layout 27% / checker 38%）。"""
from unittest.mock import MagicMock, patch

from agents.checker import CheckerAgent
from agents.layout import LayoutAgent
from pipeline_core.base_agent import AgentMeta, Message


def _make_layout(tmp_path, dry_run=False) -> LayoutAgent:
    return LayoutAgent(
        "layout", AgentMeta(name="layout", version="3.1"),
        {"quiet": True, "dry_run": dry_run}, None, None)


def _make_checker(tmp_path) -> CheckerAgent:
    return CheckerAgent(
        "checker", AgentMeta(name="checker", version="1.1"),
        {"quiet": True}, None, None)


def _msg(payload: dict, topic: str) -> Message:
    return Message(topic=topic, payload=payload, from_agent="test")


# 简单可优化的 markdown（表格边框不规整，供 LayoutOptimizer 修复）
_TABLE_DOC = (
    "# 标题\n\n"
    "| 列A | 列B |\n"
    "| --- | --- |\n"
    "| 1 | 2 |\n"
)


class TestLayoutHandle:
    def test_content_mode(self, tmp_path):
        agent = _make_layout(tmp_path)
        res = agent.handle(_msg({"content": _TABLE_DOC, "target": "out.md"},
                                "layout.optimize"))
        assert res["status"] == "ok"
        assert "optimized" in res and res["content"]
        assert res["target"] == "out.md"

    def test_content_mode_dry_run_keeps_original(self, tmp_path):
        agent = _make_layout(tmp_path, dry_run=True)
        res = agent.handle(_msg({"content": _TABLE_DOC}, "layout.optimize"))
        assert res["status"] == "ok"
        assert res["optimized"] == _TABLE_DOC

    def test_no_content_no_target_error(self, tmp_path):
        agent = _make_layout(tmp_path)
        res = agent.handle(_msg({}, "layout.optimize"))
        assert res["status"] == "error"

    def test_optimizer_exception_keeps_original(self, tmp_path):
        agent = _make_layout(tmp_path)
        with patch("scripts.layout_optimizer.LayoutOptimizer",
                   side_effect=RuntimeError("opt down")):
            res = agent.handle(_msg({"content": _TABLE_DOC}, "layout.optimize"))
        assert res["status"] == "ok"
        assert res["optimized"] == _TABLE_DOC
        assert res["fixed"] == 0


class TestLayoutFile:
    def test_file_mode(self, tmp_path):
        agent = _make_layout(tmp_path)
        f = tmp_path / "doc.md"
        f.write_text(_TABLE_DOC, encoding="utf-8")
        res = agent.handle(_msg({"target": str(f)}, "layout.optimize"))
        assert res["status"] == "ok"
        assert res["target"] == str(f.resolve())
        assert "has_changes" in res

    def test_file_missing(self, tmp_path):
        agent = _make_layout(tmp_path)
        res = agent.handle(_msg({"target": str(tmp_path / "ghost.md")},
                                "layout.optimize"))
        assert res["status"] == "error"
        assert "文件不存在" in res["message"]

    def test_file_mode_optimizer_error(self, tmp_path):
        agent = _make_layout(tmp_path)
        f = tmp_path / "doc.md"
        f.write_text(_TABLE_DOC, encoding="utf-8")
        with patch("scripts.layout_optimizer.LayoutOptimizer",
                   side_effect=RuntimeError("boom")):
            res = agent._optimize_file(str(f))
        assert res["status"] == "error"


class TestLayoutCheckerDone:
    def test_handle_checker_done_with_content(self, tmp_path):
        agent = _make_layout(tmp_path)
        agent.handle_checker_done(_msg({"content": _TABLE_DOC, "target": "x.md"},
                                     "checker.done"))
        # bus 为 None 时 publish 无副作用，不抛异常即通过

    def test_handle_checker_done_file_mode(self, tmp_path):
        agent = _make_layout(tmp_path)
        f = tmp_path / "d.md"
        f.write_text(_TABLE_DOC, encoding="utf-8")
        agent.handle_checker_done(_msg({"target": str(f)}, "checker.done"))

    def test_handle_checker_done_empty_payload_noop(self, tmp_path):
        agent = _make_layout(tmp_path)
        agent.handle_checker_done(_msg({}, "checker.done"))  # 无 target/content → 直接返回


class TestCheckerHandle:
    def test_active_check_pass(self, tmp_path):
        agent = _make_checker(tmp_path)
        res = agent.handle(_msg({"content": "# 标题\n\n这是正常的文档内容。\n"},
                                "checker.check"))
        assert res["status"] in ("pass", "fail")
        assert "P0" in res and "total_issues" in res

    def test_no_target_no_content_error(self, tmp_path):
        agent = _make_checker(tmp_path)
        res = agent.handle(_msg({}, "checker.check"))
        assert res["status"] == "error"

    def test_p0_blocked_status(self, tmp_path):
        agent = _make_checker(tmp_path)
        agent._check = MagicMock(return_value={"P0": 2, "P1": 0, "P2": 0, "P3": 0})
        res = agent.handle(_msg({"content": "x"}, "checker.check"))
        assert res["status"] == "blocked"

    def test_writer_done_topic_routes_to_auto_check(self, tmp_path):
        agent = _make_checker(tmp_path)
        agent._on_writer_done = MagicMock()
        res = agent.handle(_msg({"content": "x"}, "writer.done"))
        assert res is None
        agent._on_writer_done.assert_called_once()


class TestCheckerCheck:
    def test_file_mode(self, tmp_path):
        agent = _make_checker(tmp_path)
        f = tmp_path / "doc.md"
        f.write_text("# 文档\n\n内容段落。\n", encoding="utf-8")
        res = agent._check(str(f))
        assert res["target"] == str(f)
        assert "P0" in res

    def test_file_missing(self, tmp_path):
        agent = _make_checker(tmp_path)
        res = agent._check(str(tmp_path / "ghost.md"))
        assert res["status"] == "error"

    def test_empty_content(self, tmp_path):
        agent = _make_checker(tmp_path)
        res = agent._check(None, content="")
        assert res["status"] == "error"

    def test_import_error_skips(self, tmp_path):
        agent = _make_checker(tmp_path)
        with patch.dict("sys.modules", {"scripts.markdown_checker": None}):
            res = agent._check(None, content="some content")
        assert res["status"] == "skip"

    def test_checker_exception(self, tmp_path):
        agent = _make_checker(tmp_path)
        with patch("scripts.markdown_checker.Checker") as mock_cls:
            mock_cls.return_value.run.side_effect = RuntimeError("boom")
            res = agent._check(None, content="some content")
        assert res["status"] == "error"


class TestCheckerOnWriterDone:
    def test_publishes_result(self, tmp_path):
        agent = _make_checker(tmp_path)
        agent._on_writer_done(_msg(
            {"content": "# T\n\n内容。\n", "task_id": "t1"}, "writer.done"))
        # bus=None 时 publish 无副作用；覆盖 _on_writer_done 主路径

    def test_empty_payload_returns_early(self, tmp_path):
        agent = _make_checker(tmp_path)
        agent._check = MagicMock()
        agent._on_writer_done(_msg({}, "writer.done"))
        agent._check.assert_not_called()
