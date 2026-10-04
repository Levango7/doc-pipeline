"""验证摄入层：真实 PDF / 文本 / 不存在文件 / 图片四条路径。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docpipeline import ingest  # noqa: E402

print("可用后端:", ingest.available_backends())
print("OCR 后端:", ingest.ocr_backend() or "无")
print()

PDF = ROOT / "output" / "render_e2e.pdf"
res = ingest.ingest(PDF)
print(f"[PDF] status={res['status']} backend={res.get('backend')} "
      f"pages={res.get('pages')} tables={res.get('tables')} "
      f"chars={res.get('chars')} needs_ocr={res.get('needs_ocr')}")
print("--- Markdown 前 400 字 ---")
print(res.get("markdown", "")[:400])
print()

TXT = ROOT / "input_test.md"
res2 = ingest.ingest(TXT)
print(f"[TXT] status={res2['status']} chars={res2.get('chars')}")
print()

res3 = ingest.ingest(ROOT / "output" / "不存在.pdf")
print(f"[缺失] status={res3['status']} message={res3.get('message')}")
print()

IMG = ROOT / "output" / "render_e2e.pdf"
fake_img = Path(str(IMG) + ".png")
res4 = ingest.ingest(ROOT / "output" / "image_x.png")
print(f"[图片-不存在] status={res4['status']} message={res4.get('message')}")
print()

batch = ingest.ingest_many([PDF, TXT, ROOT / "nope.pdf"])
print(f"[批量] succeeded={batch['succeeded']} failed={batch['failed']} "
      f"chars={batch['chars']} backends={batch['backends']}")
