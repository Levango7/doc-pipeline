"""摄入层测试 — docpipeline/ingest.py + agents/ingest_agent.py

覆盖：
- PDF 提取（用 PyMuPDF 动态生成样本，不依赖仓库里的产物文件）
- 中文无损 + 标题层级按字号推断
- 表格 → Markdown 表格
- 扫描件识别（无文本层的页必须被标 needs_ocr，不能静默返回空）
- 编码嗅探（GBK 中文不能乱码）
- 图片路径在 OCR 缺失时如实回报，不静默失败
- 后端探测必须**真的实例化**才算可用（本机 paddleocr 装着但
  PP-StructureV3 缺 paddlepaddle，只 import 会给出错误承诺）
- Agent 契约：批量部分失败不中断、产物落盘、路径穿越防护

PyMuPDF 缺失时相关用例跳过。
"""
import sys
from pathlib import Path

import pytest

from docpipeline import ingest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

needs_mupdf = pytest.mark.skipif(not ingest.HAS_PYMUPDF, reason="pymupdf 未安装")


def _find_cjk_font() -> str | None:
    """找一个可用的中文字体路径（生成中文 PDF 用）。

    PyMuPDF 默认字体 Helvetica 不含中文字形，直接 insert_text 会写成
    `······`。因此测试样本必须显式加载中文字体。找不到就跳过用例——
    典型是 Linux CI 容器里没装 CJK 字体。
    """
    import os
    for path in (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ):
        if os.path.exists(path):
            return path
    return None


needs_cjk_font = pytest.mark.skipif(
    _find_cjk_font() is None, reason="系统无中文字体，无法生成中文 PDF 样本")


@pytest.fixture
def sample_pdf(tmp_path):
    """动态生成一份含中文、多级标题与表格的 PDF。"""
    pymupdf = pytest.importorskip("pymupdf")
    font = _find_cjk_font()
    if font is None:
        pytest.skip("系统无中文字体")

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=500)
    page.insert_font(fontname="cn", fontfile=font)

    page.insert_text((60, 80), "季度经营报告", fontsize=22, fontname="cn")
    page.insert_text((60, 130), "第一部分 概述", fontsize=16, fontname="cn")
    page.insert_text((60, 165), "本季度整体收入增长百分之十二，"
                                "主要来自华东区域的贡献。",
                     fontsize=11, fontname="cn")
    page.insert_text((60, 200), "第二部分 明细", fontsize=16, fontname="cn")
    page.insert_text((60, 235), "下表列出了各区域的具体数据。",
                     fontsize=11, fontname="cn")

    # 2 行 3 列带框线表格（供 find_tables 识别）
    x0, y0, x1, y1 = 60, 270, 520, 370
    for i in range(1, 3):
        page.draw_line((x0, y0 + (y1 - y0) * i / 3),
                       (x1, y0 + (y1 - y0) * i / 3), width=1.0)
    for i in range(1, 3):
        page.draw_line((x0 + (x1 - x0) * i / 3, y0),
                       (x0 + (x1 - x0) * i / 3, y1), width=1.0)
    page.draw_line((x0, y0), (x1, y0), width=1.0)
    page.draw_line((x0, y1), (x1, y1), width=1.0)
    page.draw_line((x0, y0), (x0, y1), width=1.0)
    page.draw_line((x1, y0), (x1, y1), width=1.0)

    rows = [["区域", "收入", "占比"], ["华东", "1200", "45%"],
            ["华南", "900", "30%"]]
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            page.insert_text((x0 + (x1 - x0) * c / 3 + 8,
                              y0 + (y1 - y0) * (r + 0.7) / 3),
                             val, fontsize=11, fontname="cn")

    out = tmp_path / "sample.pdf"
    doc.save(str(out))
    doc.close()
    return out


@pytest.fixture
def ascii_pdf(tmp_path):
    """不含中文的 PDF，用于不依赖字体的用例（扫描件/加密）。"""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "plain ascii content for testing", fontsize=12)
    out = tmp_path / "ascii.pdf"
    doc.save(str(out))
    doc.close()
    return out


# ───────────────────────────── PDF 提取 ─────────────────────────────

@needs_mupdf
class TestPdfIngest:
    def test_extracts_chinese_without_mojibake(self, sample_pdf):
        res = ingest.ingest(sample_pdf)
        assert res["status"] == "ok", res
        assert "�" not in res["markdown"]
        assert "季度经营报告" in res["markdown"]

    def test_body_text_preserved(self, sample_pdf):
        res = ingest.ingest(sample_pdf)
        assert "收入增长" in res["markdown"]

    def test_headings_inferred_from_font_size(self, sample_pdf):
        """数字版 PDF 无语义标签，标题靠字号推断——这是核心能力。

        注意：PDF 提取出的行内可能含不换行空格(\\xa0)，
        因此断言用正则归一化空白而非直接比对。
        """
        import re
        res = ingest.ingest(sample_pdf)
        md = re.sub(r"\s+", " ", res["markdown"])
        assert "# 季度经营报告" in md      # 22pt → h1
        assert "## 第一部分 概述" in md    # 16pt → h2

    def test_page_count_reported(self, sample_pdf):
        assert ingest.ingest(sample_pdf)["pages"] == 1

    def test_table_detected(self, sample_pdf):
        res = ingest.ingest(sample_pdf)
        assert res["tables"] >= 1
        assert "| 区域 |" in res["markdown"] or "|区域|" in res["markdown"]

    def test_markdown_table_shape(self, sample_pdf):
        res = ingest.ingest(sample_pdf)
        lines = [ln for ln in res["markdown"].splitlines() if ln.startswith("|")]
        assert len(lines) >= 4           # 表头 + 分隔 + 2 行数据
        assert set(lines[1].replace("|", "").replace(" ", "")) <= {"-"}

    def test_no_overlong_blank_runs(self, sample_pdf):
        md = ingest.ingest(sample_pdf)["markdown"]
        assert "\n\n\n\n" not in md

    def test_scanned_pdf_flagged(self, tmp_path):
        """无文本层的 PDF 必须标 needs_ocr，不能静默返回空。"""
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        out = tmp_path / "scanned.pdf"
        doc.save(str(out))
        doc.close()

        res = ingest.ingest(out)
        assert res["status"] == "ok"
        assert res["scanned_pages"] == 1
        assert res["needs_ocr"] is True

    def test_encrypted_pdf_errors(self, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "secret content here for testing")
        out = tmp_path / "enc.pdf"
        doc.save(str(out), encryption=pymupdf.PDF_ENCRYPT_AES_256,
                 owner_pw="o", user_pw="u")
        doc.close()

        res = ingest.ingest(out)
        assert res["status"] == "error"
        assert "加密" in res["message"]


# ───────────────────────────── 文本摄入 ─────────────────────────────

class TestTextIngest:
    @pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "gbk", "gb18030"])
    def test_encoding_sniffing(self, tmp_path, enc):
        """中文编码嗅探：GBK/GB18030 不能变乱码。"""
        p = tmp_path / f"t_{enc}.md"
        p.write_bytes("季度报告：收入增长十二个百分点".encode(enc))
        res = ingest.ingest(p)
        assert res["status"] == "ok"
        assert "季度报告" in res["markdown"]
        assert "�" not in res["markdown"]

    def test_txt_suffix(self, tmp_path):
        p = tmp_path / "notes.txt"
        p.write_text("一些会议记录内容", encoding="utf-8")
        assert ingest.ingest(p)["status"] == "ok"

    def test_empty_file_errors(self, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("", encoding="utf-8")
        res = ingest.ingest(p)
        assert res["status"] == "error"

    def test_works_without_pymupdf(self, tmp_path, monkeypatch):
        """纯文本摄入不应依赖 pymupdf。"""
        monkeypatch.setattr(ingest, "HAS_PYMUPDF", False)
        p = tmp_path / "a.md"
        p.write_text("内容", encoding="utf-8")
        assert ingest.ingest(p)["status"] == "ok"

    def test_pdf_without_pymupdf_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest, "HAS_PYMUPDF", False)
        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF")
        res = ingest.ingest(p)
        assert res["status"] == "error"
        assert "pymupdf" in res["message"]


# ───────────────────────────── 错误路径 ─────────────────────────────

class TestErrorPaths:
    def test_missing_file(self, tmp_path):
        res = ingest.ingest(tmp_path / "nope.pdf")
        assert res["status"] == "error"
        assert "不存在" in res["message"]

    def test_directory_rejected(self, tmp_path):
        res = ingest.ingest(tmp_path)
        assert res["status"] == "error"

    def test_unsupported_suffix(self, tmp_path):
        p = tmp_path / "data.xlsx"
        p.write_bytes(b"PK")
        res = ingest.ingest(p)
        assert res["status"] == "error"
        assert "不支持" in res["message"]

    def test_image_without_ocr_reports_actionable_hint(self, tmp_path, monkeypatch):
        """图片路径在 OCR 缺失时必须说清装什么，而不是静默空结果。"""
        monkeypatch.setattr(ingest, "ocr_backend", lambda: None)
        p = tmp_path / "scan.png"
        p.write_bytes(b"\x89PNG\r\n")
        res = ingest.ingest(p)
        assert res["status"] == "error"
        assert res["needs_ocr"] is True
        assert "paddleocr" in res["message"] or "mineru" in res["message"]

    @pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg", ".bmp",
                                        ".tiff", ".webp"])
    def test_image_suffixes_all_reach_ocr(self, tmp_path, monkeypatch, suffix):
        monkeypatch.setattr(ingest, "ocr_backend", lambda: None)
        p = tmp_path / f"img{suffix}"
        p.write_bytes(b"x")
        assert ingest.ingest(p).get("needs_ocr") is True


# ───────────────────────────── 后端探测 ─────────────────────────────

class TestBackendDetection:
    def test_ocr_probe_does_not_trust_bare_import(self, monkeypatch):
        """核心回归：paddleocr 装着 ≠ PP-StructureV3 能用。

        本机实测：paddleocr 3.7.0 已装，但缺 paddlepaddle（3.14 无
        wheel）→ PP-StructureV3 实例化即抛错。只做 import 会误报可用，
        给调用方一个兑现不了的承诺。
        """
        import sys as _sys
        import types

        fake = types.ModuleType("paddleocr")

        class _Boom:
            def __init__(self, *a, **kw):
                raise RuntimeError("paddlepaddle is not installed")

        fake.PPStructureV3 = _Boom
        monkeypatch.setitem(_sys.modules, "paddleocr", fake)
        monkeypatch.setattr(ingest, "_OCR_BACKEND_CACHE", ingest._UNSET)

        assert ingest.ocr_backend() is None

    def test_ocr_probe_accepts_working_backend(self, monkeypatch):
        import sys as _sys
        import types

        fake = types.ModuleType("paddleocr")

        class _Ok:
            def __init__(self, *a, **kw):
                pass

        fake.PPStructureV3 = _Ok
        monkeypatch.setitem(_sys.modules, "paddleocr", fake)
        monkeypatch.setattr(ingest, "_OCR_BACKEND_CACHE", ingest._UNSET)

        assert ingest.ocr_backend() == "paddleocr"

    def test_probe_result_cached(self, monkeypatch):
        """探测结果进程内缓存：模型实例化是秒级开销，不能每次调用都重做。"""
        import sys as _sys
        import types

        calls = []

        fake = types.ModuleType("paddleocr")

        class _Counting:
            def __init__(self, *a, **kw):
                calls.append(1)

        fake.PPStructureV3 = _Counting
        monkeypatch.setitem(_sys.modules, "paddleocr", fake)
        monkeypatch.setattr(ingest, "_OCR_BACKEND_CACHE", ingest._UNSET)

        assert ingest.ocr_backend() == "paddleocr"
        assert ingest.ocr_backend() == "paddleocr"
        assert len(calls) == 1      # 第二次命中缓存，未再实例化

    def test_supported_suffixes_cover_pdf_image_text(self):
        assert ".pdf" in ingest.SUPPORTED_SUFFIXES
        assert ".png" in ingest.SUPPORTED_SUFFIXES
        assert ".md" in ingest.SUPPORTED_SUFFIXES

    def test_available_backends_lists_pymupdf(self):
        if ingest.HAS_PYMUPDF:
            assert "pymupdf" in ingest.available_backends()


# ───────────────────────────── 批量摄入 ─────────────────────────────

class TestIngestMany:
    def test_partial_failure_continues(self, tmp_path):
        good = tmp_path / "a.md"
        good.write_text("有效内容", encoding="utf-8")
        bad = tmp_path / "missing.md"

        res = ingest.ingest_many([good, bad])
        assert res["status"] == "ok"
        assert res["succeeded"] == 1
        assert res["failed"] == 1
        assert "有效内容" in res["markdown"]

    def test_all_failed_is_error(self, tmp_path):
        res = ingest.ingest_many([tmp_path / "x.md", tmp_path / "y.md"])
        assert res["status"] == "error"
        assert res["succeeded"] == 0

    def test_documents_carry_file_field(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("内容", encoding="utf-8")
        res = ingest.ingest_many([p])
        assert res["documents"][0]["file"] == str(p)

    def test_multiple_files_merged(self, tmp_path):
        a = tmp_path / "a.md"
        a.write_text("第一份", encoding="utf-8")
        b = tmp_path / "b.md"
        b.write_text("第二份", encoding="utf-8")
        res = ingest.ingest_many([a, b])
        assert res["succeeded"] == 2
        assert "第一份" in res["markdown"] and "第二份" in res["markdown"]

    def test_needs_ocr_propagated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest, "ocr_backend", lambda: None)
        img = tmp_path / "s.png"
        img.write_bytes(b"x")
        assert ingest.ingest_many([img])["needs_ocr"] is True


# ────────────────────────────── 表格转换 ──────────────────────────────

class TestRowsToMarkdown:
    def test_basic_shape(self):
        md = ingest._rows_to_markdown([["a", "b"], ["1", "2"]])
        lines = md.splitlines()
        assert lines[0] == "| a | b |"
        assert set(lines[1].replace("|", "").replace(" ", "")) == {"-"}
        assert lines[2] == "| 1 | 2 |"

    def test_ragged_row_padded(self):
        md = ingest._rows_to_markdown([["a", "b", "c"], ["1"]])
        assert md.splitlines()[2].count("|") == 4

    def test_pipe_escaped(self):
        md = ingest._rows_to_markdown([["a|b", "c"], ["1", "2"]])
        assert "\\|" in md

    def test_none_cell_becomes_empty(self):
        md = ingest._rows_to_markdown([["a", "b"], [None, "2"]])
        assert md.splitlines()[2].startswith("| ")


# ────────────────────────────── Agent 契约 ──────────────────────────────

def _make_agent(tmp_path, config=None):
    from agents.ingest_agent import IngestAgent
    from pipeline_core.registry import AgentMeta

    class _Bus:
        def subscribe(self, topic, handler=None):
            pass

        def publish(self, topic, sender, payload):
            pass

    class _Reg:
        pass

    meta = AgentMeta(name="ingest", version="1.0")
    cfg = {"output_dir": str(tmp_path / "ingested")}
    cfg.update(config or {})
    return IngestAgent("ingest", meta, cfg, _Bus(), _Reg())


def _msg(**payload):
    from pipeline_core.base_agent import Message
    payload.setdefault("task_id", "t1")
    return Message(topic="ingest.input", from_agent="test", payload=payload)


class TestIngestAgent:
    def test_single_file_param(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("资料内容", encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(f)))
        assert res["status"] == "ok"
        assert res["succeeded"] == 1

    def test_files_list_param(self, tmp_path):
        files = []
        for i in range(3):
            p = tmp_path / f"{i}.md"
            p.write_text(f"资料{i}", encoding="utf-8")
            files.append(str(p))
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(files=files))
        assert res["succeeded"] == 3

    def test_paths_alias_supported(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("内容", encoding="utf-8")
        agent = _make_agent(tmp_path)
        assert agent.handle(_msg(paths=[str(p)]))["succeeded"] == 1

    def test_duplicate_paths_deduped(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("内容", encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(files=[str(p), str(p)]))
        assert res["succeeded"] == 1

    def test_no_files_is_a_honest_skip_not_success(self, tmp_path):
        """没有语料 = 无事可做。

        标 skipped 而不是 error：legacy `run()` 会给每个已注册 Agent 都发一次
        RPC，摄入没有语料是常态而非错误；但也不能记成 success——报表必须看得出
        这一格什么都没做。
        """
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg())
        assert res["status"] == "skipped"
        assert "未指定待摄入文件" in res["message"]

    def test_artifact_written(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("资料正文", encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(p), task_id="job42"))
        assert res["artifact"]
        content = Path(res["artifact"]).read_text(encoding="utf-8")
        assert "资料正文" in content
        assert Path(res["artifact"]).name == "job42.md"

    def test_artifact_name_sanitized(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("内容", encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(p), task_id="../../evil/x"))
        name = Path(res["artifact"]).name
        assert ".." not in name
        assert "/" not in name

    def test_partial_failure_reported(self, tmp_path):
        good = tmp_path / "a.md"
        good.write_text("有效", encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(files=[str(good), str(tmp_path / "no.md")]))
        assert res["status"] == "ok"
        assert res["failed"] == 1
        assert res["failures"][0]["file"].endswith("no.md")

    def test_ocr_hint_when_needed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest, "ocr_backend", lambda: None)
        img = tmp_path / "scan.png"
        img.write_bytes(b"x")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(files=[str(img)]))
        assert res.get("ocr_hint")
        assert "paddle" in res["ocr_hint"] or "mineru" in res["ocr_hint"]

    def test_documents_summary_shape(self, tmp_path):
        p = tmp_path / "a.md"
        p.write_text("内容", encoding="utf-8")
        agent = _make_agent(tmp_path)
        res = agent.handle(_msg(file=str(p)))
        doc = res["documents"][0]
        assert set(doc) >= {"file", "backend", "chars", "message"}

    def test_get_info_reports_backends(self, tmp_path):
        agent = _make_agent(tmp_path)
        info = agent.get_info()
        assert "backends" in info
        assert ".pdf" in info["supported"]

    def test_on_snapshot(self, tmp_path):
        agent = _make_agent(tmp_path)
        snap = agent.on_snapshot()
        assert "output_dir" in snap and "ocr_enabled" in snap

    def test_ingest_agent_is_trusted(self):
        from pathlib import Path

        from pipeline_core.agent_loader import declares_sandbox_trust
        p = Path(__file__).parent.parent / "agents" / "ingest_agent.py"
        assert declares_sandbox_trust(p)
