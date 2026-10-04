"""渲染层测试 — docpipeline/renderer.py + agents/renderer_agent.py

覆盖：
- Markdown 解析（标题层级 / 列表 / 围栏代码 / 引用 / 分隔线）
- docx 渲染：Word 标题样式落地（决定能否自动生成目录）
- pdf 渲染：中文保留、代码缩进保真
- 格式分发与不支持格式的降级
- Agent 契约：格式归一化、文件名解析、失败不中断流水线
- pipeline YAML 可被 scheduler 解析（防止声明与 Agent 不一致）

后端缺失时用 monkeypatch 强制 HAS_DOCX/HAS_PDF=False，
断言降级行为（返回 error 而非抛异常）。
"""
import sys
from pathlib import Path

import pytest

from docpipeline import renderer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE_MD = """# 季度技术报告

## 概述

本季度围绕异步编程能力做了系统性重构，核心目标是把检索、写作与渲染
三个环节串成可交付的流水线，并保证产出格式可用于打印与送审。

### 关键进展

- 完成 DAG 编排内核，支持断点续传与熔断保护
- 正文提取保留代码块换行与缩进
- 渲染层打通 docx 与 pdf 双路线

```python
async def render(doc: str) -> bytes:
    result = await pipeline.run(doc)
    return result.to_pdf()
```

> 说明：渲染是增强环节，失败不影响 Markdown 主产物。

---

## 后续计划

下季度补齐知识库摄入侧。
"""

needs_docx = pytest.mark.skipif(not renderer.HAS_DOCX, reason="python-docx 未安装")
needs_pdf = pytest.mark.skipif(not renderer.HAS_PDF, reason="reportlab 未安装")

# pymupdf 仅用于校验 pdf 文本层，是测试期依赖而非运行时依赖；
# 未安装时跳过这几个用例，而不是让整条 CI 变红。
try:
    import pymupdf  # noqa: F401
    HAS_PYMUPDF = True
except ImportError:      # pragma: no cover - 环境相关
    HAS_PYMUPDF = False
needs_pdftext = pytest.mark.skipif(not HAS_PYMUPDF, reason="pymupdf 未安装")


# ───────────────────────────── Markdown 解析 ─────────────────────────────

class TestParseMarkdown:
    def test_heading_levels(self):
        kinds = [k for k, _ in renderer.parse_markdown("# a\n## b\n### c")]
        assert kinds == ["h1", "h2", "h3"]

    def test_list_items(self):
        blocks = renderer.parse_markdown("- 第一项\n* 第二项\n1. 第三项")
        assert [k for k, _ in blocks] == ["li", "li", "li"]
        assert blocks[2][1] == "第三项"

    def test_fenced_code_preserved(self):
        blocks = renderer.parse_markdown("```python\nx = 1\n    y = 2\n```")
        assert blocks[0][0] == "code"
        assert "    y = 2" in blocks[0][1]

    def test_tilde_fence(self):
        blocks = renderer.parse_markdown("~~~\nplain\n~~~")
        assert blocks[0][0] == "code"

    def test_inline_code_stripped(self):
        assert renderer.parse_markdown("调用 `foo()` 完成")[0][1] == "调用 foo() 完成"

    def test_bold_stripped(self):
        assert renderer.parse_markdown("**重点**内容")[0][1] == "重点内容"

    def test_link_keeps_url(self):
        # 引用来源是可信度关键信息，URL 不能丢
        out = renderer.parse_markdown("见 [文档](https://x.com/a)")[0][1]
        assert "文档" in out and "https://x.com/a" in out

    def test_quote(self):
        assert renderer.parse_markdown("> 引用内容")[0][0] == "quote"

    def test_horizontal_rule(self):
        assert renderer.parse_markdown("---")[0][0] == "hr"

    def test_blank_lines_skipped(self):
        assert renderer.parse_markdown("a\n\n\nb") == [("p", "a"), ("p", "b")]

    def test_empty_input(self):
        assert renderer.parse_markdown("") == []

    def test_unclosed_fence_tolerated(self):
        blocks = renderer.parse_markdown("```py\nx=1")
        assert blocks[0][0] == "code"
        assert "x=1" in blocks[0][1]


# ─────────────────────────────── docx 渲染 ───────────────────────────────

@needs_docx
class TestRenderDocx:
    def _render(self, tmp_path, md=SAMPLE_MD, name="out.docx"):
        res = renderer.render_docx(md, tmp_path / name, title="季度技术报告")
        assert res["status"] == "ok", res
        return res, tmp_path / name

    def test_creates_file(self, tmp_path):
        res, path = self._render(tmp_path)
        assert path.exists()
        assert res["size"] > 0

    def test_uses_real_word_heading_styles(self, tmp_path):
        """落真正的 Heading 样式——Word 才能自动生成目录、支持导航。"""
        from docx import Document
        _, path = self._render(tmp_path)
        doc = Document(path)
        styles = {p.style.name for p in doc.paragraphs if p.text.strip()}
        assert "Title" in styles
        assert "Heading 1" in styles or "Heading 2" in styles

    def test_creates_parent_dir(self, tmp_path):
        _, path = self._render(tmp_path, name="nested/deep/out.docx")
        assert path.exists()

    def test_chinese_font_pinned_in_styles(self, tmp_path):
        """w:eastAsia 必须在样式层钉死，否则跨机打开回退宋体。"""
        import re
        import zipfile
        _, path = self._render(tmp_path)
        with zipfile.ZipFile(path) as z:
            styles_xml = z.read("word/styles.xml").decode("utf-8")
        assert "eastAsia" in styles_xml
        assert re.search(r'w:eastAsia="[^"]*[一-鿿][^"]*"', styles_xml)

    def test_list_bullets(self, tmp_path):
        from docx import Document
        _, path = self._render(tmp_path)
        doc = Document(path)
        assert any(p.style.name == "List Bullet" for p in doc.paragraphs)

    def test_empty_content_errors(self, tmp_path):
        assert renderer.render_docx("", tmp_path / "x.docx")["status"] == "error"

    def test_missing_backend_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(renderer, "HAS_DOCX", False)
        res = renderer.render_docx("x", tmp_path / "x.docx")
        assert res["status"] == "error"
        assert "python-docx" in res["message"]


# ──────────────────────────────── pdf 渲染 ────────────────────────────────

@needs_pdf
class TestRenderPdf:
    def _render(self, tmp_path, md=SAMPLE_MD, name="out.pdf"):
        res = renderer.render_pdf(md, tmp_path / name, title="季度技术报告")
        assert res["status"] == "ok", res
        return res, tmp_path / name

    def test_creates_file(self, tmp_path):
        res, path = self._render(tmp_path)
        assert path.exists()
        assert res["size"] > 0

    @needs_pdftext
    def test_chinese_fully_preserved(self, tmp_path):
        import re

        import pymupdf
        _, path = self._render(tmp_path)
        pdf = pymupdf.open(path)
        text = "".join(p.get_text() for p in pdf)
        cjk = re.compile(r"[\u4e00-\u9fff]")
        assert len(cjk.findall(text)) == len(cjk.findall(SAMPLE_MD))

    @needs_pdftext
    def test_no_mojibake(self, tmp_path):
        import pymupdf
        _, path = self._render(tmp_path)
        text = "".join(p.get_text() for p in pymupdf.open(path))
        assert "�" not in text

    @needs_pdftext
    def test_code_indent_preserved(self, tmp_path):
        """wordWrap="CJK" 会吞行首空格，缩进必须转 &nbsp; 才保得住。"""
        import pymupdf
        _, path = self._render(tmp_path)
        text = "".join(p.get_text() for p in pymupdf.open(path))
        assert "    result = await pipeline.run(doc)" in text

    def test_preserve_indent_helper(self):
        assert renderer._preserve_indent("    x") == "&nbsp;" * 4 + "x"
        assert renderer._preserve_indent("") == ""
        assert renderer._preserve_indent("a<b") == "a&lt;b"

    def test_empty_content_errors(self, tmp_path):
        assert renderer.render_pdf("", tmp_path / "x.pdf")["status"] == "error"

    def test_missing_backend_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setattr(renderer, "HAS_PDF", False)
        res = renderer.render_pdf("x", tmp_path / "x.pdf")
        assert res["status"] == "error"
        assert "reportlab" in res["message"]


# ─────────────────────────────── 统一入口 ───────────────────────────────

class TestRenderDispatch:
    def test_rejects_unknown_format(self, tmp_path):
        res = renderer.render("x", tmp_path / "x.rtf", fmt="rtf")
        assert res["status"] == "error"
        assert "rtf" in res["message"]

    def test_accepts_dot_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(renderer, "HAS_DOCX", False)
        # ".docx" 与 "docx" 等价，不应因前导点被拒
        res = renderer.render("x", tmp_path / "x.docx", fmt=".docx")
        assert "不支持的格式" not in res.get("message", "")

    def test_supported_formats_reflects_env(self):
        avail = renderer.supported_formats()
        assert set(avail) <= {"docx", "pdf"}

    def test_clean_inline_variants(self):
        assert renderer.clean_inline("`a` **b** [c](d)") == "a b c (d)"


# ─────────────────────────────── Agent 契约 ───────────────────────────────

def _make_agent(tmp_path, config=None):
    from agents.renderer_agent import RendererAgent
    from pipeline_core.registry import AgentMeta

    class _Bus:
        def subscribe(self, topic, handler=None):
            pass

        def publish(self, topic, sender, payload):
            pass

    class _Reg:
        pass

    meta = AgentMeta(name="renderer", version="1.0")
    cfg = {"formats": ["docx", "pdf"], "output_dir": str(tmp_path)}
    cfg.update(config or {})
    return RendererAgent("renderer", meta, cfg, _Bus(), _Reg())


needs_both = pytest.mark.skipif(
    not (renderer.HAS_DOCX and renderer.HAS_PDF), reason="需要 docx + pdf 后端")


@needs_both
class TestRendererAgent:
    def _msg(self, **payload):
        from pipeline_core.base_agent import Message
        payload.setdefault("task_id", "t1")
        return Message(topic="renderer.render", from_agent="test", payload=payload)

    def test_renders_both_formats(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(self._msg(content=SAMPLE_MD))
        assert res["status"] == "ok"
        assert res["formats"] == ["docx", "pdf"]
        assert Path(res["outputs"]["docx"]).exists()
        assert Path(res["outputs"]["pdf"]).exists()

    def test_basename_from_task_id(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(self._msg(content=SAMPLE_MD, task_id="job_9"))
        assert Path(res["outputs"]["docx"]).name == "job_9.docx"

    def test_basename_from_target_stem(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(self._msg(content=SAMPLE_MD,
                                      target="reports/我的文档.md"))
        assert Path(res["outputs"]["docx"]).name == "我的文档.docx"

    def test_task_id_sanitized(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(self._msg(content=SAMPLE_MD,
                                      task_id="../../evil/path"))
        assert "/" not in Path(res["outputs"]["docx"]).name
        assert ".." not in res["outputs"]["docx"]

    def test_empty_content_errors(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent.handle(self._msg(content=""))["status"] == "error"

    def test_unsupported_formats_filtered_by_env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(renderer, "HAS_PDF", False)
        agent = _make_agent(tmp_path, {"formats": ["docx", "pdf"]})
        res = agent.handle(self._msg(content=SAMPLE_MD))
        assert "pdf" not in res["formats"]
        assert res["status"] == "ok"

    def test_all_backends_missing_does_not_crash(self, tmp_path, monkeypatch):
        """后端全缺时如实报错，但应是结构化结果而非抛异常。"""
        monkeypatch.setattr(renderer, "HAS_DOCX", False)
        monkeypatch.setattr(renderer, "HAS_PDF", False)
        agent = _make_agent(tmp_path)
        res = agent.handle(self._msg(content=SAMPLE_MD))
        assert res["status"] == "error"
        assert res["formats"] == []

    def test_parse_formats_string(self, tmp_path):
        agent = _make_agent(tmp_path, {"formats": "docx, pdf"})
        assert agent._formats == ["docx", "pdf"]

    def test_parse_formats_empty_uses_env(self, tmp_path):
        agent = _make_agent(tmp_path, {"formats": []})
        assert agent._formats == renderer.supported_formats()

    def test_drops_unknown_format(self, tmp_path):
        agent = _make_agent(tmp_path, {"formats": ["docx", "rtf"]})
        assert agent._formats == ["docx"]

    def test_resolve_base_name_fallback(self):
        from agents.renderer_agent import RendererAgent
        assert RendererAgent._resolve_base_name({}, "") == "document"


# ────────────────────────── pipeline 声明一致性 ──────────────────────────

class TestPipelineDeclaration:
    def test_docgen_render_parses(self):
        from pipeline_core.scheduler import Scheduler
        sched = Scheduler()
        plan = sched.parse_file(
            str(ROOT / "pipelines" / "docgen-render.yaml"), verify_lock=False)
        assert plan.pipeline_name == "docgen-render"
        names = {n.agent_name for lv in plan.levels for n in lv}
        assert any(n.startswith("renderer") for n in names)

    def test_renderer_is_trusted_agent(self):
        from pathlib import Path

        from pipeline_core.agent_loader import declares_sandbox_trust
        p = Path(__file__).parent.parent / "agents" / "renderer_agent.py"
        assert declares_sandbox_trust(p)

    def test_lockfile_in_sync(self):
        """lockfile 必须与 YAML 一致，否则运行时会被拒绝执行。"""
        from pipeline_core.scheduler import Scheduler
        sched = Scheduler()
        sched.parse_file(
            str(ROOT / "pipelines" / "docgen-render.yaml"), verify_lock=True)
