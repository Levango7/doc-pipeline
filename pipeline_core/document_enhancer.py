"""
DocumentEnhancer - 已有文档增强模块
=====================================
对已有 Markdown 文档进行逐章节 LLM 增强，支持：
  - 逐章节内容深化（保持结构，丰富细节）
  - 搜索引擎补充最新资料
  - ASCII 图修复
  - 格式转换导出（HTML/Word）

用法：
  from pipeline_core.document_enhancer import DocumentEnhancer
  enhancer = DocumentEnhancer()
  result = enhancer.enhance("input.md", output_dir="output/")
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

from pipeline_core.llm_router import get_router
from pipeline_core.search_engines import SearchEngineManager

logger = logging.getLogger(__name__)

# 增强 LLM 提示词模板
ENHANCE_PROMPT = """你是一位资深技术文档审阅专家。请对以下 Markdown 章节内容进行增强优化。

## 增强要求
1. **保持结构**：原样保留所有 `### ` 和 `#### ` 标题，不修改标题文字，不添加新标题
2. **丰富细节**：在标题下方的正文段落中补充技术细节、配置示例、最佳实践
3. **修正错误**：修正已知的不准确或过时信息
4. **提升可读性**：优化表达，使中文更流畅自然
5. **保留格式**：原样保留代码块、表格、列表、Mermaid 图、分割线(---)等格式，不修改

## 严格禁止
- 禁止生成任何 `## ` 或 `### ` 开头的标题行
- 禁止修改任何已有的 `### `、`#### ` 标题文字（包括标题编号）
- 禁止修改代码块中的任何内容
- 禁止在代码块内插入或删除任何字符
- 禁止添加"增强后"、"优化后"等元描述文字
- 禁止重复输出原标题

## 原始内容
{content}

## 输出要求
直接输出增强后的 Markdown 内容，不要添加任何解释性文字。
"""

# 搜索增强提示词（结合搜索结果）
ENHANCE_WITH_SEARCH_PROMPT = """你是一位资深技术文档审阅专家。请参考以下搜索结果，对 Markdown 章节内容进行增强优化。

## 增强要求
1. **保持结构**：原样保留所有 `### ` 和 `#### ` 标题，不修改标题文字，不添加新标题
2. **结合搜索**：将搜索结果中的最新信息融入内容
3. **丰富细节**：在标题下方的正文段落中补充技术细节、配置示例、最佳实践
4. **修正错误**：修正已知的不准确或过时信息
5. **提升可读性**：优化表达，使中文更流畅自然
6. **保留格式**：原样保留代码块、表格、列表、Mermaid 图、分割线(---)等格式，不修改

## 严格禁止
- 禁止生成任何 `## ` 或 `### ` 开头的标题行
- 禁止修改任何已有的 `### `、`#### ` 标题文字（包括标题编号）
- 禁止修改代码块中的任何内容
- 禁止在代码块内插入或删除任何字符
- 禁止添加"增强后"、"优化后"等元描述文字
- 禁止重复输出原标题

## 搜索结果（供参考）
{search_results}

## 原始内容
{content}

## 输出要求
直接输出增强后的 Markdown 内容，不要添加任何解释性文字。
"""


class DocumentEnhancer:
    """已有文档增强器"""

    def __init__(self):
        self._llm_router = get_router()
        self._search_mgr = SearchEngineManager.from_env()
        self._stats = {"sections": 0, "enhanced": 0, "searched": 0, "ascii_fixed": 0,
                       "fake_headings_removed": 0}

    def enhance(
        self,
        input_path: str,
        output_dir: str = "output",
        with_search: bool = True,
        max_search_results: int = 5,
    ) -> dict:
        """
        增强已有 Markdown 文档

        Args:
            input_path: 输入文档路径
            output_dir: 输出目录
            with_search: 是否启用搜索补充
            max_search_results: 搜索最大结果数

        Returns:
            {"status": "success", "output_path": str, "stats": dict, "duration": float}
        """
        start = time.time()
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # 1. 读取文档
        print(f"[Enhancer] 读取文档: {input_path}")
        content = input_path.read_text(encoding="utf-8")

        # 2. 解析章节
        sections = self._parse_sections(content)
        self._stats["sections"] = len(sections)
        print(f"[Enhancer] 解析到 {len(sections)} 个章节")

        # 3. 收集原始文档的 ## 标题（用于后续全局清理的精确匹配）
        original_titles = set(title for title, _ in sections if title.startswith("## "))

        # 4. 逐章节增强
        enhanced_sections = []
        for i, (title, body) in enumerate(sections):
            print(f"[Enhancer] [{i+1}/{len(sections)}] 增强: {title[:60]}")
            enhanced_body = self._enhance_section(title, body, with_search, max_search_results)
            enhanced_sections.append((title, enhanced_body))
            self._stats["enhanced"] += 1

        # 5. 重组文档
        print(f"[Enhancer] 重组文档...")
        enhanced_content = self._reassemble(enhanced_sections)

        # 6. 全局清理虚假标题（使用原始标题集合精确匹配）
        enhanced_content, removed = self._clean_fake_headings(enhanced_content, original_titles)
        self._stats["fake_headings_removed"] = removed
        if removed > 0:
            print(f"[Enhancer] 清理了 {removed} 个虚假标题")

        # 7. 修复 ASCII 图
        print(f"[Enhancer] 修复 ASCII 图...")
        enhanced_content = self._fix_ascii(enhanced_content)

        # 8. 写入输出
        stem = input_path.stem
        output_path = output_dir / f"{stem}_enhanced.md"
        output_path.write_text(enhanced_content, encoding="utf-8")
        print(f"[Enhancer] 增强文档已写入: {output_path}")

        duration = time.time() - start
        return {
            "status": "success",
            "output_path": str(output_path),
            "stats": dict(self._stats),
            "duration": round(duration, 1),
        }

    def _parse_sections(self, content: str) -> list[tuple[str, str]]:
        """按 ## 标题拆分章节，返回 [(title, body), ...]"""
        # 提取文档标题（第一个 # 行）和后续内容
        lines = content.splitlines()
        title_line = ""
        body_start = 0

        for i, line in enumerate(lines):
            if line.startswith("# ") and not line.startswith("## "):
                title_line = line
                body_start = i + 1
                break

        # 按 ## 拆分
        body = "\n".join(lines[body_start:])
        pattern = re.compile(r"^(## .+)$", re.MULTILINE)
        splits = pattern.split(body)

        sections = []
        if title_line:
            sections.append((title_line, ""))  # 文档标题作为第一个"章节"

        # 处理拆分结果
        current_title = ""
        for part in splits:
            if part.startswith("## "):
                current_title = part
            elif current_title:
                sections.append((current_title, part.strip()))
                current_title = ""

        return sections

    MAX_CHUNK_SIZE = 3000  # 单次 LLM 调用最大内容长度

    def _enhance_section(
        self, title: str, body: str, with_search: bool, max_results: int
    ) -> str:
        """增强单个章节（超长内容自动分块）"""
        if not body.strip():
            print(f"  -> 跳过（空内容）")
            return body

        # 跳过过短章节
        if len(body.strip()) < 50:
            print(f"  -> 跳过（内容过短: {len(body.strip())} 字符）")
            return body

        # 搜索补充资料
        search_results = ""
        if with_search and self._search_mgr.is_available():
            try:
                query = f"{title} 技术详解 最佳实践"
                results = self._search_mgr.search(query, max_results=max_results)
                if results:
                    search_results = "\n".join(
                        f"- [{r.get('title', '')}]({r.get('url', '')}) : {r.get('snippet', '')[:200]}"
                        for r in results[:max_results]
                    )
                    self._stats["searched"] += 1
            except Exception as e:
                print(f"  -> 搜索失败: {e}")

        # 超长内容分块处理
        if len(body) > self.MAX_CHUNK_SIZE:
            print(f"  -> 超长内容 ({len(body)} 字符)，分块处理...")
            return self._enhance_long_section(title, body, search_results)

        print(f"  -> LLM 增强中 ({len(body)} 字符)...")
        enhanced = self._call_llm_enhance(body, search_results)
        return self._clean_llm_output(enhanced)

    def _enhance_long_section(self, title: str, body: str, search_results: str) -> str:
        """对超长章节按 ### 子标题分块增强"""
        # 尝试按 ### 子标题拆分
        sub_pattern = re.compile(r"^(### .+)$", re.MULTILINE)
        sub_splits = sub_pattern.split(body)

        if len(sub_splits) <= 1:
            # 无子标题，直接截断
            original_body = body
            body = body[: self.MAX_CHUNK_SIZE] + "\n\n> [内容过长，已截断]"
            enhanced = self._call_llm_enhance(body, search_results)
            # LLM 失败时返回原始完整内容，而非截断版本
            if enhanced is body:
                return original_body
            return self._clean_llm_output(enhanced)

        # 逐子章节增强
        enhanced_parts = []
        current_subtitle = ""
        for part in sub_splits:
            if part.startswith("### "):
                current_subtitle = part
            elif current_subtitle:
                original_chunk = part.strip()
                chunk = original_chunk
                if len(chunk) > self.MAX_CHUNK_SIZE:
                    chunk = chunk[: self.MAX_CHUNK_SIZE] + "\n\n> [内容过长，已截断]"
                print(f"    -> 子章节 LLM 增强 ({len(chunk)} 字符)...")
                enhanced = self._call_llm_enhance(chunk, search_results)
                # LLM 失败时返回原始完整内容，而非截断版本
                if enhanced is chunk:
                    enhanced = original_chunk
                else:
                    enhanced = self._clean_llm_output(enhanced)
                    enhanced = self._restore_missing_h4(original_chunk, enhanced)
                    enhanced = self._restore_code_fences(original_chunk, enhanced)
                enhanced_parts.append(f"{current_subtitle}\n\n{enhanced}")
                current_subtitle = ""

        return "\n\n".join(enhanced_parts)


    def _restore_code_fences(self, original: str, enhanced: str) -> str:
        """检查 LLM 增强后是否丢失代码块 fence，若丢失则回退到原始内容"""
        orig_fences = original.count('
```')
        enh_fences = enhanced.count('
```')
        if orig_fences != enh_fences:
            logger.warning(
                "LLM 增强丢失代码块 fence (原%d个, 现%d个)，回退到原始内容",
                orig_fences, enh_fences
            )
            return original
        return enhanced

    def _restore_missing_h4(self, original: str, enhanced: str) -> str:
        """检查 LLM 增强后是否丢失 H4 标题，若丢失则回退到原始内容"""
        import re
        orig_h4 = set(re.findall(r'^#### .+', original, re.MULTILINE))
        enh_h4 = set(re.findall(r'^#### .+', enhanced, re.MULTILINE))
        missing = orig_h4 - enh_h4
        if missing:
            logger.warning(f"LLM 增强丢失 {len(missing)} 个 H4 标题，回退到原始内容: {missing}")
            return original
        return enhanced

    def _call_llm_enhance(self, content: str, search_results: str) -> str:
        """调用 LLM 增强单段内容"""
        try:
            if search_results:
                prompt = ENHANCE_WITH_SEARCH_PROMPT.format(
                    search_results=search_results, content=content
                )
            else:
                prompt = ENHANCE_PROMPT.format(content=content)

            messages = [{"role": "user", "content": prompt}]
            enhanced, provider = self._llm_router.chat(
                messages, max_tokens=4096, temperature=0.3, timeout=60
            )
            print(f"    -> LLM 增强完成 ({provider}): {len(content)} -> {len(enhanced)} 字符")
            return enhanced
        except Exception as e:
            print(f"    -> LLM 增强失败: {e}")
            return content  # 失败时返回原文

    def _clean_llm_output(self, content: str) -> str:
        """清理 LLM 输出中的虚假标题和元描述

        注意：只移除 ## 标题（因为 ## 是章节边界，由 _reassemble 重新添加），
        不移除 ### 标题（因为 ### 是章节内容的一部分，需要保留）。
        """
        lines = content.splitlines()
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # 跳过 LLM 生成的 ## 标题行（章节边界由外部处理）
            if stripped.startswith("## "):
                continue
            # 跳过 LLM 元描述 artifacts
            if stripped in ("增强后的 Markdown 内容", "增强后的内容", "优化后的内容",
                           "增强后内容", "以下是增强后的内容", "以下是优化后的内容"):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)

    def _clean_fake_headings(self, content: str, original_titles: set[str]) -> tuple[str, int]:
        """全局清理：移除增强后文档中不属于原始结构的 ## 标题

        Args:
            content: 增强后的文档内容
            original_titles: 原始文档中所有 ## 标题的集合（用于精确匹配）
        """
        removed = 0
        lines = content.splitlines()
        result = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                if stripped in original_titles:
                    result.append(line)
                else:
                    # 虚假标题，转为加粗文本
                    fake_title = stripped[3:]  # 去掉 "## "
                    result.append(f"**{fake_title}**")
                    removed += 1
                    print(f"  [clean] 移除虚假标题: {stripped[:60]}")
            else:
                result.append(line)

        return "\n".join(result), removed

    def _reassemble(self, sections: list[tuple[str, str]]) -> str:
        """重组增强后的文档"""
        parts = []
        for title, body in sections:
            if body:
                parts.append(f"{title}\n\n{body}")
            else:
                parts.append(title)
        return "\n\n".join(parts)

    def _fix_ascii(self, content: str) -> str:
        """修复 ASCII 图（委托给 convert_ascii 模块）"""
        try:
            from scripts.convert_ascii import convert_ascii_in_text
            fixed, count = convert_ascii_in_text(content)
            if count > 0:
                self._stats["ascii_fixed"] = count
                print(f"[Enhancer] 修复了 {count} 个 ASCII 图")
                return fixed
        except ImportError:
            print("[Enhancer] convert_ascii 模块不可用，跳过 ASCII 修复")
        except Exception as e:
            print(f"[Enhancer] ASCII 修复失败: {e}")
        return content

    def get_stats(self) -> dict:
        """获取增强统计"""
        return dict(self._stats)