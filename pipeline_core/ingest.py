"""摄入层：本地文件（PDF / 图片 / 文本）→ 结构化 Markdown。

这是渲染层的对偶能力：渲染把 Markdown 变成 docx/pdf，摄入把
PDF/图片变回 Markdown，从而让**已有资料**能进入生成链路——
知识库、报告生成、多格式输出都以此为前提。

后端策略（与 renderer 的"可选依赖"约定一致）：
- **pymupdf（核心，纯 Python、几 MB）**：数字版PDF 提取，实测中文
  无损、字号信息可用于推断标题层级。默认后端。
- **OCR 后端（可选，模型 GB 级）**：扫描件/图片才需要。
  优先探测 PaddleOCR(PP-StructureV3) / MinerU，未安装则如实回报。
  不作为默认依赖——OCR 模型体积会拖垮轻量部署。

关键实现约束：
1. 数字版 PDF 的标题层级靠**字号聚类**推断，不是靠标签
2. 扫描件（无文本层）必须显式识别并路由到 OCR，否则会静默返回空
3. 表格用 PyMuPDF 的 find_tables()，失败时降级为纯文本段落
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import pymupdf
    HAS_PYMUPDF = True
except ImportError:      # pragma: no cover - 环境相关
    HAS_PYMUPDF = False

# 支持的输入类型
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}
TEXT_SUFFIXES = {".md", ".txt", ".markdown"}

SUPPORTED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES | TEXT_SUFFIXES

# 一页文本少于该字符数视为"疑似扫描件"（需要 OCR）
SCANNED_PAGE_THRESHOLD = 20

# 标题字号判定：相对正文字号的最大倍数
_HEADING_SIZE_RATIO = 1.18
# 标题文本长度上限，超过更像正文而非标题
_MAX_HEADING_LEN = 80

_RE_WS = re.compile(r"[ \t]+")
_RE_MULTI_NL = re.compile(r"\n{3,}")
# PDF 文本层常混入不换行空格（\xa0），会让下游的字符串匹配失效
# （"第一部分 概述" != "第一部分\xa0概述"），统一归一化为普通空格。
_RE_NBSP = re.compile(r"[   ]")

# OCR 后端探测结果缓存（None 也是有效结果，故用哨兵区分"未探测"）
_UNSET: str | None = "__unset__"
_OCR_BACKEND_CACHE: str | None = _UNSET


# ─────────────────────────── 后端探测 ───────────────────────────

def ocr_backend() -> str | None:
    """探测可用的 OCR 后端，返回名称；都不可用时返回 None。

    OCR 是可选能力：扫描件与图片才需要，数字版 PDF 不需要。

    注意：只 `import paddleocr` 是不够的 —— 实测本机 paddleocr 3.7.0
    虽然装着，但 PP-StructureV3 缺 paddlex[ocr] 依赖、实例化即抛错。
    因此这里必须真正**实例化一次**才能算可用，否则会给调用方
    一个无法兑现的承诺。

    结果进程内缓存：实例化要加载模型权重（秒级），不应在每次调用时重做。
    """
    global _OCR_BACKEND_CACHE
    if _OCR_BACKEND_CACHE is not _UNSET:
        return _OCR_BACKEND_CACHE

    backend: str | None = None
    try:
        from paddleocr import PPStructureV3
        PPStructureV3()          # 实例化验证；失败即视为不可用
        backend = "paddleocr"
    except Exception:
        try:
            import mineru  # noqa: F401
            backend = "mineru"
        except Exception:
            backend = None

    _OCR_BACKEND_CACHE = backend
    return backend


def available_backends() -> list[str]:
    out = []
    if HAS_PYMUPDF:
        out.append("pymupdf")
    ocr = ocr_backend()
    if ocr:
        out.append(ocr)
    return out


# ─────────────────────────── 标题层级推断 ───────────────────────────

def _infer_headings(spans: list[dict], body_size: float) -> dict[str, str]:
    """按字号把 span 映射为 Markdown 标题级别。

    数字版 PDF 没有语义标签，标题只能靠字号区分：显著大于正文的
    字号视为标题，最大的映射为 h1，其后按字号降序分配 h2/h3...
    """
    if not spans:
        return {}

    sizes = sorted({round(s["size"], 1) for s in spans}, reverse=True)
    heading_sizes = [s for s in sizes if s >= body_size * _HEADING_SIZE_RATIO]
    if not heading_sizes:
        return {}

    # 字号排名 → 标题级别（最多到 h3，再深就并入 h3）
    level_of = {sz: min(i + 1, 3) for i, sz in enumerate(heading_sizes)}

    result: dict[str, str] = {}
    for s in spans:
        sz = round(s["size"], 1)
        text = _RE_NBSP.sub(" ", s["text"]).strip()
        if sz in level_of and text and len(text) <= _MAX_HEADING_LEN:
            result[text] = "#" * level_of[sz]
    return result


def _body_size(spans: list[dict]) -> float:
    """估算正文字号：取字符数最多那一档的字号（众数）。"""
    weights: dict[float, int] = {}
    for s in spans:
        t = s["text"].strip()
        if t:
            weights[round(s["size"], 1)] = weights.get(round(s["size"], 1), 0) + len(t)
    if not weights:
        return 10.0
    # 字符数最多的字号档
    return max(weights.items(), key=lambda kv: kv[1])[0]


# ─────────────────────────── PDF 提取 ───────────────────────────

def _extract_pdf(path: Path) -> dict[str, Any]:
    """数字版 PDF → Markdown。"""
    doc = pymupdf.open(str(path))
    try:
        if doc.needs_pass:
            return {"status": "error", "message": "PDF 已加密，需要密码"}

        pages_md: list[str] = []
        scanned_pages = 0
        total_pages = doc.page_count
        table_count = 0

        for page in doc:
            page_dict = page.get_text("dict")
            spans = [
                span
                for block in page_dict.get("blocks", [])
                for line in block.get("lines", [])
                for span in line.get("spans", [])
            ]
            page_text = "".join(s["text"] for s in spans).strip()
            page_text = _RE_NBSP.sub(" ", page_text)

            # 无文本层或极少文本 → 疑似扫描件，需 OCR
            if len(page_text) < SCANNED_PAGE_THRESHOLD:
                scanned_pages += 1
                pages_md.append("")
                continue

            body = _body_size(spans)
            heading_map = _infer_headings(spans, body)
            parts: list[str] = []
            for block in page_dict.get("blocks", []):
                for line in block.get("lines", []):
                    line_txt = "".join(s["text"] for s in line.get("spans", []))
                    # 归一化不换行空格：PDF 文本层常混入 \xa0，
                    # 会让 heading_map 的键与此处文本对不上、标题识别失效
                    line_txt = _RE_NBSP.sub(" ", line_txt)
                    line_txt = _RE_WS.sub(" ", line_txt).strip()
                    if not line_txt:
                        continue
                    prefix = heading_map.get(line_txt)
                    parts.append(f"{prefix} {line_txt}" if prefix else line_txt)
                parts.append("")   # 块间空行

            # 表格
            try:
                tables = page.find_tables()
            except Exception:
                tables = []
            for tbl in tables:
                rows = tbl.extract()
                if len(rows) >= 2 and len(rows[0]) >= 2:
                    table_count += 1
                    parts.append(_rows_to_markdown(rows))
                    parts.append("")

            pages_md.append("\n".join(parts))

        markdown = _RE_MULTI_NL.sub("\n\n", "\n".join(pages_md)).strip()

        return {
            "status": "ok",
            "backend": "pymupdf",
            "markdown": markdown,
            "pages": total_pages,
            "scanned_pages": scanned_pages,
            "tables": table_count,
            "needs_ocr": scanned_pages > 0,
            "chars": len(markdown),
        }
    finally:
        doc.close()


def _rows_to_markdown(rows: list[list[Any]]) -> str:
    """表格行 → Markdown 表格。"""
    def cell(v: Any) -> str:
        return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()

    header = [cell(c) for c in rows[0]]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows[1:]:
        padded = list(row) + [""] * (len(header) - len(row))
        lines.append("| " + " | ".join(cell(c) for c in padded[:len(header)]) + " |")
    return "\n".join(lines)


# ─────────────────────────── 图片 / 扫描件 ───────────────────────────

def _extract_image(path: Path) -> dict[str, Any]:
    """图片 → Markdown，必须走 OCR。

    图片本身没有文本层，只能靠 OCR。这里**不静默返回空**——
    明确回报"需要 OCR 后端"，让调用方知道该装什么。
    """
    backend = ocr_backend()
    if not backend:
        return {
            "status": "error",
            "needs_ocr": True,
            "message": (
                f"{path.suffix} 需要 OCR 后端，当前环境未安装。"
                "推荐 pip install paddleocr（PP-StructureV3，Apache-2.0，"
                "中文版面与表格还原效果最好）或 mineru"
            ),
        }

    if backend == "paddleocr":
        return _ocr_with_paddleocr(path)
    return _ocr_with_mineru(path)


def _ocr_with_paddleocr(path: Path) -> dict[str, Any]:   # pragma: no cover - 需重模型
    """PaddleOCR / PP-StructureV3 识别。"""
    try:
        from paddleocr import PPStructureV3
        engine = PPStructureV3()
        result = engine.predict(str(path))
        markdown = _collect_paddle_markdown(result)
        return {"status": "ok" if markdown else "error",
                "backend": "paddleocr", "markdown": markdown,
                "chars": len(markdown),
                "message": "" if markdown else "OCR 未识别出文本"}
    except Exception as e:
        return {"status": "error", "backend": "paddleocr", "message": str(e)}


def _collect_paddle_markdown(result: Any) -> str:        # pragma: no cover
    """从 PP-StructureV3 结果里收集 Markdown 文本。"""
    chunks: list[str] = []
    try:
        for res in (result if isinstance(result, (list, tuple)) else [result]):
            payload = getattr(res, "json", None)
            data = payload.get("res", payload) if isinstance(payload, dict) else {}
            md = data.get("markdown") if isinstance(data, dict) else None
            text = md.get("text") if isinstance(md, dict) else md
            if isinstance(text, str):
                chunks.append(text)
    except Exception:
        pass
    return "\n\n".join(c for c in chunks if c).strip()


def _ocr_with_mineru(path: Path) -> dict[str, Any]:     # pragma: no cover - 需重模型
    """MinerU 识别。"""
    try:
        from mineru.cli.common import do_parse

        pdf_bytes = path.read_bytes()
        out = do_parse(
            output_dir=str(path.parent / f"{path.stem}_mineru"),
            pdf_file_names=[path.name],
            pdf_bytes_list=[pdf_bytes],
            p_lang_list=["ch"],
        )
        md_files = list(Path(out[0]).rglob("*.md")) if out else []
        markdown = md_files[0].read_text(encoding="utf-8") if md_files else ""
        return {"status": "ok" if markdown else "error",
                "backend": "mineru", "markdown": markdown,
                "chars": len(markdown),
                "message": "" if markdown else "MinerU 未产出 Markdown"}
    except Exception as e:
        return {"status": "error", "backend": "mineru", "message": str(e)}


# ─────────────────────────── 纯文本 ───────────────────────────

def _extract_text(path: Path) -> dict[str, Any]:
    """Markdown / txt 直读（编码嗅探，避免中文乱码）。"""
    raw = path.read_bytes()
    text = ""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    text = text.strip()
    return {"status": "ok" if text else "error", "backend": "text",
            "markdown": text, "chars": len(text),
            "message": "" if text else "空文件"}


# ─────────────────────────── 统一入口 ───────────────────────────

def ingest(file_path: str | Path) -> dict[str, Any]:
    """把本地文件转成结构化 Markdown。

    返回 dict：status/backend/markdown/chars 等。
    失败时返回 status="error" 与可读的 message，不抛异常。
    """
    if not HAS_PYMUPDF:
        # PDF 主路径依赖 pymupdf；纯文本仍可工作
        if Path(file_path).suffix.lower() in TEXT_SUFFIXES:
            return _extract_text(Path(file_path))
        return {"status": "error",
                "message": "pymupdf 未安装，无法解析 PDF（pip install pymupdf）"}

    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"文件不存在: {path}"}
    if not path.is_file():
        return {"status": "error", "message": f"不是文件: {path}"}

    suffix = path.suffix.lower()
    try:
        if suffix in PDF_SUFFIXES:
            return _extract_pdf(path)
        if suffix in IMAGE_SUFFIXES:
            return _extract_image(path)
        if suffix in TEXT_SUFFIXES:
            return _extract_text(path)
    except Exception as e:      # 单文件解析失败不应中断批量处理
        return {"status": "error", "file": str(path), "message": str(e)}

    return {"status": "error",
            "message": f"不支持的文件类型: {suffix or '(无扩展名)'}"}


def ingest_many(file_paths: list[str | Path]) -> dict[str, Any]:
    """批量摄入，逐个文件独立成败，不因单文件失败中断。"""
    docs: list[dict[str, Any]] = []
    for fp in file_paths:
        res = ingest(fp)
        res.setdefault("file", str(fp))
        docs.append(res)

    ok = [d for d in docs if d.get("status") == "ok" and d.get("markdown")]
    merged = "\n\n---\n\n".join(d["markdown"] for d in ok)
    return {
        "status": "ok" if ok else "error",
        "documents": docs,
        "succeeded": len(ok),
        "failed": len(docs) - len(ok),
        "markdown": merged,
        "chars": len(merged),
        "needs_ocr": any(d.get("needs_ocr") for d in docs),
        "backends": sorted({d.get("backend", "") for d in ok if d.get("backend")}),
    }
