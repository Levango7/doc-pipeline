"""KnowledgeBase Agent 测试 — agents/knowledge_base_agent.py

覆盖：
- 动作分发（显式 action / 按键推断）
- 索引：inline content、单文件、多文件、部分失败不中断
- **与摄入层的组合**：PDF 经 ingest 转 Markdown 后入库（这是本 Agent 的核心价值）
- 扫描件需 OCR 时如实回报，不静默跳过
- 检索：命中、top_k、无结果、嵌入器不一致时的结构化报错
- 惰性建库（Agent 加载时不创建数据库文件）
- on_stop 关闭连接

不触发模型下载：统一用 hash 嵌入器。
"""
import sys
from pathlib import Path

import pytest

from agents.knowledge_base_agent import KnowledgeBaseAgent
from pipeline_core.registry import AgentMeta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOC_ASYNC = """# 异步编程

## 事件循环

事件循环负责在协程之间切换执行权，await 时挂起并交还控制权。

## 并发控制

asyncio.gather 并发执行多个协程，Semaphore 限制并发度。
"""

DOC_DB = """# 数据库优化

## 索引设计

B+ 树索引适合范围查询，组合索引遵循最左前缀原则。
"""


class _Bus:
    def __init__(self):
        self.published: list[tuple] = []

    def subscribe(self, topic, handler=None):
        pass

    def publish(self, topic, sender, payload):
        self.published.append((topic, payload))


class _Reg:
    pass


def _make_agent(tmp_path, config=None):
    meta = AgentMeta(name="knowledge_base", version="1.0")
    cfg = {"db_path": str(tmp_path / "kb.db"), "embedder": "hash"}
    cfg.update(config or {})
    bus = _Bus()
    agent = KnowledgeBaseAgent("knowledge_base", meta, cfg, bus, _Reg())
    agent._test_bus = bus          # type: ignore[attr-defined]
    return agent


def _msg(**payload):
    from pipeline_core.base_agent import Message
    payload.setdefault("task_id", "t1")
    return Message(topic="kb.input", from_agent="test", payload=payload)


# ─────────────────────────────── 动作分发 ───────────────────────────────

class TestActionDispatch:
    def test_infers_index_from_content(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent.handle(_msg(content=DOC_ASYNC))["status"] == "ok"

    def test_infers_search_from_query(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.handle(_msg(content=DOC_ASYNC))
        assert agent.handle(_msg(query="事件循环"))["status"] == "ok"

    def test_explicit_action_index(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent.handle(_msg(action="index", content=DOC_ASYNC))["status"] == "ok"

    def test_explicit_action_search(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.handle(_msg(action="index", content=DOC_ASYNC))
        assert agent.handle(_msg(action="search", query="协程"))["status"] == "ok"

    def test_unknown_action_errors(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(action="delete_everything"))
        assert res["status"] == "error"
        assert "未知动作" in res["message"]

    def test_search_publishes_results_topic(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.handle(_msg(content=DOC_ASYNC))
        agent.handle(_msg(query="事件循环"))
        topics = [t for t, _ in agent._test_bus.published]      # type: ignore[attr-defined]
        assert "kb.results" in topics

    def test_index_publishes_indexed_topic(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.handle(_msg(content=DOC_ASYNC))
        topics = [t for t, _ in agent._test_bus.published]      # type: ignore[attr-defined]
        assert "kb.indexed" in topics


# ──────────────────────────────── 索引 ────────────────────────────────

class TestIndex:
    def test_inline_content(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(content=DOC_ASYNC, source="a.md",
                                title="异步编程"))
        assert res["indexed"] == 1
        assert res["chunks"] >= 2

    def test_no_input_errors(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(action="index"))
        assert res["status"] == "error"
        assert "未指定" in res["message"]

    def test_file_indexing(self, tmp_path):
        p = tmp_path / "note.md"
        p.write_text(DOC_DB, encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(p)))
        assert res["status"] == "ok"
        assert res["indexed"] == 1

    def test_multiple_files(self, tmp_path):
        files = []
        for i, doc in enumerate([DOC_ASYNC, DOC_DB]):
            p = tmp_path / f"d{i}.md"
            p.write_text(doc, encoding="utf-8")
            files.append(str(p))
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(files=files))
        assert res["indexed"] == 2

    def test_partial_failure_continues(self, tmp_path):
        good = tmp_path / "ok.md"
        good.write_text(DOC_ASYNC, encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(files=[str(good), str(tmp_path / "no.md")]))
        assert res["status"] == "ok"
        assert res["indexed"] == 1
        assert res["failed"] == 1

    def test_missing_file_reported(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(tmp_path / "nope.md")))
        assert res["status"] == "error"
        assert "不存在" in res["documents"][0]["message"]

    def test_gbk_file(self, tmp_path):
        p = tmp_path / "gbk.md"
        p.write_bytes("# 中文\n\nGBK 编码的内容。".encode("gbk"))
        agent = _make_agent(tmp_path)
        assert agent.handle(_msg(file=str(p)))["indexed"] == 1

    def test_same_source_reindex_replaces(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text(DOC_ASYNC, encoding="utf-8")
        agent = _make_agent(tmp_path)
        first = agent.handle(_msg(file=str(p)))["stats"]["chunks"]

        p.write_text("# 新版\n\n换了内容。", encoding="utf-8")
        res = agent.handle(_msg(file=str(p)))
        assert res["stats"]["documents"] == 1
        assert res["stats"]["chunks"] < first

    def test_stats_returned(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(content=DOC_ASYNC))
        assert res["stats"]["documents"] == 1
        assert res["stats"]["chunks"] >= 2


# ─────────────────────── 与摄入层的组合（核心价值）───────────────────────

class TestIngestComposition:
    """PDF/图片经 ingest 层转 Markdown 后入库 —— 扫描件内容也能被检索到。"""

    def _make_pdf(self, tmp_path) -> Path | None:
        pymupdf = pytest.importorskip("pymupdf")
        import os
        font = next((f for f in (
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ) if os.path.exists(f)), None)
        if font is None:
            return None

        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_font(fontname="cn", fontfile=font)
        page.insert_text((60, 90), "季度经营报告", fontsize=22, fontname="cn")
        page.insert_text((60, 140), "本季度收入增长百分之十二，"
                                    "主要来自订阅业务。", fontsize=12,
                         fontname="cn")
        out = tmp_path / "report.pdf"
        doc.save(str(out))
        doc.close()
        return out

    def test_pdf_indexed_via_ingest(self, tmp_path):
        pdf = self._make_pdf(tmp_path)
        if pdf is None:
            pytest.skip("系统无中文字体")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(pdf)))
        assert res["status"] == "ok", res
        assert res["documents"][0]["via"] == "ingest:pymupdf"

    def test_pdf_content_searchable(self, tmp_path):
        """端到端：PDF 里的内容要能被检索到。"""
        pdf = self._make_pdf(tmp_path)
        if pdf is None:
            pytest.skip("系统无中文字体")
        agent = _make_agent(tmp_path)
        agent.handle(_msg(file=str(pdf)))

        res = agent.handle(_msg(query="收入增长"))
        assert res["status"] == "ok"
        assert res["hits"] >= 1
        assert "收入" in res["results"][0]["content"]

    def test_ingest_layer_can_be_disabled(self, tmp_path):
        pdf = self._make_pdf(tmp_path)
        if pdf is None:
            pytest.skip("系统无中文字体")
        agent = _make_agent(tmp_path, {"use_ingest_layer": False})
        res = agent.handle(_msg(file=str(pdf)))
        # 关掉摄入层后 PDF 直读为乱码文本 —— 能入库但内容无意义，
        # 这里只断言"不再经过 ingest"
        assert res["documents"][0].get("via") != "ingest:pymupdf"

    def test_image_without_ocr_reports_hint(self, tmp_path, monkeypatch):
        """图片需 OCR 时必须如实回报，不能静默跳过。"""
        from pipeline_core import ingest as ing
        monkeypatch.setattr(ing, "ocr_backend", lambda: None)
        img = tmp_path / "scan.png"
        img.write_bytes(b"\x89PNG\r\n")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(img)))
        assert res["status"] == "error"
        doc = res["documents"][0]
        assert doc.get("needs_ocr") is True


# ──────────────────────────────── 检索 ────────────────────────────────

class TestSearch:
    def _seeded(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.handle(_msg(content=DOC_ASYNC, source="a.md", title="异步编程"))
        agent.handle(_msg(content=DOC_DB, source="b.md", title="数据库优化"))
        return agent

    def test_routes_to_correct_document(self, tmp_path):
        agent = self._seeded(tmp_path)
        res = agent.handle(_msg(query="组合索引最左前缀"))
        assert res["results"][0]["title"] == "数据库优化"

    def test_empty_query_errors(self, tmp_path):
        agent = _make_agent(tmp_path)
        assert agent.handle(_msg(query=""))["status"] == "error"

    def test_empty_kb_ok(self, tmp_path):
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(query="任意"))
        assert res["status"] == "ok"
        assert res["hits"] == 0

    def test_top_k_respected(self, tmp_path):
        agent = self._seeded(tmp_path)
        res = agent.handle(_msg(query="索引 协程", top_k=1))
        assert len(res["results"]) <= 1

    def test_query_echoed(self, tmp_path):
        agent = self._seeded(tmp_path)
        assert agent.handle(_msg(query="索引"))["query"] == "索引"

    def test_max_per_doc(self, tmp_path):
        agent = self._seeded(tmp_path)
        res = agent.handle(_msg(query="索引 协程", top_k=6, max_per_doc=1))
        titles = [r["title"] for r in res["results"]]
        assert len(titles) == len(set(titles))

    def test_source_prefix(self, tmp_path):
        agent = self._seeded(tmp_path)
        res = agent.handle(_msg(query="索引", source_prefix="a."))
        assert all(r["source"].startswith("a.") for r in res["results"])

    def test_embedder_mismatch_structured_error(self, tmp_path):
        # 播种用默认 1024 维（该实例只需产生副作用，无需保留引用）
        self._seeded(tmp_path)
        # 换维度 = 换向量空间
        other = _make_agent(tmp_path, {"embedder": "hash", "dim": 256})
        res = other.handle(_msg(query="索引"))
        assert res["status"] == "error"
        assert "向量空间" in res["message"]
        # 报错必须同时说明"库里是什么"和"当前是什么"
        assert res["stored_embedders"]
        assert res["embedder"]


# ─────────────────────────────── 生命周期 ───────────────────────────────

class TestLifecycle:
    def test_lazy_db_creation(self, tmp_path):
        """Agent 加载不应创建数据库文件（惰性建库）。"""
        db = tmp_path / "lazy.db"
        _make_agent(tmp_path, {"db_path": str(db)})
        assert not db.exists()

    def test_db_created_on_first_use(self, tmp_path):
        db = tmp_path / "lazy.db"
        agent = _make_agent(tmp_path, {"db_path": str(db)})
        agent.handle(_msg(content=DOC_ASYNC))
        assert db.exists()

    def test_on_stop_closes(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.handle(_msg(content=DOC_ASYNC))
        assert agent._kb is not None
        agent.on_stop()
        assert agent._kb is None

    def test_on_stop_without_kb(self, tmp_path):
        _make_agent(tmp_path).on_stop()      # 不应抛异常

    def test_get_info(self, tmp_path):
        agent = _make_agent(tmp_path)
        info = agent.get_info()
        assert info["requested_embedder"] == "hash"
        assert "candidate_embedders" in info
        assert info["active_embedder"] is None      # 尚未建库
        agent.handle(_msg(content=DOC_ASYNC))
        assert agent.get_info()["active_embedder"].startswith("hash")

    def test_on_snapshot(self, tmp_path):
        snap = _make_agent(tmp_path).on_snapshot()
        assert "db_path" in snap and "embedder" in snap

    def test_is_trusted_agent(self):
        from pipeline_core.agent_loader import AgentLoader
        assert "knowledge_base_agent" in AgentLoader._TRUSTED_AGENTS

    def test_persists_across_agent_instances(self, tmp_path):
        a1 = _make_agent(tmp_path)
        a1.handle(_msg(content=DOC_ASYNC))
        a1.on_stop()

        a2 = _make_agent(tmp_path)
        res = a2.handle(_msg(query="事件循环"))
        assert res["hits"] >= 1
