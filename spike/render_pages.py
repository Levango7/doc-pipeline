"""把 spike 产出的 PDF 渲染成 PNG，用于肉眼核对排版保真度。

跑法：python spike/render_pages.py
"""

from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "spike" / "out" / "spike_report.pdf"
OUT = PDF.parent


def main() -> None:
    doc = pymupdf.open(PDF)
    print(f"页数: {doc.page_count}")
    for i in range(doc.page_count):
        out = OUT / f"pdf_p{i + 1}.png"
        doc[i].get_pixmap(dpi=110).save(out)
        print(f"  {out.name}  {out.stat().st_size:,} B")


if __name__ == "__main__":
    main()
