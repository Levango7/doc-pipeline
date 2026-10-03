"""FetcherAgent 正文提取 — 标题（<h1>-<h6>）保真护栏。

背景：标题原先与普通段落同等对待，受正文长度门槛（<30 字符）约束，
短标题（`<h2>总结</h2>`）被整段丢弃，标题语义在提取阶段就丢失——
下游渲染层再强也拿不到标题，只能把所有内容当正文平铺。

本文件锁住修复后的行为：
- 短标题不再被丢弃，转成 Markdown 标记保留层级
- 标题顺序与层级正确
- 超长"伪标题"仍被丢弃（误标的长段落）
- 正文的长度/密度门槛不受影响（兼容性保证）
- 两条提取路径（selectolax / 正则回退）行为一致
"""
import pytest

from agents.fetcher import FetcherAgent

# 标题刻意取得短（<30 字符），正是修复前被丢弃的场景
SHORT_HEADINGS = (
    "<h1>概述</h1>"
    "<h2>核心概念</h2>"
    "<h3>细节</h3>"
)

# 正文需超过 MIN_CONTENT_LENGTH(200)，否则会触发"全文兜底"分支
# （该分支按设计会剥掉所有标签，是既有行为，不属于标题修复范围）。
# 三段正文必须内容不同 —— _dedupe_blocks 会把完全相同的块当重复删掉，
# 三段雷同会导致剩余长度不足 200 而误触兜底。
P1 = (
    "这是第一段正文，内容足够长以便通过密度筛选与长度门槛，"
    "用于验证标题修复之后正文提取行为保持不变，"
    "并且这一段必须与另外两段有实质差异。"
)
P2 = (
    "第二段正文讨论事件循环的调度机制，说明任务如何在协程之间切换，"
    "这一段同样需要足够长度，并且与前后段落内容不同。"
)
P3 = (
    "第三段正文总结异步编程的适用边界，指出计算密集型任务并不适合，"
    "内容同样保持足够长度以通过所有正文筛选条件。"
)
P4 = (
    "第四段正文补充工程实践建议，包括超时控制、异常传播与并发度上限，"
    "这些内容同样足够长，确保整体不触发全文兜底分支。"
)
# 单段场景用：够长以避免触发全文兜底
LONG_PARA = P1 + P2 + P3


def _doc(headings: str) -> str:
    return (
        "<html><body><article>"
        f"{headings}"
        f"<p>{P1}</p><h2>核心概念</h2><p>{P2}</p><h3>细节</h3><p>{P3}</p>"
        "</article></body></html>"
    )


HTML = (
    "<html><body><article>"
    "<h1>概述</h1>" f"<p>{P1}</p>"
    "<h2>核心概念</h2>" f"<p>{P2}</p>"
    "<h3>细节</h3>" f"<p>{P3}</p>"
    "</article></body></html>"
)


@pytest.fixture
def agent(tmp_path):
    return FetcherAgent.__new__(FetcherAgent)


# ───────────────────────────── 模块级辅助函数 ─────────────────────────────

class TestMarkdownHeadingHelper:
    @pytest.mark.parametrize("tag,level", [
        ("h1", "#"), ("h2", "##"), ("h3", "###"),
        ("h4", "####"), ("h5", "#####"), ("h6", "######"),
    ])
    def test_level_maps_to_hashes(self, tag, level):
        from agents.fetcher import _as_markdown_heading
        assert _as_markdown_heading(tag, "标题") == f"{level} 标题"

    def test_level_clamped_to_six(self):
        from agents.fetcher import _as_markdown_heading
        # 非法层级不得生成 7 个以上 #（会变成代码围栏语义）
        assert _as_markdown_heading("h9", "x").count("#") <= 6


# ─────────────────────────── selectolax 路径 ───────────────────────────

class TestSelectolaxHeadings:
    def test_short_headings_survive(self, agent):
        text = agent._extract_text(HTML)
        assert "# 概述" in text
        assert "## 核心概念" in text
        assert "### 细节" in text

    def test_heading_order_preserved(self, agent):
        text = agent._extract_text(HTML)
        idx = (text.index("# 概述"),
               text.index("## 核心概念"),
               text.index("### 细节"))
        assert idx == tuple(sorted(idx)), "标题顺序应与原文一致"

    def test_prose_still_extracted(self, agent):
        text = agent._extract_text(HTML)
        assert P1 in text
        assert P2 in text
        assert P3 in text

    def test_h5_h6_supported(self, agent):
        html = (
            "<html><body><article>"
            "<h5>五级标题</h5><h6>六级标题</h6>"
            f"<p>{LONG_PARA}</p>"
            "</article></body></html>"
        )
        text = agent._extract_text(html)
        assert "##### 五级标题" in text
        assert "###### 六级标题" in text

    def test_oversized_heading_dropped(self, agent):
        """被误标成 h1 的整段正文（>120 字符）不应变成标题。"""
        long_fake = "误" * 200
        html = (
            "<html><body><article>"
            f"<h1>{long_fake}</h1>"
            f"<p>{LONG_PARA}</p>"
            "</article></body></html>"
        )
        text = agent._extract_text(html)
        assert f"# {long_fake}" not in text


# ──────────────────────────── 正则回退路径 ────────────────────────────

class TestRegexPathHeadings:
    def test_short_headings_survive(self, agent):
        text = agent._extract_text_regex(HTML)
        assert "# 概述" in text
        assert "## 核心概念" in text
    def test_heading_order_preserved(self, agent):
        text = agent._extract_text_regex(HTML)
        idx = (text.index("# 概述"),
               text.index("## 核心概念"),
               text.index("### 细节"))
        assert idx == tuple(sorted(idx))

    def test_prose_still_extracted(self, agent):
        text = agent._extract_text_regex(HTML)
        assert P1 in text
        assert P2 in text
        assert P3 in text

    def test_oversized_heading_dropped(self, agent):
        long_fake = "误" * 200
        html = f"<article><h1>{long_fake}</h1><p>{LONG_PARA}</p></article>"
        text = agent._extract_text_regex(html)
        assert f"# {long_fake}" not in text


# ──────────────────────────── 两条路径一致性 ────────────────────────────

class TestPathParity:
    @pytest.mark.parametrize("headings", [
        SHORT_HEADINGS,                       # 多级短标题
        "<h2>单标题</h2>",                     # 单个
        "<h1>A</h1><h2>B</h2><h3>C</h3>",    # 极短标题
    ])
    def test_same_headings_both_paths(self, agent, headings):
        html = f"<html><body><article>{headings}<p>{LONG_PARA}</p></article></body></html>"
        a = agent._extract_text(html)
        b = agent._extract_text_regex(html)
        for path, name in ((a, "selectolax"), (b, "regex")):
            for h in headings.split("<h")[1:]:
                title = h.split(">")[1].split("<")[0]
                assert title in path, f"{name} 路径丢失标题 {title}"


# ────────────────────── 兼容性：标题修复不得影响既有门槛 ──────────────────────

class TestCompatibility:
    def test_short_paragraph_still_dropped(self, agent):
        """短段落仍应被丢弃——只有标题获得豁免。"""
        html = f"<article><p>短</p><p>{LONG_PARA}</p></article>"
        text = agent._extract_text(html)
        assert "\n短\n" not in text
        assert "# 短" not in text

    def test_link_only_block_still_dropped(self, agent):
        links = " ".join(f"https://spam{i}.com/x" for i in range(30))
        html = (
            f"<article><h2>链接段</h2><div>{links}</div>"
            f"<p>{P1}</p><p>{P2}</p><p>{P3}</p><p>{P4}</p></article>"
        )
        text = agent._extract_text(html)
        assert "spam0.com" not in text
        assert "## 链接段" in text

    def test_navigation_headings_still_removed(self, agent):
        """nav 里的短标题不得混入正文（降噪不得被标题豁免破坏）。"""
        html = (
            "<html><body><nav><h2>首页</h2><h2>新闻</h2></nav>"
            f"<article><h2>真标题</h2><p>{LONG_PARA}</p></article></body></html>"
        )
        text = agent._extract_text(html)
        assert "## 真标题" in text
        assert "## 首页" not in text

    def test_code_block_extraction_unaffected(self, agent):
        """代码块修复与标题修复必须同时生效。"""
        code = "def f():\n    return 1"
        html = (
            "<html><body><article><h2>示例</h2>"
            f"<pre><code>{code}</code></pre>"
            f"<p>{LONG_PARA}</p></article></body></html>"
        )
        for text in (agent._extract_text(html), agent._extract_text_regex(html)):
            assert "## 示例" in text
            assert "\n    return 1" in text
            assert "return 1" in text
            assert "def f():" in text
