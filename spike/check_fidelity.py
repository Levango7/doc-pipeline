"""程序化核对 spike 产物的排版保真度（不依赖肉眼看图）。

检查项：
1. 中文是否真的嵌入（无豆腐块/丢字）——抽查字符能否被PDF 文本层还原
2. 标题层级是否保留（字号梯度）
3. 表格/列表/代码块是否保留结构
4. docx 侧：样式是否正确落到Word 标题样式（决定能否生成目录、可否被二次编辑）

跑法：python spike/check_fidelity.py
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf
from docx import Document

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "spike" / "out" / "spike_report.pdf"
DOCX = ROOT / "spike" / "out" / "spike_report.docx"
SRC = ROOT / "output" / "smoke_20260830.md"

CJK = re.compile(r"[\u4e00-\u9fff]")


def check_pdf() -> None:
    print("=" * 60)
    print("PDF 保真度")
    print("=" * 60)
    doc = pymupdf.open(PDF)
    print(f"页数              : {doc.page_count}")

    # 字体嵌入情况
    fonts: Counter[str] = Counter()
    for page in doc:
        for f in page.get_fonts(full=True):
            fonts[f[3]] += 1
    print(f"嵌入字体          : {dict(fonts)}")

    # 中文抽取往返：中文字符能否被文本层还原
    text = "".join(page.get_text() for page in doc)
    src_cjk = len(CJK.findall(SRC.read_text(encoding='utf-8')))
    pdf_cjk = len(CJK.findall(text))
    ratio = pdf_cjk / src_cjk if src_cjk else 0
    print(f"中文字符          : 源 {src_cjk} → PDF {pdf_cjk}（{ratio:.0%} 保留）")

    # 标题字号梯度
    sizes: Counter[float] = Counter()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if span["text"].strip():
                        sizes[round(span["size"], 1)] += len(span["text"].strip())
    top = sorted(sizes.items(), key=lambda kv: -kv[0])[:6]
    print(f"字号分布(字符数)  : {top}")
    print(f"  → 层级数        : {len(sizes)} 种不同字号")

    # 列表符号 / 结构标记
    print(f"列表符号(•)       : {text.count(chr(0x2022))}")
    print(f"降级声明保留      : {'是' if '降级声明' in text else '否'}")


def check_docx() -> None:
    print()
    print("=" * 60)
    print("DOCX 保真度")
    print("=" * 60)
    doc = Document(DOCX)
    paras = [p for p in doc.paragraphs if p.text.strip()]
    styles = Counter(p.style.name for p in paras)
    print(f"非空段落          : {len(paras)}")
    print(f"样式分布          : {dict(styles)}")

    # Word 标题样式是可被导航窗格/目录识别的关键
    heading_styles = [s for s in styles if s.startswith("Heading") or s == "Title"]
    print(f"  → Word 标题段落 : {sum(styles[s] for s in heading_styles)}（决定能否自动生成目录）")

    # 东亚字体是否落到 XML（决定中文在别人机器上会不会变宋体）
    # docx 是 zip，必须解包读 styles.xml，直接当纯文本读会误报"未设置"
    import zipfile

    with zipfile.ZipFile(DOCX) as z:
        styles_xml = z.read("word/styles.xml").decode("utf-8")
        doc_xml = z.read("word/document.xml").decode("utf-8")

    east_asia = sorted(set(re.findall(r'w:eastAsia="([^"]+)"', styles_xml)))
    print(f"样式层eastAsia   : {east_asia}")
    print(f"  → 中文已钉字体 : {'是' if any('雅黑' in f or '宋' in f or '黑' in f for f in east_asia) else '否'}")
    print(f"run 级覆盖次数   : {len(re.findall(r'w:eastAsia=', doc_xml))}（代码块等特例）")


def main() -> None:
    check_pdf()
    check_docx()


if __name__ == "__main__":
    main()
