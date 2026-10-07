"""验证哈希嵌入的相似度排序是否合理（知识库的基础）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from artesian import embeddings as E  # noqa: E402


def main() -> None:
    print("可用后端:", E.available_embedders())
    emb = E.get_embedder("hash")
    print(f"后端: {emb.name}  dim: {emb.dim}\n")

    query = "Python 异步编程的协程调度"
    candidates = [
        "本文介绍 asyncio 协程与事件循环的调度机制",     # 高度相关
        "Python 异步编程中 await 的用法",                # 相关
        "数据库索引优化与查询计划分析",                   # 无关
        "今天天气不错，适合出去散步",                     # 完全无关
    ]

    qv = emb.embed_one(query)
    print(f"查询: {query}\n")
    scored = [(E.cosine(qv, emb.embed_one(c)), c) for c in candidates]
    for score, text in sorted(scored, reverse=True):
        print(f"  {score:.4f}  {text}")

    print("\n=== 排序正确性（相关项应高于无关项）:")
    ok = scored[0][0] > scored[-1][0] and scored[1][0] > scored[2][0]
    print("  通过" if ok else "  未通过")

    print("\n=== 确定性（跨进程稳定的前提）:")
    v1 = emb.embed_one("测试文本 determinism")
    v2 = emb.embed_one("测试文本 determinism")
    print(f"  同进程两次一致: {v1 == v2}")

    print("\n=== 长度无关性（L2 归一化）:")
    short = emb.embed_one("数据库索引优化")
    long = emb.embed_one("数据库索引优化 " + "补充说明内容 " * 20)
    print(f"  短文本自相似: {E.cosine(short, short):.4f}")
    print(f"  长短相似度  : {E.cosine(short, long):.4f}")

    print("\n=== 归一化校验:")
    norm = sum(x * x for x in v1) ** 0.5
    print(f"  L2 范数: {norm:.6f} (应≈1.0)")

    print("\n=== 打包往返:")
    blob = E.pack_vector(v1)
    back = E.unpack_vector(blob)
    print(f"  原始 {len(v1)} 维 → {len(blob)} 字节 → 还原 {len(back)} 维")
    print(f"  往返一致: {all(abs(a-b) < 1e-6 for a, b in zip(v1, back, strict=False))}")


if __name__ == "__main__":
    main()
