"""渲染层：Markdown → docx / pdf / xlsx / pptx。

设计依据（见 spike/README.md 的可行性验证结论，GO）：
- **docx 走 OOXML 路线**（python-docx）：落真正的 Word 标题样式，
  Word 能自动生成目录、导航窗格可跳转、用户可二次编辑 → "活文档"
- **pdf 走 ReportLab 路线**：定版归档、可打印送审
- **xlsx / pptx 走结构化子集**（openpyxl / python-pptx）：不是全量转换器——
  表格落工作表 / 幻灯片真表，正文落要点；截断等降级在返回值里如实回报
- 四条路线互补而非替代

关键实现约束（踩坑记录，勿轻易改动）：
1. python-docx 的 `run.font.name` 只写 w:ascii/w:hAnsi，中文走 w:eastAsia，
   缺省会回退宋体 → 必须在**样式层**设置 `w:eastAsia`，否则跨机排版不一致
2. reportlab 5.x 的字体家族映射表只有 13 个西文家族，任何 CID 中文字体
   都需同时过三道关：registerFont + _ps2tt_map + _tt2ps_map
3. reportlab 的 `wordWrap="CJK"` 会吞掉行首空格 → 代码缩进必须转 &nbsp;
4. 表格识别走**保守判据**：表头行须以 `|` 起收 + 分隔行须含竖线，两条都
   满足才认成表格；宁可回落成段落，也不把散文里的竖线行误吞成表格
5. python-pptx 的 `font.name` 同样只写 `a:latin`；中文走 `a:ea`，须手写 rPr

各后端都是**可选依赖**：未安装时对应函数返回明确的 error 结果，
而不是抛异常中断流水线（降级策略，与项目既有约定一致）。
"""
from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any

# ────────────────────────────── 依赖探测 ──────────────────────────────

try:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor
    HAS_DOCX = True
except ImportError:      # pragma: no cover - 环境相关
    HAS_DOCX = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from reportlab.platypus import Table as _RT
    from reportlab.platypus.tables import TableStyle as _RTStyle
    HAS_PDF = True
except ImportError:      # pragma: no cover - 环境相关
    HAS_PDF = False

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font as _XlFont
    from openpyxl.utils import get_column_letter as _xl_col_letter
    HAS_XLSX = True
except ImportError:      # pragma: no cover - 环境相关
    HAS_XLSX = False

try:
    from pptx import Presentation
    from pptx.util import Inches as _PPInches
    HAS_PPTX = True
except ImportError:      # pragma: no cover - 环境相关
    HAS_PPTX = False

CJK_FONT = "微软雅黑"
MONO_FONT = "Consolas"
DEFAULT_TITLE = "生成文档"

_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_RE_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_RE_RULE = {"---", "***", "___"}
_RE_FENCE_LINE = re.compile(r"^\s*(`{3,}|~{3,})\s*(\S*)")
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
# 分隔行：| --- | :---: | 这类；单元格只允许 -、:、空格与竖线。
# 前置 (?=.*\|) 要求行内至少有一个竖线——否则单独的 `---`（水平线）
# 会沾上表头行被误判成分隔行。
_RE_TABLE_SEP = re.compile(r"^(?=.*\|)\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
# 单元格切分：只按「前面没有反斜杠」的竖线切，`\|` 是字面竖线
_RE_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

_RT_STYLE = None
if HAS_PDF:
    from reportlab.lib import colors as _rl_colors
    _RT_STYLE = _RTStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, _rl_colors.Color(0.72, 0.72, 0.72)),
        ("BACKGROUND", (0, 0), (-1, 0), _rl_colors.Color(0.93, 0.94, 0.96)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])


# ───────────────────────────── Markdown 解析 ─────────────────────────────

def clean_inline(text: str) -> str:
    """行内标记清洗：代码 / 粗体 / 图片 / 链接 → 纯文本。

    链接保留为 `文字 (URL)`——引用来源是文档可信度的关键信息，
    不能像正文提取那样丢掉 URL。
    图片必须在链接规则**之前**吃掉：`![alt](url)` 会被 `_RE_LINK`
    命中 `[alt](url)` 部分，留下一个 `!` 残渣——所以先处理图片。
    """
    text = _RE_INLINE_CODE.sub(r"\1", text)
    text = _RE_BOLD.sub(r"\1", text)
    text = _RE_IMAGE.sub(r"\1 (\2)", text)
    text = _RE_LINK.sub(r"\1 (\2)", text)
    return text.strip()


def parse_markdown(md: str) -> list[tuple[str, str]]:
    """把 Markdown 拆成 (kind, text) 序列。

    kind ∈ h1/h2/h3/p/li/code/quote/table/hr
    表格 = 连续 `|` 行且第二行是分隔行；否则整段回落为普通段落（不误吞）。
    """
    blocks: list[tuple[str, str]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # 围栏代码块（``` 或 ~~~）
        fence = _RE_FENCE_LINE.match(line)
        if fence:
            marker = fence.group(1)[0] * 3
            i += 1
            buf: list[str] = []
            while i < len(lines):
                closing = _RE_FENCE_LINE.match(lines[i].rstrip())
                if closing and closing.group(1).startswith(marker):
                    i += 1
                    break
                buf.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(buf)))
            continue

        if not line.strip():
            i += 1
            continue

        # 表格：本行是 | 行 且 下一行是分隔行，才认成表格；否则按普通段落走。
        # 分隔行只被跳过、不进数据——它可能不带首竖线（`--- | ---`），
        # 用"先全收再 pop"的写法会在这种形态上越界（已用回归用例钉住）。
        if _RE_TABLE_ROW.match(line) and i + 1 < len(lines) \
                and _RE_TABLE_SEP.match(lines[i + 1]):
            raw: list[str] = [line]
            i += 2                       # 跳过分隔行
            while i < len(lines) and _RE_TABLE_ROW.match(lines[i]):
                raw.append(lines[i])
                i += 1
            blocks.append(("table", "\n".join(raw)))
            continue

        if line.strip() in _RE_RULE:
            blocks.append(("hr", ""))
        elif line.startswith("### "):
            blocks.append(("h3", clean_inline(line[4:])))
        elif line.startswith("## "):
            blocks.append(("h2", clean_inline(line[3:])))
        elif line.startswith("# "):
            blocks.append(("h1", clean_inline(line[2:])))
        elif _RE_LIST_ITEM.match(line):
            blocks.append(("li", clean_inline(_RE_LIST_ITEM.sub("", line))))
        elif line.lstrip().startswith(">"):
            blocks.append(("quote", clean_inline(line.lstrip().lstrip("> "))))
        else:
            blocks.append(("p", clean_inline(line)))
        i += 1
    return blocks


def _table_rows(text: str) -> list[list[str]]:
    """table 块文本 → 二维行。

    竖线按「未被反斜杠转义」切分——ingest 产出把单元格里的字面竖线
    写作 `\\|`，用一个 `split("|")` 会把 `a\\|b` 劈成两半。首尾竖线
    产生的空首/空尾单元格丢掉；列数取齐最长行（缺的补空）。
    """
    rows: list[list[str]] = []
    for line in text.splitlines():
        cells = [c.strip().replace("\\|", "|")
                 for c in _RE_UNESCAPED_PIPE.split(line.strip())]
        if cells and cells[0] == "":
            cells.pop(0)                   # 行首的 |
        if cells and cells[-1] == "":
            cells.pop()                    # 行尾的 |
        rows.append(cells)
    if not rows:
        return []                          # 空文本不走到 max()（防子集调用方）
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


# ─────────────────────────────── docx 渲染 ───────────────────────────────

def _force_style_font(style, font: str = CJK_FONT) -> None:
    """在样式层钉死 ascii / hAnsi / eastAsia 三个字体属性。

    python-docx 的 style.font.name 只写 ascii/hAnsi，中文走 eastAsia 会回退
    宋体 → 文档在别人机器上排版不一致。在样式层设一次即可覆盖全文档，
    比逐 run 设置干净得多（长文档会产生海量 <w:rFonts>）。
    """
    from docx.oxml.ns import qn

    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), font)


def _set_run_font(run, font: str) -> None:
    """run 级字体覆盖（代码块等特例需要等宽字体）。"""
    from docx.oxml.ns import qn

    run.font.name = font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia"):
        rfonts.set(qn(attr), font)


def _setup_docx_styles(doc) -> None:
    """文档级样式：字号、行距、间距、中文字体，统一在样式层配置。"""
    normal = doc.styles["Normal"]
    normal.font.size = Pt(10.5)
    normal.paragraph_format.line_spacing = 1.4
    normal.paragraph_format.space_after = Pt(6)
    _force_style_font(normal)

    for name, size in (("Heading 1", 16), ("Heading 2", 13), ("Title", 22)):
        try:
            st = doc.styles[name]
        except KeyError:      # pragma: no cover - 模板缺样式
            continue
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor(0x11, 0x2B, 0x4A)
        st.paragraph_format.space_before = Pt(12)
        st.paragraph_format.space_after = Pt(7)
        _force_style_font(st)


_RE_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def xml_safe(text: str) -> tuple[str, int]:
    """剔掉 OOXML 文本层不接受的控制字符（保留 \t\n\r），返回 (干净文本, 剔掉几个)。

    抓来的正文里混一个 `\x08` 就够让 python-docx 抛 "All strings must be XML
    compatible"，整条流水线到此为止。这些字符不可见也不承载内容，剔除是唯一能
    交付的选择——但**数量要如实报给调用方**：正文被动过不该是静默的。
    """
    return _RE_XML_ILLEGAL.subn("", text)


def render_docx(markdown: str, output_path: str | Path,
                title: str = DEFAULT_TITLE) -> dict[str, Any]:
    """Markdown → docx。

    落真正的 Word 标题样式（Title / Heading 1 / Heading 2 / List Bullet），
    而非加粗的普通段落——这样 Word 才能自动生成目录、支持导航与二次编辑。
    """
    if not HAS_DOCX:
        return {"status": "error",
                "message": "python-docx 未安装，跳过 docx 渲染（pip install python-docx）"}
    markdown, stripped = xml_safe(markdown)
    # 标题同一条边界：docx 那边 core_properties 写入被 suppress 包着，脏标题不会
    # 报错而是静默丢掉文档属性——那比抛异常更难查。
    title, title_stripped = xml_safe(title)
    stripped += title_stripped
    blocks = parse_markdown(markdown)
    if not blocks:
        return {"status": "error", "message": "内容为空，无法渲染"}

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    _setup_docx_styles(doc)

    stats = {"h1": 0, "h2": 0, "h3": 0, "p": 0, "li": 0, "code": 0, "quote": 0,
             "table": 0}
    for kind, text in blocks:
        if kind == "h1":
            p = doc.add_heading("", level=0)      # → Word "Title" 样式
            p.add_run(text)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif kind == "h2":
            doc.add_heading("", level=1).add_run(text)
        elif kind == "h3":
            doc.add_heading("", level=2).add_run(text)
        elif kind == "li":
            doc.add_paragraph(style="List Bullet").add_run(text)
        elif kind == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            p.paragraph_format.line_spacing = 1.0
            r = p.add_run(text if text.strip() else " ")
            r.font.size = Pt(8.5)
            _set_run_font(r, MONO_FONT)
        elif kind == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(18)
            r = p.add_run(text)
            r.font.size = Pt(9.5)
            r.font.italic = True
            r.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        elif kind == "table":
            rows = _table_rows(text)
            if rows:
                t = doc.add_table(rows=len(rows), cols=len(rows[0]))
                t.style = "Table Grid"
                for ri, row in enumerate(rows):
                    for ci in range(len(rows[0])):
                        run = t.cell(ri, ci).paragraphs[0].add_run(row[ci])
                        if ri == 0:               # 表头加粗
                            run.font.bold = True
            else:
                doc.add_paragraph().add_run(text)
        elif kind == "hr":
            continue
        else:
            doc.add_paragraph().add_run(text)
        stats[kind] = stats.get(kind, 0) + 1

    # 文档属性写入失败不影响产出（core_properties 在某些模板下不可写）
    with contextlib.suppress(Exception):
        doc.core_properties.title = title

    doc.save(str(path))
    return {"status": "ok", "path": str(path), "format": "docx",
            "size": path.stat().st_size, "blocks": stats,
            "control_chars_stripped": stripped}


# ─────────────────────────────── pdf 渲染 ───────────────────────────────

def _register_cjk_font() -> None:
    """让 reportlab 5.x 能用内置 CID 中文字体 STSong-Light。

    reportlab 5 的字体查找有三道关卡，CID 中文字体全都不在默认表里：
      1. pdfmetrics.registerFont —— 注册 face
      2. fonts._ps2tt_map —— PostScript 名 → (family, bold, italic)
      3. fonts._tt2ps_map —— 反向查找
    CID 字体无独立粗体字重，粗体/斜体一律映射到常规体，
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


def _pdf_styles() -> dict[str, Any]:
    """ReportLab 样式表。

    所有中文样式一律从 BodyText 继承再覆盖 fontName——从 Heading1/2/3
    继承无效，父样式在构造期就已解析字体家族。
    """
    base = getSampleStyleSheet()
    no_infer = {"bold": False, "italic": False}
    body = ParagraphStyle(
        "BodyCN", parent=base["BodyText"], fontName="STSong-Light",
        fontSize=10.5, leading=16, spaceAfter=7, wordWrap="CJK", **no_infer,
    )
    return {
        "h1": ParagraphStyle(
            "H1CN", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=21, leading=27, alignment=1, spaceAfter=14,
            textColor="#112B4A", **no_infer),
        "h2": ParagraphStyle(
            "H2CN", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=15, leading=21, spaceBefore=12, spaceAfter=8,
            textColor="#112B4A", **no_infer),
        "h3": ParagraphStyle(
            "H3CN", parent=base["BodyText"], fontName="STSong-Light",
            fontSize=12.5, leading=18, spaceBefore=9, spaceAfter=5, **no_infer),
        "p": body,
        "li": ParagraphStyle("Li", parent=body, leftIndent=16, bulletIndent=6,
                             spaceAfter=4),
        "quote": ParagraphStyle("Quote", parent=body, leftIndent=16,
                                fontSize=9.5, textColor="#555555", **no_infer),
        "cell": ParagraphStyle("CellCN", parent=body, fontSize=9,
                               leading=13, spaceAfter=0, **no_infer),
        "code": ParagraphStyle(
            "Code", parent=base["Code"], fontName="Courier", fontSize=7.5,
            leading=9.5, leftIndent=16, backColor="#F5F5F5", borderPadding=3),
    }


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _preserve_indent(line: str) -> str:
    """保住代码行前导缩进。

    ReportLab 的 Paragraph 在 wordWrap="CJK" 下会吞掉行首空格，
    导致代码缩进全部消失、层级被抹平。先把前导空格转成 &nbsp; 再转义。
    """
    if not line:
        return ""
    stripped = line.lstrip(" ")
    indent = len(line) - len(stripped)
    return "&nbsp;" * indent + _xml_escape(stripped)


def render_pdf(markdown: str, output_path: str | Path,
               title: str = DEFAULT_TITLE) -> dict[str, Any]:
    """Markdown → pdf（A4，可打印定版归档）。"""
    if not HAS_PDF:
        return {"status": "error",
                "message": "reportlab 未安装，跳过 pdf 渲染（pip install reportlab）"}
    markdown, stripped = xml_safe(markdown)
    title, title_stripped = xml_safe(title)
    stripped += title_stripped
    blocks = parse_markdown(markdown)
    if not blocks:
        return {"status": "error", "message": "内容为空，无法渲染"}

    _register_cjk_font()
    st = _pdf_styles()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm, title=title,
    )
    flow: list[Any] = []
    for kind, text in blocks:
        if kind == "hr":
            flow.append(Spacer(1, 7))
        elif kind == "li":
            flow.append(Paragraph(_xml_escape(text), st["li"], bulletText="•"))
        elif kind == "code":
            # Paragraph 不吃换行 → 逐行独立 Paragraph，保住代码排版
            for ln in (text.splitlines() or [""]):
                flow.append(Paragraph(_preserve_indent(ln.rstrip()) or "&nbsp;",
                                      st["code"]))
        elif kind == "table":
            rows = _table_rows(text)
            if rows:
                data = [[Paragraph(_xml_escape(c), st["cell"]) for c in row]
                        for row in rows]
                tbl = _RT(data, hAlign="LEFT")
                tbl.setStyle(_RT_STYLE)
                flow.append(tbl)
                flow.append(Spacer(1, 7))
            else:
                flow.append(Paragraph(_xml_escape(text), st["p"]))
        else:
            flow.append(Paragraph(_xml_escape(text), st[kind]))

    doc.build(flow)
    return {"status": "ok", "path": str(path), "format": "pdf",
            "size": path.stat().st_size, "blocks": len(flow),
            "control_chars_stripped": stripped}


# ─────────────────────────────── xlsx 渲染 ───────────────────────────────

_XLSX_BAD_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")


def _xlsx_sheet_name(raw: str, used: set[str]) -> str:
    """工作表名：Excel 限 31 字符、禁 []:*?/\\；重名自动加序号。"""
    name = _XLSX_BAD_SHEET_CHARS.sub(" ", raw).strip()[:31] or "Sheet"
    base = name
    n = 2
    while name in used:
        suffix = f" ({n})"
        name = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(name)
    return name


def _xlsx_col_width(rows: list[list[str]], ci: int) -> float:
    """列宽 ≈ 最宽单元格的显示宽度（CJK 按 2 计），夹在 [10, 60]。"""
    width = 0
    for row in rows:
        if ci < len(row):
            width = max(width, sum(2 if ord(ch) > 0x2E7F else 1
                                   for ch in str(row[ci])))
    return float(min(max(width + 2, 10), 60))


def render_xlsx(markdown: str, output_path: str | Path,
                title: str = DEFAULT_TITLE) -> dict[str, Any]:
    """Markdown → xlsx（结构化子集）。

    映射：每张 Markdown 表格 → 一个工作表（名取最近的上游标题，重名/超长
    自动处理），表头加粗、冻结首行、列宽按内容估。无表格时整篇按行落进
    单个「正文」工作表——`mode` 字段如实回报走了哪条路，不是静默二选一。
    单元格一律按文本写入：保真优先（`007` 这类前导零、ID 串不做数值推断）。
    """
    if not HAS_XLSX:
        return {"status": "error",
                "message": "openpyxl 未安装，跳过 xlsx 渲染（pip install openpyxl）"}
    markdown, stripped = xml_safe(markdown)
    title, title_stripped = xml_safe(title)
    stripped += title_stripped
    blocks = parse_markdown(markdown)
    if not blocks:
        return {"status": "error", "message": "内容为空，无法渲染"}

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    default_ws = wb.active
    used: set[str] = {default_ws.title}
    tables: list[tuple[str, list[list[str]]]] = []
    heading = ""
    for kind, text in blocks:
        if kind in ("h1", "h2", "h3"):
            heading = text
        elif kind == "table":
            rows = _table_rows(text)
            if rows:
                tables.append((heading or f"表{len(tables) + 1}", rows))

    rows_written = 0
    if tables:
        for name, rows in tables:
            ws = wb.create_sheet(_xlsx_sheet_name(name, used))
            for ri, row in enumerate(rows, start=1):
                for ci, cell in enumerate(row, start=1):
                    c = ws.cell(row=ri, column=ci, value=cell)
                    if ri == 1:
                        c.font = _XlFont(bold=True)
                rows_written += 1
            for ci in range(1, len(rows[0]) + 1):
                ws.column_dimensions[_xl_col_letter(ci)].width = \
                    _xlsx_col_width(rows, ci - 1)
            ws.freeze_panes = "A2"
        wb.remove(default_ws)
        mode = "tables"
    else:
        ws = default_ws
        ws.title = _xlsx_sheet_name("正文", used)
        for kind, text in blocks:
            if kind == "hr":
                continue
            for line in text.splitlines() or [""]:
                rows_written += 1
                ws.cell(row=rows_written, column=1, value=line)
        ws.column_dimensions["A"].width = 80
        mode = "text"

    with contextlib.suppress(Exception):
        wb.properties.title = title
    wb.save(str(path))
    return {"status": "ok", "path": str(path), "format": "xlsx",
            "size": path.stat().st_size, "mode": mode,
            "sheets": len(wb.sheetnames), "tables": len(tables),
            "rows": rows_written, "control_chars_stripped": stripped}


# ─────────────────────────────── pptx 渲染 ───────────────────────────────

#: 幻灯片不是文档：溢出在 pptx 里不报错、只是看不见，所以宁可显式截断并注明
_PPTX_MAX_CODE_LINES = 12
_PPTX_MAX_TABLE_ROWS = 12
_PPTX_MAX_TABLE_COLS = 8


def _pptx_pin_font(run, font: str = CJK_FONT) -> None:
    """python-pptx 的 font.name 只写 a:latin；中文走 a:ea（连带 a:cs），手写 rPr。"""
    from pptx.oxml.ns import qn

    rpr = run._r.get_or_add_rPr()
    for tag in ("a:latin", "a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = rpr.makeelement(qn(tag), {})
            rpr.append(el)
        el.set("typeface", font)


def render_pptx(markdown: str, output_path: str | Path,
                title: str = DEFAULT_TITLE) -> dict[str, Any]:
    """Markdown → pptx（结构化子集）。

    映射：h1/h2/h3 各起一页（标题 = 标题文本）；其后的段落/列表/引用成要点；
    表格落**真的 PowerPoint 表格**。超 12 行 / 8 列的表格、超 12 行的代码块
    就地截断，并把「原表 N 行 × M 列」写在页内——幻灯片不是文档，宁可显式
    截断，也不让内容溢出到版面外（pptx 的溢出既不报错也看不见）。
    """
    if not HAS_PPTX:
        return {"status": "error",
                "message": "python-pptx 未安装，跳过 pptx 渲染（pip install python-pptx）"}
    markdown, stripped = xml_safe(markdown)
    title, title_stripped = xml_safe(title)
    stripped += title_stripped
    blocks = parse_markdown(markdown)
    if not blocks:
        return {"status": "error", "message": "内容为空，无法渲染"}

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    slides = 0
    tables = 0
    body: Any = None
    cur_slide: Any = None

    def _pin_paragraph(para) -> None:
        for run in para.runs:
            _pptx_pin_font(run)

    def _add_slide(heading: str) -> None:
        nonlocal slides, body, cur_slide
        cur_slide = prs.slides.add_slide(prs.slide_layouts[1])  # Title and Content
        t = cur_slide.shapes.title
        t.text = heading
        _pin_paragraph(t.text_frame.paragraphs[0])
        body = cur_slide.placeholders[1].text_frame
        slides += 1

    def _bullet(text: str) -> None:
        nonlocal body
        if body is None:
            _add_slide(title)
        assert body is not None
        first = body.paragraphs[0]
        para = first if (not first.runs and first.text == "") \
            else body.add_paragraph()
        para.text = text
        _pin_paragraph(para)

    def _add_table(rows: list[list[str]]) -> None:
        nonlocal tables
        if cur_slide is None:
            _add_slide(title)
        assert cur_slide is not None
        n_cols = min(len(rows[0]), _PPTX_MAX_TABLE_COLS)
        truncated = len(rows) > _PPTX_MAX_TABLE_ROWS + 1 \
            or len(rows[0]) > _PPTX_MAX_TABLE_COLS
        data = [r[:n_cols] for r in rows[: _PPTX_MAX_TABLE_ROWS + 1]]
        if truncated:
            data.append(
                [f"…（原表 {len(rows)} 行 × {len(rows[0])} 列，已截断）"]
                + [""] * (n_cols - 1))
        shape = cur_slide.shapes.add_table(
            len(data), n_cols,
            _PPInches(0.6), _PPInches(1.7),
            prs.slide_width - _PPInches(1.2), _PPInches(0.4 * len(data)))
        tbl = shape.table
        for ri, row in enumerate(data):
            for ci in range(n_cols):
                cell = tbl.cell(ri, ci)
                cell.text = row[ci] if ci < len(row) else ""
                for para in cell.text_frame.paragraphs:
                    _pin_paragraph(para)
                    if ri == 0:
                        for run in para.runs:
                            run.font.bold = True
        tables += 1

    for kind, text in blocks:
        if kind in ("h1", "h2", "h3"):
            _add_slide(text or title)
        elif kind == "table":
            rows = _table_rows(text)
            if rows:
                _add_table(rows)
            else:
                _bullet(text)
        elif kind == "code":
            lines = text.splitlines() or [""]
            for ln in lines[:_PPTX_MAX_CODE_LINES]:
                _bullet(ln if ln.strip() else " ")
            if len(lines) > _PPTX_MAX_CODE_LINES:
                _bullet(f"…（代码块共 {len(lines)} 行，已截断）")
        elif kind == "hr":
            continue
        else:                          # p / li / quote
            _bullet(text)

    if slides == 0:                    # 纯表格/纯代码文档也会有一页落脚
        _add_slide(title)
    with contextlib.suppress(Exception):
        prs.core_properties.title = title
    prs.save(str(path))
    return {"status": "ok", "path": str(path), "format": "pptx",
            "size": path.stat().st_size, "slides": slides,
            "tables": tables, "control_chars_stripped": stripped}


# ───────────────────────────── 统一入口 ─────────────────────────────

_BACKENDS = {"docx": render_docx, "pdf": render_pdf,
             "xlsx": render_xlsx, "pptx": render_pptx}


def render(markdown: str, output_path: str | Path, fmt: str = "docx",
           title: str = DEFAULT_TITLE) -> dict[str, Any]:
    """按扩展名/格式名分发到具体后端。"""
    backend = _BACKENDS.get(fmt.lower().lstrip("."))
    if backend is None:
        return {"status": "error",
                "message": f"不支持的格式: {fmt}（当前支持 {', '.join(_BACKENDS)}）"}
    return backend(markdown, output_path, title=title)


def supported_formats() -> list[str]:
    """当前环境实际可用的格式（供 API/Agent 如实回报能力）。"""
    flags = {"docx": HAS_DOCX, "pdf": HAS_PDF,
             "xlsx": HAS_XLSX, "pptx": HAS_PPTX}
    return [f for f, ok in flags.items() if ok]
