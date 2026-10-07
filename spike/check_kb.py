"""验证知识库端到端：切块 → 入库 → 检索 → 持久化。

用项目真实产出的文档做样本（output/render_e2e.md），不是玩具数据。
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from artesian.knowledge_base import KnowledgeBase, chunk_markdown  # noqa: E402

DOCS = {
    "异步编程指南": """# 异步编程指南

## 事件循环

事件循环是 asyncio 的核心调度器，负责在协程之间切换执行权。
当协程遇到 await 时会挂起并把控制权交还事件循环。

## 协程

协程通过 async def 声明，是可以在执行中暂停与恢复的函数。
await 关键字用于等待另一个协程完成。

## 并发控制

使用 asyncio.gather 可以并发执行多个协程，总耗时约等于最慢的那个。
信号量 Semaphore 用于限制并发度，避免压垮下游服务。
""",
    "数据库优化手册": """# 数据库优化手册

## 索引设计

B+ 树索引适合范围查询，哈希索引只支持等值查询。
组合索引遵循最左前缀原则，顺序设计错误会导致索引失效。

## 查询计划

EXPLAIN 可以查看查询计划，重点关注全表扫描与临时表。
慢查询日志是定位性能问题的主要入口。

## 分库分表

水平拆分解决单表数据量过大，垂直拆分解决单库连接数瓶颈。
拆分后需要处理分布式事务与跨库 join 的问题。
""",
}


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kb_check_"))
    try:
        # ── 1. 切块结构感知 ──
        print("=" * 60)
        print("1. 切块（结构感知）")
        chunks = chunk_markdown(DOCS["异步编程指南"])
        print(f"   块数: {len(chunks)}")
        for c in chunks:
            path = c["heading_path"] or "(无标题)"
            preview = c["content"].replace("\n", " ")[:52]
            print(f"   [{path}] {preview}…")

        # ── 2. 入库 ──
        print()
        print("=" * 60)
        print("2. 入库")
        kb = KnowledgeBase(tmp / "kb.db", embedder_name="hash")
        for title, text in DOCS.items():
            res = kb.add_document(text, source=f"notes/{title}.md", title=title)
            print(f"   {title}: {res['status']} 块数={res.get('chunks')} "
                  f"字符={res.get('chars')}")
        print(f"   统计: {kb.stats()}")

        # ── 3. 检索 ──
        print()
        print("=" * 60)
        print("3. 检索")
        for query in ["如何用信号量限制并发", "索引为什么失效", "B+树和哈希索引区别"]:
            res = kb.search(query, top_k=2)
            print(f"\n   查询: {query}  (扫描 {res['scanned']} 块)")
            for r in res["results"]:
                print(f"     {r['score']:.4f} [{r['title']} > {r['heading_path']}]")
                print(f"            {r['content'][:60].replace(chr(10), ' ')}…")

        # ── 4. 跨文档区分能力 ──
        print()
        print("=" * 60)
        print("4. 跨文档区分（应各自命中正确文档）")
        res = kb.search("分布式事务怎么处理", top_k=1)
        top = res["results"][0] if res["results"] else None
        print(f"   查询'分布式事务' → 命中文档: {top['title'] if top else '无'}")
        print(f"   期望: 数据库优化手册 → "
              f"{'通过' if top and top['title'] == '数据库优化手册' else '未通过'}")

        # ── 5. 结果多样性（max_per_doc）──
        print()
        print("=" * 60)
        print("5. 多样性控制")
        r_all = kb.search("索引", top_k=4)
        r_div = kb.search("索引", top_k=4, max_per_doc=1)
        print(f"   不限: {[x['title'][:4] for x in r_all['results']]}")
        print(f"   限1 : {[x['title'][:4] for x in r_div['results']]}")

        # ── 6. 持久化 ──
        print()
        print("=" * 60)
        print("6. 持久化（关闭后重开）")
        kb.close_all()
        kb2 = KnowledgeBase(tmp / "kb.db", embedder_name="hash")
        res = kb2.search("协程和await", top_k=1)
        print(f"   重开后检索: {res['status']} 命中={len(res['results'])}")
        print(f"   统计: {kb2.stats()['documents']} 文档 / "
              f"{kb2.stats()['chunks']} 块")

        # ── 7. 嵌入器不匹配检测 ──
        print()
        print("=" * 60)
        print("7. 换嵌入器的保护（不能静默给垃圾结果）")
        kb3 = KnowledgeBase(tmp / "kb.db", embedder_name="hash", dim=256)
        res = kb3.search("索引")
        print(f"   status={res['status']}")
        print(f"   message={res.get('message', '(无)')[:100]}")

        # ── 8. 同源替换（资料更新不留过期块）──
        print()
        print("=" * 60)
        print("8. 同源替换")
        before = kb2.stats()["chunks"]
        kb2.add_document("# 新版\n\n完全不同的新内容，旧版本应当被清理。",
                        source="notes/异步编程指南.md", title="异步编程指南")
        after = kb2.stats()["chunks"]
        print(f"   替换前 {before} 块 → 替换后 {after} 块（应大幅减少）")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
