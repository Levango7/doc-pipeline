"""渲染层可行性验证（spike，不入库）

目的：用管线真实产出的 Markdown，验证 docx / pdf 两条渲染路线的保真度，
为"升级为多格式文档系统"提供 go/no-go 依据。

跑法：python spike/render_spike.py
产物：spike/out/*.docx, *.pdf
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spike" / "out"


# ─────────────────────────── Markdown 解析（最小可用子集）───────────────────────────

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def clean_inline(text: str) -> str:
    """行内标记清洗：代码/粗体/链接 → 纯文本（去掉反引号等噪音）。"""
    text = INLINE_CODE.sub(r"\1", text)
    text = BOLD.sub(r"\1", text)
    text = LINK.sub(r"\1 (\2)", text)
    return text.strip()


def parse_markdown(md: str) -> list[tuple[str, str]]:
    """把 Markdown 拆成 (kind, text) 序列。kind ∈ h1/h2/h3/p/li/code/quote。"""
    blocks: list[tuple[str, str]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()

        # 代码块围栏
        if line.strip().startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过收尾围栏
            blocks.append(("code", "\n".join(buf)))
            continue

        if not line.strip():
            i += 1
            continue

        if line.startswith("### "):
            blocks.append(("h3", clean_inline(line[4:])))
        elif line.startswith("## "):
            blocks.append(("h2", clean_inline(line[3:])))
        elif line.startswith("# "):
            blocks.append(("h1", clean_inline(line[2:])))
        elif line.strip() in ("---", "***", "___"):
            blocks.append(("hr", ""))
        elif re.match(r"^\s*[-*+]\s+", line):
            blocks.append(("li", clean_inline(re.sub(r"^\s*[-*+]\s+", "", line))))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            blocks.append(("li", clean_inline(re.sub(r"^\s*\d+[.)]\s+", "", line))))
        elif line.lstrip().startswith(">"):
            blocks.append(("quote", clean_inline(line.lstrip().lstrip("> "))))
        else:
            blocks.append(("p", clean_inline(line)))
        i += 1
    return blocks


# ─────────────────────────────── docx 渲染（OOXML 路线）──────────────────────────────

CJK_FONT = "微软雅黑"
MONO_FONT = "Consolas"


def _set_cjk(run, font: str = CJK_FONT) -> None:
    """保留：供个别 run 需要覆盖字体时使用（如代码块用 Consolas）。

    常规文本的东亚字体在样式层统一设置（见 _setup_docx_styles），
    避免长文档里每个 run 都挂一份 w:rFonts。
    """
    from docx.oxml.ns import qn

    run.font.name = font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), font)
    rfonts.set(qn("w:hAnsi"), font)
    rfonts.set(qn("w:eastAsia"), font)


def _force_style_font(style, font: str = CJK_FONT) -> None:
    """在样式层钉死 ascii/hAnsi/eastAsia 三个字体属性。

    python-docx 的 style.font.name 只写 ascii/hAnsi，中文走 eastAsia 会回退宋体。
    直接操作 style.element.rPr.rFonts 一次设全，文档内所有该样式文本统一生效，
    比逐 run 设置干净得多。
    """
    from docx.oxml.ns import qn

    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), font)
    rfonts.set(qn("w:hAnsi"), font)
    rfonts.set(qn("w:eastAsia"), font)


def _setup_docx_styles(doc) -> None:
    """文档级样式：字号、行距、间距、中文字体，全部在样式层统一配置。"""
    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    normal.paragraph_format.line_spacing = 1.4
    normal.paragraph_format.space_after = Pt(6)
    _force_style_font(normal)

    for name, size in (("Heading 1", 16), ("Heading 2", 13), ("Title", 22)):
        try:
            st = doc.styles[name]
        except KeyError:
            continue
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor(0x11, 0x2B, 0x4A)
        st.paragraph_format.space_before = Pt(12)
        st.paragraph_format.space_after = Pt(7)
        _force_style_font(st)


def render_docx(blocks: list[tuple[str, str]], path: Path) -> dict:
    doc = Document()
    _setup_docx_styles(doc)

    stats = {"h1": 0, "h2": 0, "h3": 0, "p": 0, "li": 0, "code": 0, "quote": 0}

    for kind, text in blocks:
        if kind == "h1":
            # level=0 → Word "Title" 样式
            p = doc.add_heading("", level=0)
            p.add_run(text)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            stats["h1"] += 1

        elif kind == "h2":
            p = doc.add_heading("", level=1)
            p.add_run(text)
            stats["h2"] += 1

        elif kind == "h3":
            p = doc.add_heading("", level=2)
            p.add_run(text)
            stats["h3"] += 1

        elif kind == "li":
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(text)
            stats["li"] += 1

        elif kind == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            p.paragraph_format.line_spacing = 1.0
            r = p.add_run(text if text.strip() else " ")
            r.font.size = Pt(8.5)
            # 代码块逐 run 设等宽字体（Normal 样式是中文正文字体，这里必须覆盖）
            _set_cjk(r, MONO_FONT)
            stats["code"] += 1

        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            r = p.add_run(text)
            r.font.size = Pt(9.5)
            r.font.italic = True
            r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            stats["quote"] += 1

        elif kind == "hr":
            continue

        else:  # p
            p = doc.add_paragraph()
            p.add_run(text)
            stats["p"] += 1

    doc.save(path)
    return stats


# ────────────────────────── pdf 渲染（ReportLab 纯 Python 路线）──────────────────────────

def _register_cjk_family() -> None:
    """让 reportlab 5.x 能用内置 CID 中文字体 STSong-Light。

    reportlab 5 的字体查找有三道关卡，全都得过：

    1. ps2tt()：PostScript 名 → (family, bold, italic)，只查 13 个西文家族的固定表
    2. tt2ps()：family + bold + italic → 具体字体名，同样只有西文表
    3. pdfmetrics._typefaces：字体名 → 已注册 face，需显式 registerFont

    CID 字体不带粗体字重，所以粗体/斜体一律映射到常规体，
    标题层次改用字号 + 颜色 + 间距表达。
    """
    from reportlab.lib import fonts
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    ps_name = "STSong-Light"
    key = "stsong-light"

    pdfmetrics.registerFont(UnicodeCIDFont(ps_name))
    fonts._ps2tt_map.setdefault(key, (key, False, False))
    fonts._tt2ps_map.setdefault((key, False, False), ps_name)


def _pdf_styles() -> dict:
    """ReportLab 样式表。

    注意：STSong-Light 是 CID 字体，reportlab 无法推断它的 bold/italic 家族
    （ps2tt 抛 ValueError）。且从 Heading1/2/3 继承无效——父样式在构造期
    就已解析字体家族。因此所有中文样式一律从 BodyText（默认 Helvetica 家族）
    继承，再显式覆盖 fontName/bold/italic。
    """
    base = getSampleStyleSheet()
    no_infer = {"bold": False, "italic": False}
    body_cn = ParagraphStyle(
        "BodyCN",
        parent=base["BodyText"],
        fontName="STSong-Light",  # reportlab 内置 CJK 字体（CID），免装系统中文字体
        fontSize=10.5,
        leading=16,
        spaceAfter=7,
        wordWrap="CJK",  # 中文按字断行，不靠空格
        **no_infer,
    )
    return {
        "h1": ParagraphStyle(
            "H1CN", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=21, leading=27, alignment=1, spaceAfter=14,
            textColor="#112B4A", **no_infer,
        ),
        "h2": ParagraphStyle(
            "H2CN", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=15, leading=21, spaceBefore=12, spaceAfter=8,
            textColor="#112B4A", **no_infer,
        ),
        "h3": ParagraphStyle(
            "H3CN", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=12.5, leading=18, spaceBefore=9, spaceAfter=5, **no_infer,
        ),
        "p": body_cn,
        "li": ParagraphStyle(
            "Li", parent=body_cn, leftIndent=16, bulletIndent=6, spaceAfter=4,
        ),
        "quote": ParagraphStyle(
            "Quote", parent=body_cn, leftIndent=16, fontSize=9.5,
            textColor="#555555", **no_infer,
        ),
        "code": ParagraphStyle(
            "Code", parent=base["Code"], fontName="Courier",
            fontSize=7.5, leading=9.5, leftIndent=16,
            backColor="#F5F5F5", borderPadding=3,
        ),
    }


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _preserve_indent(line: str) -> str:
    """保住代码行前导缩进。

    ReportLab 的 Paragraph 在 wordWrap="CJK" 下会吞掉行首空格，
    导致 `    tasks = [...]` 的缩进全部消失——代码层级被抹平。
    解决办法：先把前导空格转成不换行空格&nbsp;，转义在之后做。
    """
    if not line:
        return ""
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    return "&nbsp;" * indent + _xml_escape(stripped)


def render_pdf(blocks: list[tuple[str, str]], path: Path) -> dict:
    _register_cjk_family()
    st = _pdf_styles()
    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="渲染层保真度验证",
    )
    flow = []
    for kind, text in blocks:
        if kind == "hr":
            flow.append(Spacer(1, 7))
            continue
        if kind == "li":
            flow.append(Paragraph(_xml_escape(text), st["li"], bulletText="•"))
        elif kind == "code":
            # reportlab Paragraph 不吃换行 → 逐行独立 Paragraph，保住代码排版
            code_lines = text.splitlines() or [""]
            for ln in code_lines:
                flow.append(
                    Paragraph(_preserve_indent(ln.rstrip()) or "&nbsp;", st["code"])
                )
        else:
            flow.append(Paragraph(_xml_escape(text), st[kind]))
    doc.build(flow)
    return {"blocks": len(flow)}


# ─────────────────────────────────────── 主流程 ───────────────────────────────────────

def main() -> int:
    src = ROOT / "output" / "smoke_20260830.md"
    if not src.exists():
        print(f"[!] 找不到输入样本：{src}")
        return 1

    md = src.read_text(encoding="utf-8")
    blocks = parse_markdown(md)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"输入      : {src.name}（{len(md):,} 字符）")
    kinds: dict[str, int] = {}
    for k, _ in blocks:
        kinds[k] = kinds.get(k, 0) + 1
    print(f"解析出块  : {len(blocks)} 个 → {kinds}")

    # 关键质量信号：正文里残留的"代码块塌陷"程度
    long_p = [t for k, t in blocks if k == "p" and len(t) > 1500]
    if long_p:
        print(
            f"[!] 检出 {len(long_p)} 个超长段落（>1500 字符），"
            f"最长 {max(len(t) for t in long_p):,} 字符 —— 正文提取未剥代码块换行"
        )

    t0 = time.perf_counter()
    dx = OUT / "spike_report.docx"
    st_docx = render_docx(blocks, dx)
    t_docx = time.perf_counter() - t0

    t0 = time.perf_counter()
    pf = OUT / "spike_report.pdf"
    st_pdf = render_pdf(blocks, pf)
    t_pdf = time.perf_counter() - t0

    print(f"\ndocx      : {dx.name}  {dx.stat().st_size:,} B  "
          f"{t_docx*1000:.0f} ms  {st_docx}")
    print(f"pdf       : {pf.name}  {pf.stat().st_size:,} B  "
          f"{t_pdf*1000:.0f} ms  {st_pdf}")
    print(f"\n产物目录  : {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())