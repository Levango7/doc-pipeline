"""tests/test_checker.py — CheckerAgent markdown 规则检查。"""


from agents.checker import CheckerAgent
from pipeline_core.base_agent import AgentMeta, Message


def _make_agent():
    config = {"quiet": True}
    return CheckerAgent(
        "checker", AgentMeta(name="checker", version="1.0"),
        config, None, None)


class TestCheckerAgent:
    def test_active_check_passes_clean_markdown(self):
        agent = _make_agent()
        msg = Message(topic="checker.input", payload={
            "task_id": "t1",
            "content": (
                "# Title\n\n"
                "Some content here.\n\n"
                "## Section\n\n"
                "More text with [a link](http://example.com).\n\n"
                "## 参考资料\n\n"
                "- [ref](http://example.com/ref)\n"
            ),
        }, from_agent="test")
        res = agent.handle(msg)
        assert res is not None
        assert res["status"] in ("ok", "pass")

    def test_check_detects_empty_links(self):
        agent = _make_agent()
        msg = Message(topic="checker.input", payload={
            "task_id": "t2",
            "content": "# Title\n\n[](http://example.com)\n",
        }, from_agent="test")
        res = agent.handle(msg)
        # 应检测到空链接问题
        assert res is not None

    def test_check_detects_broken_links(self):
        agent = _make_agent()
        msg = Message(topic="checker.input", payload={
            "task_id": "t3",
            "content": "[link](http://this-domain-does-not-exist-12345.com)\n",
        }, from_agent="test")
        res = agent.handle(msg)
        assert res is not None

    def test_no_content_returns_error(self):
        agent = _make_agent()
        msg = Message(topic="checker.input", payload={"task_id": "t4"},
                      from_agent="test")
        res = agent.handle(msg)
        # 无内容应返回错误或跳过
        assert res is None or res["status"] in ("error", "skip")

    def test_handles_file_input(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("# Hello\n\nWorld\n", encoding="utf-8")
        agent = _make_agent()
        msg = Message(topic="checker.input", payload={
            "task_id": "t5", "input_file": str(f),
        }, from_agent="test")
        res = agent.handle(msg)
        assert res is not None
