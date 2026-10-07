"""验证 rebuild() 与真实语义嵌入后端（sentence-transformers）。"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from artesian import embeddings as E  # noqa: E402
from artesian.knowledge_base import KnowledgeBase  # noqa: E402

DOC = """# 产品运营手册

## 收入结构

公司本季度营收较去年同期增长百分之十二，主要来自订阅业务。

## 用户增长

月活跃用户数突破三百万，新增用户主要来自自然流量。
"""


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="kb_rebuild_"))
    try:
        # ── rebuild：换维度后重建 ──
        print("=" * 60)
        print("1. rebuild() 换维度重建")
        kb = KnowledgeBase(tmp / "kb.db", embedder_name="hash", dim=1024)
        kb.add_document(DOC, source="ops.md", title="运营手册")
        print(f"   初始: {kb.stats()['chunks']} 块, "
              f"embedders={kb.stats()['embedders']}")

        res = kb.search("营收增长")
        print(f"   重建前检索: {res['status']}, 命中 {len(res['results'])}")

        # 换成 256 维（此时检索应报错）
        kb_small = KnowledgeBase(tmp / "kb.db", embedder_name="hash", dim=256)
        print(f"   换 256 维后检索: {kb_small.search('营收增长')['status']} "
              f"(应为 error)")

        # 重建
        r = kb_small.rebuild()
        print(f"   rebuild: {r['status']} 重建 {r['rebuilt']} 块 → "
              f"{r['embedder']}")
        res = kb_small.search("营收增长")
        print(f"   重建后检索: {res['status']}, 命中 {len(res['results'])}")
        if res["results"]:
            print(f"     top: {res['results'][0]['content'][:50]}…")

        # ── 语义后端（需联网下载模型，默认跳过）──
        print()
        print("=" * 60)
        print("2. auto 回落行为（网络受限环境的关键保护）")
        import time as _t
        t0 = _t.time()
        emb = E.get_embedder("auto")
        cost = _t.time() - t0
        print(f"   auto 选中: {emb.name}  (耗时 {cost:.1f}s)")
        reasons = E.auto_fallback_reasons()
        if reasons:
            print("   回落原因:")
            for k, v in reasons.items():
                print(f"     - {k}: {v[:110]}")
        assert isinstance(emb, E.Embedder), "auto 必须总能返回可用后端"

        print()
        print("=" * 60)
        print("3. 真实语义嵌入（local 后端，需联网，默认跳过）")
        if os.environ.get("TEST_LOCAL_EMBEDDER") != "1":
            print("   未设 TEST_LOCAL_EMBEDDER=1，跳过（模型需从 HF 下载）")
            return
        try:
            t0 = time.time()
            local = E.get_embedder("local")
            cost = time.time() - t0
            print(f"   模型加载: {cost:.1f}s  维度: {local.dim}  "
                  f"identity: {local.name}")
        except Exception as e:
            print(f"   加载失败: {str(e)[:200]}")
            return

        # 语义能力对比：用词不同但意思相同
        pairs = [
            ("公司营收增长情况", "本季度收入同比上涨", "同义改写（语义应高）"),
            ("公司营收增长情况", "今天天气很好适合散步", "完全无关（语义应低）"),
        ]
        print("\n   语义 vs 词法对比:")
        hv = E.get_embedder("hash")
        for a, b, label in pairs:
            s_sem = E.cosine(local.embed_one(a), local.embed_one(b))
            s_lex = E.cosine(hv.embed_one(a), hv.embed_one(b))
            print(f"   {label}")
            print(f"     语义(local)={s_sem:.4f}   词法(hash)={s_lex:.4f}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
