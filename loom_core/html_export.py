"""HTML 交付原语：Markdown → HTML 字符串 + 静态站点构建。

引擎层交付认两块（spec §2.1 验收线第 4 条的缺口）：
  1. ``markdown_to_html_string`` —— 单页 HTML（此前只活在 scripts/format_converter
     里，引擎交付要用就得跨层抄；现在以这里为唯一实现，scripts 与 renderer 都委托它）；
  2. ``build_site`` —— 把一轮运行的多份 Markdown 产物构建成可浏览的静态站点
     （index 导航 + 每份产物一页，页间互链）。

全部纯标准库、离线、逐字可复现；不发起任何网络请求。
"""
from __future__ import annotations

import html as _html
import re
from pathlib import Path
from typing import Any

_DEFAULT_CSS = """
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; }
        h1, h2, h3, h4, h5, h6 { margin-top: 1.5em; border-bottom: 1px solid #eee; padding-bottom: 0.3em; }
        pre { background: #f6f8fa; padding: 16px; border-radius: 6px; overflow: auto; }
        code { background: #f6f8fa; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; }
        pre code { background: none; padding: 0; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px 12px; }
        th { background: #f6f8fa; }
        blockquote { border-left: 4px solid #ddd; margin: 0; padding-left: 16px; color: #666; }
        hr { border: none; border-top: 2px solid #eee; }
        a { color: #0366d6; text-decoration: none; }
"""


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)
    return text


def _markdown_to_html_body(md: str) -> str:
    """块级 Markdown → HTML（与 scripts/format_converter 原实现逐行为一致）。"""
    lines = md.split("\n")
    out: list[str] = []
    in_code = False
    in_table = False
    in_list = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                lang = line.strip()[3:].strip()
                out.append(f'<pre><code class="language-{lang}">')
                in_code = True
            continue
        if in_code:
            out.append(_escape_html(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline_md(m.group(2))}</h{level}>")
            continue
        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if cells and all(re.match(r"^[-:]+$", c) for c in cells):
                continue
            if not in_table:
                out.append("<table><thead><tr>")
                for c in cells:
                    out.append(f"<th>{_inline_md(c)}</th>")
                out.append("</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>")
                for c in cells:
                    out.append(f"<td>{_inline_md(c)}</td>")
                out.append("</tr>")
            continue
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        m = re.match(r"^[\s]*[-*+]\s+(.*)", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.strip().startswith(">"):
            out.append(f"<blockquote>{_inline_md(line.strip()[1:].strip())}</blockquote>")
            continue
        if re.match(r"^---+\s*$", line):
            out.append("<hr>")
            continue
        if line.strip():
            out.append(f"<p>{_inline_md(line)}</p>")
        else:
            out.append("")
    if in_table:
        out.append("</tbody></table>")
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def markdown_to_html_string(md_text: str, title: str = "文档",
                            css: str | None = None) -> str:
    """Markdown 文本 → 完整 HTML 页面字符串（含 <head> 与内置样式）。"""
    body = _markdown_to_html_body(md_text)
    style = _DEFAULT_CSS if css is None else css
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '    <meta charset="UTF-8">\n'
        '    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>{_html.escape(title)}</title>\n"
        f"    <style>{style}</style>\n</head>\n<body>\n"
        '    <div class="container">\n'
        f"{body}\n    </div>\n</body>\n</html>"
    )


# ─── 静态站点构建 ─────────────────────────────────

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_\-]+")


def _slugify(name: str, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or fallback


def build_site(pages: list[dict[str, Any]], out_dir: str | Path,
               site_title: str = "交付站点") -> dict[str, Any]:
    """把一组 Markdown 页面构建成静态站点。

    pages 每项：{"title": 页面标题, "markdown": 正文, "slug": 可选文件名
    （缺省按 title 归一；冲突时追加序号）}。产出：
      out_dir/index.html           导航页（列出全部页面并互链）
      out_dir/pages/<slug>.html    每份产物一页（带返回导航）

    纯离线：不取任何远程资源；样式与单页转换共用一份。
    """
    root = Path(out_dir)
    pages_dir = root / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    built: list[dict[str, str]] = []
    for idx, page in enumerate(pages):
        title = str(page.get("title") or f"页面 {idx + 1}")
        markdown = str(page.get("markdown") or "")
        slug = _slugify(str(page.get("slug") or title), f"page-{idx + 1}")
        while slug in used:
            slug = f"{slug}-{idx + 1}"
        used.add(slug)
        page_path = pages_dir / f"{slug}.html"
        page_html = markdown_to_html_string(markdown, title=title)
        page_html = page_html.replace(
            '<div class="container">',
            '<div class="container"><p><a href="../index.html">← 返回目录</a></p>',
            1)
        page_path.write_text(page_html, encoding="utf-8")
        built.append({"title": title, "slug": slug, "path": str(page_path)})

    nav: list[str] = []
    for item in built:
        nav.append(f'<li><a href="pages/{item["slug"]}.html">{_html.escape(item["title"])}</a></li>')
    nav_body = "\n".join(nav) or "<li>（无页面）</li>"
    index_html = markdown_to_html_string(
        f"# {site_title}\n\n本站由流水线交付自动构建，共 {len(built)} 页。\n",
        title=site_title)
    index_html = index_html.replace(
        '<div class="container">',
        '<div class="container"><ul class="site-nav">\n' + nav_body + "\n</ul>",
        1)
    index_path = root / "index.html"
    index_path.write_text(index_html, encoding="utf-8")

    return {"index": str(index_path), "pages": built, "page_count": len(built)}
