"""端到端闭环验证：fetcher 提取（含代码块保真）→ docx / pdf 渲染。

跑法：python spike/check_e2e.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents.fetcher import FetcherAgent  # noqa: E402
from spike.render_spike import (  # noqa: E402
    OUT,
    parse_markdown,
    render_docx,
    render_pdf,
)

HTML = """<html><body><article>
<h1>Python 异步编程实践</h1>
<p>异步编程是一种并发编程模型，可以在单线程中高效地执行多个任务，特别适用于 I/O 密集型任务。</p>
<pre><code class="language-python">import asyncio

async def worker(n):
    await asyncio.sleep(1)
    return f"任务 {n} 完成"

async def main():
    tasks = [asyncio.create_task(worker(i)) for i in range(3)]
    for r in await asyncio.gather(*tasks):
        print(r)

asyncio.run(main())</code></pre>
<h2>并发执行的好处</h2>
<p>下面这个例子展示了同步与异步的执行时间差异，同步版本总耗时是三者之和。</p>
<pre><code>start = time.time()
for i in range(3):
    time.sleep(1)
print(time.time() - start)</code></pre>
<p>使用 asyncio.gather 之后，总耗时约等于最长的单个任务，而不是三者之和，效率显著提升。</p>
</article></body></html>"""


def main() -> None:
    agent = FetcherAgent.__new__(FetcherAgent)
    text = agent._extract_text(HTML)

    print("=" * 64)
    print("步骤1 — fetcher 提取结果")
    print("=" * 64)
    print(text)
    print()

    blocks = parse_markdown(text)
    kinds = {}
    for k, _ in blocks:
        kinds[k] = kinds.get(k, 0) + 1
    print(f"→ 解析为 {len(blocks)} 块: {kinds}")

    OUT.mkdir(parents=True, exist_ok=True)
    dx = OUT / "e2e_from_fetch.docx"
    pf = OUT / "e2e_from_fetch.pdf"
    render_docx(blocks, dx)
    render_pdf(blocks, pf)
    print(f"→ docx: {dx.name}  {dx.stat().st_size:,} B")
    print(f"→ pdf : {pf.name}  {pf.stat().st_size:,} B")

    # 核对代码是否以代码块形式进入渲染产物
    from docx import Document

    doc = Document(dx)
    code_paras = [
        p for p in doc.paragraphs
        if p.text.strip().startswith(("import asyncio", "    tasks", "await", "asyncio.run"))
    ]
    print(f"→ docx 中保真代码行: {len(code_paras)}")
    for p in code_paras[:5]:
        print(f"    | {p.text}")

    import pymupdf

    pdf = pymupdf.open(pf)
    pdf_text = "".join(pg.get_text() for pg in pdf)
    print(f"→ pdf 页数: {pdf.page_count}")
    print(f"→ pdf 含缩进代码行: {'是' if '    tasks = [asyncio' in pdf_text else '否'}")
    print(f"→ pdf 含压平痕迹  : {'是（未修复）' if 'asyncio . run' in pdf_text else '否'}")


if __name__ == "__main__":
    main()
