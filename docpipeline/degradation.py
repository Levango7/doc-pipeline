"""文档降级产物：writer 承认"某一节没内容"时用的那套标记。

这些字符串是 writer 与 quality_gate 之间的**契约**而不是文案：门禁用它们判断
"这份产出到底有没有内容"。此前两处各写一份，于是漂移过一次 —— writer 改发
「降级声明」与「（暂无可用的相关内容）」，而门禁的名单里只有
「未采集到可整合的搜索结果」与「无待整合内容」，结果一份 4/5 章节是占位符的
文档拿到了 98.8 pass 并正常落盘（2026-10-06 实测）。故字符串收敛到本模块单处定义。
"""

from __future__ import annotations

import re

# 整份文档没有可整合素材时的自述
EMPTY_RESULT_DOC = "未采集到可整合的搜索结果"
NO_SOURCE = "无待整合内容"

# 单个章节提取不到足够内容时的占位行（成对出现：一节一行）
SECTION_PLACEHOLDER = "*（暂无可用的相关内容）*"

# 无 LLM 且存在占位章节时，writer 在文档头部插入的自述块
DEGRADE_BANNER = "> ⚠️ **降级声明**"

# 门禁据此判失败：占位符标记，命中即视为"没有产出"
PLACEHOLDER_MARKERS = (EMPTY_RESULT_DOC, NO_SOURCE)

_SECTION_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)


def placeholder_section_count(content: str) -> int:
    """正文里以占位符交出去的章节数。"""
    return content.count(SECTION_PLACEHOLDER)


def section_count(content: str) -> int:
    """二级标题数，即文档声称要交付的章节数。"""
    return len(_SECTION_HEADING_RE.findall(content))


def placeholder_ratio(content: str) -> float:
    """占位章节占比；没有章节时按"整份都是占位"计（返回 1.0 而非除零崩掉）。"""
    sections = section_count(content)
    placeholders = placeholder_section_count(content)
    if placeholders == 0:
        return 0.0
    if sections == 0:
        return 1.0
    return placeholders / sections
