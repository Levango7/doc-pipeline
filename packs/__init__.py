"""docgen pack - 文档生成领域件（writer/quality_gate/checker/layout/fact_checker + docgen 系流水线 + quality/prompts）。

2026-10-11 从核心 agents/ 与 pipelines/ 拆出（spec §5.2 pack 机制 v1 的物理论证）。
引擎（loom_core）不 import 本包；本包经 pack 清单被发现与装配。
"""
