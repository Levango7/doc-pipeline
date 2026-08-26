"""DocumentEnhancer 回归：原子写、fence 感知清洗、LLM 失败回退不清洗"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pipeline_core.document_enhancer as de_module
from pipeline_core.document_enhancer import DocumentEnhancer


def _make_enhancer():
    enh = DocumentEnhancer.__new__(DocumentEnhancer)
    enh._stats = {"sections": 0, "enhanced": 0, "searched": 0,
                  "ascii_fixed": 0, "fake_headings_removed": 0}
    return enh


class _FailRouter:
    def chat(self, *args, **kwargs):
        raise RuntimeError("llm down")


class _FixedRouter:
    def __init__(self, output):
        self.output = output

    def chat(self, *args, **kwargs):
        return self.output, "mock"


class _NoSearch:
    def is_available(self):
        return False


# ─── 原子写 ────────────────────────────

class TestAtomicWrite:

    def test_atomic_write_interrupt_keeps_old_file(self, tmp_path, monkeypatch):
        """写入中断时旧 _enhanced.md 保持完好且无临时文件残留"""
        enh = _make_enhancer()
        inp = tmp_path / "doc.md"
        inp.write_text("# T\n\n## S\n\nshort\n", encoding="utf-8")
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        old_out = out_dir / "doc_enhanced.md"
        old_out.write_text("OLD ENHANCED CONTENT", encoding="utf-8")

        real_replace = os.replace

        def flaky_replace(src, dst):
            if Path(str(dst)) == old_out:
                raise OSError("simulated interruption")
            return real_replace(src, dst)

        monkeypatch.setattr(de_module.os, "replace", flaky_replace)

        with pytest.raises(OSError):
            enh.enhance(str(inp), output_dir=str(out_dir))

        assert old_out.read_text(encoding="utf-8") == "OLD ENHANCED CONTENT"
        leftovers = [p for p in out_dir.iterdir() if p.name != "doc_enhanced.md"]
        assert leftovers == []

    def test_atomic_write_success_no_temp_leftovers(self, tmp_path):
        """正常写入后目标内容正确且无临时文件残留"""
        enh = _make_enhancer()
        inp = tmp_path / "doc.md"
        inp.write_text("# T\n\n## S\n\nshort\n", encoding="utf-8")
        out_dir = tmp_path / "out"

        result = enh.enhance(str(inp), output_dir=str(out_dir))

        assert result["status"] == "success"
        written = (out_dir / "doc_enhanced.md").read_text(encoding="utf-8")
        assert "## S" in written
        assert "short" in written
        leftovers = [p for p in out_dir.iterdir() if p.name != "doc_enhanced.md"]
        assert leftovers == []


# ─── fence 感知清洗 ────────────────────────────

class TestFenceAwareCleaning:

    def test_clean_preserves_hash_inside_code_fence(self, tmp_path):
        """代码 fence 内以 ## 开头的行不被误删，fence 外虚假标题仍被清理"""
        enh = _make_enhancer()
        text = (
            "para one\n"
            "```python\n"
            "## not a heading comment\n"
            "x = 1\n"
            "```\n"
            "## Fake Heading\n"
            "para two\n"
        )
        cleaned = enh._clean_llm_output(text)
        assert "## not a heading comment" in cleaned
        assert "x = 1" in cleaned
        assert "para one" in cleaned
        assert "## Fake Heading" not in cleaned

    def test_unclosed_fence_preserves_rest(self, tmp_path):
        """未闭合 fence 后的内容一律保留（宁可保留不可误删）"""
        enh = _make_enhancer()
        text = "intro\n```bash\n## looks like heading\nrun --help\n"
        cleaned = enh._clean_llm_output(text)
        assert "## looks like heading" in cleaned
        assert "run --help" in cleaned

    def test_clean_still_removes_artifacts_outside_fence(self):
        """fence 外的元描述 artifacts 仍被清理"""
        enh = _make_enhancer()
        text = "以下是增强后的内容\nreal paragraph\n"
        cleaned = enh._clean_llm_output(text)
        assert "以下是增强后的内容" not in cleaned
        assert "real paragraph" in cleaned


# ─── 主路径 LLM 失败回退一致性 ────────────────────────────

class TestMainPathFallbackConsistency:

    BODY = (
        "This section has plenty of real technical content,\n"
        "well above the fifty character minimum for enhancement.\n"
        "## LLM Generated Heading\n"
        "tail content line.\n"
    )

    def test_llm_fallback_returns_original_uncleaned(self, tmp_path):
        """主路径 LLM 失败回退原文后不再被清洗（与分块路径一致）"""
        enh = _make_enhancer()
        enh._llm_router = _FailRouter()
        enh._search_mgr = _NoSearch()

        out = enh._enhance_section("## Title", self.BODY, with_search=False, max_results=3)

        assert out == self.BODY
        assert "## LLM Generated Heading" in out

    def test_clean_guard_reverts_on_excessive_removal(self, tmp_path):
        """清洗移除超过阈值时放弃清洗，使用未清洗的 LLM 输出"""
        enh = _make_enhancer()
        enh._search_mgr = _NoSearch()
        body = "Real body content long enough for enhancement path.\n" * 3
        raw = "kept paragraph stays here\n" + "".join(
            f"## junk heading {i}\n" for i in range(40)
        )
        enh._llm_router = _FixedRouter(raw)

        out = enh._enhance_section("## Title", body, with_search=False, max_results=3)

        assert out == raw

    def test_clean_guard_reverts_when_cleaned_empty(self, tmp_path):
        """清洗结果为空时放弃清洗"""
        enh = _make_enhancer()
        enh._search_mgr = _NoSearch()
        body = "Real body content long enough for enhancement path.\n" * 3
        raw = "## only fake headings\n## another one\n"
        enh._llm_router = _FixedRouter(raw)

        out = enh._enhance_section("## Title", body, with_search=False, max_results=3)

        assert out == raw

    def test_normal_cleaning_still_applies_within_threshold(self, tmp_path):
        """正常范围内的清洗仍然生效"""
        enh = _make_enhancer()
        enh._search_mgr = _NoSearch()
        body = "Real body content long enough for enhancement path.\n" * 3
        raw = ("good line one\n" + "## Fake\n" +
               "good line two\n" + "good line three\n" + "good line four\n")
        enh._llm_router = _FixedRouter(raw)

        out = enh._enhance_section("## Title", body, with_search=False, max_results=3)

        assert "## Fake" not in out
        assert "good line one" in out
