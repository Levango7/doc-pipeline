# 渲染层可行性验证（spike）

**日期**：2026-10-03 · **结论**：**GO** —— Markdown → docx / pdf 两条路线均可行，
渲染耗时在百毫秒级，中文与标题层次保真。升级为多格式文档系统的核心技术前提成立。

## 怎么跑

```bash
pip install python-docx reportlab pymupdf
python spike/render_spike.py      # 解析 Markdown → 产出 docx + pdf
python spike/render_pages.py      # pdf 渲染成 png（人工肉眼核对用）
python spike/check_fidelity.py    # 程序化核对保真度指标
```

输入样本用管线**真实产出**的 `output/smoke_20260830.md`，不是玩具样本——
这样才能暴露真实数据里的脏问题（见下方"实测数据"）。

## 实测数据

| 指标 | 结果 |
|---|---|
| 输入 | `smoke_20260830.md`，6,589 字符 → 解析为 43 个块 |
| docx | 38.5 KB，38 ms，37 非空段落 |
| pdf | 11.6 KB，22 ms，4 页 A4 |
| 中文字符保留 | **100%**（源 1,885 → PDF 1,885，无丢字/豆腐块） |
| PDF 嵌入字体 | `STSong-Light`（CID）+ `Helvetica` |
| 标题层级 | PDF 5 种字号梯度 / docx 8 个 Word 标题段落 |
| 中文列表符号 | 6 个 `•` 保留 |
| docx 中文字体 | 样式层钉死`微软雅黑`（`w:eastAsia`） |

**docx 落的是真正的 Word 标题样式**（`Title` / `Heading 1` / `List Bullet`），
不是加粗的普通段落——这是关键：意味着 Word 能自动生成目录、导航窗格可跳转、
用户可二次编辑，文档不是"图片式"的死文件。

## 两条路线的取舍

| | docx（python-docx / OOXML） | pdf（ReportLab） |
|---|---|---|
| 部署| 纯 Python，零原生依赖 | 纯 Python（内置 CID 字体） |
| 中文 | 需手动钉 `w:eastAsia`，否则回退宋体 | 需注册 CID 字体家族映射 |
| 可编辑 | 是（Word 原生） | 否（只读） |
| 适用 | 要交付/二次编辑的文档 | 要打印/归档/送审 |

两者**互补而非替代**：docx 给"活文档"，pdf 给"定版归档"。

## 踩坑记录（省得后人重踩）

1. **reportlab 5.x 用中文字体要过三道关卡**，缺一即抛 `ValueError`/`KeyError`：
   - `pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))` —— 注册 face
   - `fonts._ps2tt_map["stsong-light"] = ("stsong-light", False, False)` —— 正向映射
   - `fonts._tt2ps_map[("stsong-light", False, False)] = "STSong-Light"` —— 反向映射
   reportlab 5 的映射表**只有 13 个西文家族**，任何 CID 中文字体都不在内。
   从 `Heading1/2/3` 继承样式**无效**（父样式构造期就已解析字体家族），
   必须从 `BodyText` 继承再显式覆盖。CID 字体无独立粗体字重，
   标题层次只能用字号 + 颜色 + 间距表达。
2. **python-docx 不自动设东亚字体**。`run.font.name = x` 只写 `w:ascii`/`w:hAnsi`，
   中文字符走 `w:eastAsia`，缺省回退宋体 → 跨机排版不一致。
   长文档应在**样式层**设一次（`style.element.rPr.rFonts`），
   而非逐 run 设置，否则产生海量 `<w:rFonts>`。
3. **docx 是 zip**，检查 XML 必须 `zipfile` 解包读 `word/styles.xml`，
   直接 `read_bytes().decode()` 当纯文本读会误报"字体未设置"。
4. **ReportLab 的 `Paragraph` 不吃换行符**，代码块要逐行拆成独立 Paragraph，
   否则整块塌成一行。
5. **核对排版保真度优先用程序化指标**（字符保留率、字号梯度、样式分布、
   XML 属性落地情况），比肉眼看图更可靠、更适合进 CI。

## ⚠️ 验证中暴露的真实数据缺陷

管线自身的产出质量问题，与渲染层无关但**必须先修**：

```
[!] 检出 2 个超长段落（>1500 字符），最长 2,258 字符
```

**`agents/fetcher.py` 的正文提取没有剥离代码块的换行与缩进**，
导致大段代码被压成一坨连续文本（`asyncio . run ( main ( ) )` 这样）。
这会毁掉任何格式转换的保真度——渲染层再强也救不回已经塌陷的输入。

**含义**：渲染层可行 ✅，但**上游正文提取是下一个必修项**。

---

## 正文提取修复（2026-10-03 已完成）

上述缺陷已修复。`agents/fetcher.py` 现在把 `<pre>` 代码块单独摘出、
保留换行与缩进，转成 Markdown 围栏代码块后再拼回正文尾部。

### 修复前后对比

```
修复前：asyncio . run ( main ( ) )   ✅ 解析：loop.run_in_executor : ...
修复后：
        async def main():
            tasks = [asyncio.create_task(worker(i)) for i in range(3)]
            await asyncio.gather(*tasks)
```

### 一并修掉的两个既有缺陷

1. **正文重复**：CSS 选择器同时匹配容器标签（`article`/`section`/`div`）
   和其子节点（`p`/`li`），父节点文本本身就是所有子节点文本的拼接，
   同一段文字被收集两遍。新增 `_dedupe_blocks()` 去重。
2. **API 缺陷**：`_extract_text_regex(html, code_text=...)` 早前实现是
   "传入 code_text 就跳过摘除 `<pre>`"，导致 `<pre>` 残留 → 代码以压平形态
   混进正文，与保真副本重复。改为**始终重新摘除**，`code_text` 仅作复用。

### 端到端闭环验证

`spike/check_e2e.py`：真实 HTML → fetcher 提取 → docx/pdf 渲染。

```
→ pdf 含缩进代码行: 是
→ pdf 含压平痕迹  : 否
```

### 新增护栏

`tests/test_fetcher_code_blocks.py`，33 个用例（两条提取路径参数化覆盖）：

- 换行 / 缩进 / 围栏 / 语言标注保住
- `&lt;` `&nbsp;` 等实体在代码里正确还原
- 代码自身含 ``` 时外层围栏自动加长
- 重复代码去重（同一段常被多处嵌入）
- `<pre>` 摘除后相邻文本不粘连
- 回归护栏：`asyncio . run` / `main ( )` 等压平痕迹不得出现
- `script`/`style` 噪音过滤不得被放宽

### 额外发现（非本修复引入）

短标题（<30 字符）会被密度法过滤掉，`<h1>`/`<h2>` 文字会并入相邻正文，
**标题语义在提取阶段就丢了**。这是 `MIN_CONTENT_LENGTH = 200` 的既有行为，
待后续处理（渲染层已经能正确渲染 h1/h2/h3，只要上游传下来就行）。

### 踩坑补记

- **ReportLab `wordWrap="CJK"` 会吞掉行首空格** → 代码缩进全部消失。
  解法：前导空格转 `&nbsp;` 再做 XML 转义（`_preserve_indent`）。
- 围栏长度检测别用 `line.split("`")[0]` 取长度——那是拿不到反引号数量的，
  要用正则 `^([`]+)` 直接捕获。