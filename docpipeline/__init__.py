"""docpipeline - 文档领域层

放在流水线引擎**之上**的一组能力：把内容变成文档（渲染、摄入、增强）。
与 `pipeline_core` 的依赖方向是单向的：

    docpipeline  ->  pipeline_core        （允许）
    pipeline_core ->  docpipeline         （禁止，见 tests/test_layering.py）

`pipeline_core` 是领域无关的引擎（DAG、消息总线、重试、熔断、检查点），
它不应该知道自己的工作对象是不是文档。一旦引擎反向 import 本包，
"自托管工作流引擎"就退化成"只能做文档的脚本集合"，
也挡住了后续接入非文档类工作流。

本包不做顶层 re-export。可选重依赖（python-docx、PDF 后端）本来就在函数内惰性
import，`import docpipeline` 并不贵；真正的问题是打桩入口：顶层 re-export 会让
`docpipeline.render` 和 `docpipeline.renderer.render` 同时存在，测试 patch 前者
对后者无效，是典型的假绿来源。请总是按模块路径 import。

模块：
  - renderer: Markdown -> docx / pdf
  - ingest: PDF / 图片 / 纯文本 -> 结构化 Markdown
  - document_enhancer: 对已有 Markdown 逐章节 LLM 增强
"""
