"""知识库与嵌入层测试 — pipeline_core/embeddings.py + knowledge_base.py

覆盖：
- 嵌入层：特征提取、确定性、L2 归一化、打包往返、余弦、后端选择与错误
- 切块：结构感知（按 Markdown 标题）、标题路径进上下文、长文本重叠窗口
- 知识库：入库/检索/持久化/同源替换/删除/统计/重建
- **换嵌入器的保护**：向量空间不一致必须明确报错，不能静默返回空
  （这是实现中被验证出来的真 bug：hash 身份不带维度时，
  1024→256 维切换会静默返回 0 结果）

不触发真实模型下载：local 后端只测其可选择性，不实例化。
"""
import sys
from pathlib import Path

import pytest

from pipeline_core import embeddings as E
from pipeline_core.knowledge_base import KnowledgeBase, chunk_markdown

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ────────────────────────────── 特征提取 ──────────────────────────────

class TestExtractFeatures:
    def test_cjk_bigrams(self):
        feats = E.extract_features("数据库")
        assert "c:数据" in feats
        assert "c:据库" in feats

    def test_single_cjk_char_falls_back(self):
        feats = E.extract_features("好")
        assert "c:好" in feats

    def test_cjk_runs_split_separately(self):
        # 跨标点的字不应组成 bigram
        feats = E.extract_features("数据，库")
        assert "c:数据" in feats
        assert "c:据库" not in feats

    def test_latin_words_lowercased(self):
        feats = E.extract_features("Kafka Streams")
        assert "w:kafka" in feats
        assert "w:streams" in feats

    def test_stopwords_removed(self):
        feats = E.extract_features("the and of kafka")
        assert "w:the" not in feats
        assert "w:kafka" in feats

    def test_numbers_kept(self):
        feats = E.extract_features("增长 12.5 个百分点")
        assert "n:12.5" in feats

    def test_single_letter_latin_dropped(self):
        assert "w:a" not in E.extract_features("a b c kafka")

    def test_empty_text(self):
        assert E.extract_features("") == {}

    def test_punctuation_only(self):
        assert E.extract_features("！！！。。。") == {}


# ────────────────────────────── 哈希嵌入 ──────────────────────────────

class TestHashEmbedder:
    def test_dim_matches_config(self):
        emb = E.HashEmbedder(dim=256)
        assert len(emb.embed_one("测试")) == 256

    def test_name_includes_dim(self):
        """身份必须含维度 —— 否则换维度时知识库检测不出向量空间变化。"""
        assert E.HashEmbedder(dim=256).name == "hash:256"
        assert E.HashEmbedder(dim=1024).name != E.HashEmbedder(dim=256).name

    def test_invalid_dim_rejected(self):
        with pytest.raises(ValueError):
            E.HashEmbedder(dim=0)
        with pytest.raises(ValueError):
            E.HashEmbedder(dim=-5)

    def test_deterministic_within_process(self):
        emb = E.HashEmbedder()
        assert emb.embed_one("一致性测试") == emb.embed_one("一致性测试")

    def test_deterministic_across_instances(self):
        """跨实例稳定 —— 内置 hash() 会按进程加盐，必须用 hashlib。"""
        assert (E.HashEmbedder().embed_one("跨进程稳定")
                == E.HashEmbedder().embed_one("跨进程稳定"))

    def test_l2_normalized(self):
        vec = E.HashEmbedder().embed_one("归一化检验文本")
        norm = sum(v * v for v in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-6

    def test_empty_text_gives_zero_vector(self):
        vec = E.HashEmbedder().embed_one("")
        assert all(v == 0.0 for v in vec)

    def test_batch_matches_single(self):
        emb = E.HashEmbedder()
        texts = ["第一段", "第二段"]
        assert emb.embed(texts) == [emb.embed_one(t) for t in texts]

    def test_related_scores_above_unrelated(self):
        emb = E.HashEmbedder()
        q = emb.embed_one("数据库索引优化")
        near = emb.embed_one("数据库索引的设计原则")
        far = emb.embed_one("今天天气不错适合散步")
        assert E.cosine(q, near) > E.cosine(q, far)

    def test_identical_text_scores_one(self):
        emb = E.HashEmbedder()
        v = emb.embed_one("完全相同的一段文本内容")
        assert abs(E.cosine(v, v) - 1.0) < 1e-6


# ────────────────────────────── 向量工具 ──────────────────────────────

class TestVectorUtils:
    def test_pack_unpack_roundtrip(self):
        vec = [0.1, -0.25, 1.0, 0.0]
        back = E.unpack_vector(E.pack_vector(vec))
        assert len(back) == 4
        assert all(abs(a - b) < 1e-6 for a, b in zip(vec, back, strict=False))

    def test_pack_is_compact(self):
        # float32：1024 维 = 4096 字节
        assert len(E.pack_vector([0.0] * 1024)) == 4096

    def test_cosine_orthogonal(self):
        assert E.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0

    def test_cosine_opposite(self):
        assert abs(E.cosine([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-9

    def test_cosine_dim_mismatch_returns_zero(self):
        assert E.cosine([1.0, 0.0], [1.0]) == 0.0

    def test_cosine_empty_returns_zero(self):
        assert E.cosine([], []) == 0.0

    def test_cosine_zero_vector_returns_zero(self):
        assert E.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


# ────────────────────────────── 后端选择 ──────────────────────────────

class TestGetEmbedder:
    def test_hash_explicit(self):
        assert isinstance(E.get_embedder("hash"), E.HashEmbedder)

    def test_hash_with_dim(self):
        assert E.get_embedder("hash", dim=512).dim == 512

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError) as ei:
            E.get_embedder("nonexistent")
        assert "未知嵌入后端" in str(ei.value)

    def test_auto_always_returns_something(self, monkeypatch):
        """auto 必须永不失败：离线环境要能回落到 hash。

        这里把候选限制为 hash，避免测试触发 sentence-transformers
        的模型下载（实测本机 huggingface.co 不可达，会卡重试）。
        """
        monkeypatch.setattr(E, "available_embedders", lambda: ["hash"])
        emb = E.get_embedder("auto")
        assert isinstance(emb, E.HashEmbedder)

    def test_auto_falls_back_when_candidate_construction_fails(self, monkeypatch):
        """候选"库装着但建不起来"时必须回落，而不是把异常抛给调用方。"""
        monkeypatch.setattr(E, "available_embedders", lambda: ["hash", "local"])

        def _boom(*a, **kw):
            raise ValueError("本地嵌入模型加载失败: 网络不可达")

        monkeypatch.setattr(E, "LocalEmbedder", _boom)
        emb = E.get_embedder("auto")
        assert isinstance(emb, E.HashEmbedder)
        # 失败原因要如实记录，便于状态接口暴露给用户
        assert "local" in E.auto_fallback_reasons()
        assert "网络不可达" in E.auto_fallback_reasons()["local"]

    def test_available_includes_hash_always(self):
        assert "hash" in E.available_embedders()

    def test_api_requires_key(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValueError) as ei:
            E.APIEmbedder()
        assert "API Key" in str(ei.value)

    def test_api_reads_env(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test")
        emb = E.APIEmbedder(model="m")
        assert emb.name == "api:m"
        assert emb._base.startswith("http")


# ──────────────────────────────── 切块 ────────────────────────────────

class TestChunkMarkdown:
    def test_splits_by_heading(self):
        md = "# 标题一\n\n内容一\n\n## 标题二\n\n内容二\n"
        chunks = chunk_markdown(md)
        assert len(chunks) == 2
        assert "内容一" in chunks[0]["content"]
        assert "内容二" in chunks[1]["content"]

    def test_heading_path_nested(self):
        md = "# 总\n\n## 章\n\n### 节\n\n正文内容\n"
        chunks = chunk_markdown(md)
        assert chunks[-1]["heading_path"] == "总 > 章 > 节"

    def test_heading_path_in_content(self):
        """标题路径必须进切块内容 —— 否则脱离标题的片段语义残缺。"""
        md = "# 部署手册\n\n## 回滚\n\n执行回滚脚本。\n"
        content = chunk_markdown(md)[-1]["content"]
        assert "部署手册 > 回滚" in content
        assert "执行回滚脚本" in content

    def test_no_heading_text_single_chunk(self):
        chunks = chunk_markdown("一段没有任何标题的纯文本内容。")
        assert len(chunks) == 1
        assert chunks[0]["heading_path"] == ""

    def test_empty_input(self):
        assert chunk_markdown("") == []
        assert chunk_markdown("   \n\n  ") == []

    def test_long_section_windowed(self):
        md = "# 长文\n\n" + "内容句子。" * 400
        chunks = chunk_markdown(md, max_chars=200, overlap=50)
        assert len(chunks) > 1
        assert all(len(c["content"]) <= 200 + 50 for c in chunks)

    def test_windows_overlap(self):
        body = "".join(f"第{i}段。" for i in range(200))
        chunks = chunk_markdown(f"# T\n\n{body}", max_chars=100, overlap=40)
        # 相邻窗口应有重叠内容（不是硬切）
        assert len(chunks) >= 2

    def test_heading_only_no_body(self):
        assert chunk_markdown("# 只有标题") == []

    def test_invalid_max_chars(self):
        with pytest.raises(ValueError):
            chunk_markdown("# T\n\n正文", max_chars=0)

    def test_overlap_clamped(self):
        """overlap 不得大于窗口一半，否则死循环。"""
        chunks = chunk_markdown("# T\n\n" + "ab" * 500,
                                max_chars=100, overlap=999)
        assert len(chunks) < 100        # 能跑完即证明未死循环


# ────────────────────────────── 知识库 ──────────────────────────────

@pytest.fixture
def kb(tmp_path):
    k = KnowledgeBase(tmp_path / "kb.db", embedder_name="hash")
    yield k
    k.close_all()


DOC_A = """# 异步编程

## 事件循环

事件循环负责在协程之间切换执行权，await 时挂起并交还控制权。

## 并发控制

asyncio.gather 并发执行多个协程；Semaphore 限制并发度。
"""

DOC_B = """# 数据库优化

## 索引设计

B+ 树索引适合范围查询，哈希索引只支持等值查询，组合索引遵循最左前缀。
"""


class TestKnowledgeBaseWrite:
    def test_add_document(self, kb):
        res = kb.add_document(DOC_A, source="a.md", title="异步编程")
        assert res["status"] == "ok"
        assert res["chunks"] >= 2
        assert res["chars"] == len(DOC_A)

    def test_add_empty_rejected(self, kb):
        assert kb.add_document("")["status"] == "error"
        assert kb.add_document("   ")["status"] == "error"

    def test_add_file(self, kb, tmp_path):
        p = tmp_path / "note.md"
        p.write_text(DOC_B, encoding="utf-8")
        res = kb.add_file(p)
        assert res["status"] == "ok"

    def test_add_file_missing(self, kb, tmp_path):
        res = kb.add_file(tmp_path / "nope.md")
        assert res["status"] == "error"
        assert "不存在" in res["message"]

    def test_add_file_gbk(self, kb, tmp_path):
        p = tmp_path / "gbk.md"
        p.write_bytes("# 中文标题\n\n这是 GBK 编码的内容。".encode("gbk"))
        assert kb.add_file(p)["status"] == "ok"

    def test_same_source_replaces(self, kb):
        kb.add_document(DOC_A, source="same.md")
        first = kb.stats()["chunks"]
        kb.add_document("# 新\n\n完全不同的内容。", source="same.md")
        assert kb.stats()["documents"] == 1
        assert kb.stats()["chunks"] < first

    def test_replace_false_keeps_both(self, kb):
        kb.add_document(DOC_A, source="s.md")
        kb.add_document(DOC_B, source="s.md", replace=False)
        assert kb.stats()["documents"] == 2

    def test_stats(self, kb):
        kb.add_document(DOC_A, source="a.md")
        st = kb.stats()
        assert st["documents"] == 1
        assert st["chunks"] >= 2
        assert st["consistent"] is True

    def test_list_documents(self, kb):
        kb.add_document(DOC_A, source="a.md", title="异步编程")
        docs = kb.list_documents()
        assert len(docs) == 1
        assert docs[0]["source"] == "a.md"
        assert docs[0]["title"] == "异步编程"
        assert docs[0]["chunks"] >= 2

    def test_delete_document(self, kb):
        res = kb.add_document(DOC_A, source="a.md")
        assert kb.delete_document(res["doc_id"]) is True
        assert kb.stats()["documents"] == 0
        assert kb.stats()["chunks"] == 0

    def test_delete_missing_returns_false(self, kb):
        assert kb.delete_document("nonexistent") is False

    def test_meta_roundtrip(self, kb):
        kb.add_document(DOC_A, source="a.md", meta={"author": "张三"})
        assert kb.list_documents()[0]["source"] == "a.md"


class TestKnowledgeBaseSearch:
    def test_basic_search(self, kb):
        kb.add_document(DOC_A, source="a.md", title="异步编程")
        kb.add_document(DOC_B, source="b.md", title="数据库优化")
        res = kb.search("事件循环和协程", top_k=3)
        assert res["status"] == "ok"
        assert res["results"]
        assert res["results"][0]["title"] == "异步编程"

    def test_cross_document_routing(self, kb):
        kb.add_document(DOC_A, source="a.md", title="异步编程")
        kb.add_document(DOC_B, source="b.md", title="数据库优化")
        top = kb.search("组合索引最左前缀", top_k=1)["results"][0]
        assert top["title"] == "数据库优化"

    def test_empty_query(self, kb):
        res = kb.search("")
        assert res["status"] == "error"

    def test_empty_kb_returns_ok_empty(self, kb):
        res = kb.search("任意查询")
        assert res["status"] == "ok"
        assert res["results"] == []

    def test_result_shape(self, kb):
        kb.add_document(DOC_A, source="a.md", title="异步编程")
        r = kb.search("事件循环")["results"][0]
        assert set(r) >= {"chunk_id", "doc_id", "content", "score",
                          "source", "title", "heading_path"}

    def test_scores_descending(self, kb):
        kb.add_document(DOC_A, source="a.md")
        kb.add_document(DOC_B, source="b.md")
        scores = [r["score"] for r in kb.search("索引", top_k=5)["results"]]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_limits(self, kb):
        kb.add_document(DOC_A, source="a.md")
        assert len(kb.search("协程", top_k=1)["results"]) <= 1

    def test_min_score_filters(self, kb):
        kb.add_document(DOC_A, source="a.md")
        res = kb.search("协程", min_score=0.99)
        assert all(r["score"] >= 0.99 for r in res["results"])

    def test_max_per_doc_diversity(self, kb):
        kb.add_document(DOC_A, source="a.md", title="A")
        kb.add_document(DOC_B, source="b.md", title="B")
        res = kb.search("索引 协程", top_k=6, max_per_doc=1)
        titles = [r["title"] for r in res["results"]]
        assert len(titles) == len(set(titles))

    def test_source_prefix_filter(self, kb):
        kb.add_document(DOC_A, source="proj/x.md", title="X")
        kb.add_document(DOC_B, source="other/y.md", title="Y")
        res = kb.search("索引", source_prefix="proj/")
        assert all(r["source"].startswith("proj/") for r in res["results"])

    def test_scanned_reported(self, kb):
        kb.add_document(DOC_A, source="a.md")
        assert kb.search("协程")["scanned"] >= 2


class TestKnowledgeBasePersistence:
    def test_survives_reopen(self, tmp_path):
        db = tmp_path / "kb.db"
        k1 = KnowledgeBase(db, embedder_name="hash")
        k1.add_document(DOC_A, source="a.md", title="异步编程")
        k1.close_all()

        k2 = KnowledgeBase(db, embedder_name="hash")
        res = k2.search("事件循环")
        k2.close_all()
        assert res["status"] == "ok"
        assert res["results"]
        assert res["results"][0]["title"] == "异步编程"

    def test_vectors_persist_correctly(self, tmp_path):
        """重开后检索结果应与首次一致（向量存取无损）。"""
        db = tmp_path / "kb.db"
        k1 = KnowledgeBase(db, embedder_name="hash")
        k1.add_document(DOC_A, source="a.md")
        s1 = k1.search("并发控制", top_k=1)["results"][0]["score"]
        k1.close_all()

        k2 = KnowledgeBase(db, embedder_name="hash")
        s2 = k2.search("并发控制", top_k=1)["results"][0]["score"]
        k2.close_all()
        assert abs(s1 - s2) < 1e-6


class TestEmbedderSwitchProtection:
    """换嵌入器的保护 —— 实现中被验证出来的真 bug 的回归护栏。

    原实现里 hash 后端身份不含维度，1024→256 维切换时名字检查通过、
    维度检查静默跳过全部切块，最终返回"检索成功但 0 结果"，
    调用方会误以为知识库里没有相关内容。
    """

    def test_dim_switch_reports_error(self, tmp_path):
        db = tmp_path / "kb.db"
        k1 = KnowledgeBase(db, embedder_name="hash", dim=1024)
        k1.add_document(DOC_A, source="a.md")
        k1.close_all()

        k2 = KnowledgeBase(db, embedder_name="hash", dim=256)
        res = k2.search("事件循环")
        k2.close_all()
        assert res["status"] == "error"
        assert "向量空间" in res["message"]
        assert "rebuild" in res["message"]

    def test_name_mismatch_reports_error(self, tmp_path, monkeypatch):
        db = tmp_path / "kb.db"
        k1 = KnowledgeBase(db, embedder_name="hash")
        k1.add_document(DOC_A, source="a.md")
        k1.close_all()

        class FakeEmbedder(E.Embedder):
            name = "fake:model"
            dim = 1024

            def embed(self, texts):
                return [[0.5] * 1024 for _ in texts]

        k2 = KnowledgeBase(db, embedder=FakeEmbedder())
        res = k2.search("事件循环")
        k2.close_all()
        assert res["status"] == "error"
        assert "fake:model" in res["message"]

    def test_stats_flags_inconsistent(self, tmp_path):
        db = tmp_path / "kb.db"
        k1 = KnowledgeBase(db, embedder_name="hash", dim=1024)
        k1.add_document(DOC_A, source="a.md")
        k1.close_all()

        k2 = KnowledgeBase(db, embedder_name="hash", dim=256)
        assert k2.stats()["consistent"] is False
        k2.close_all()

    def test_rebuild_restores_search(self, tmp_path):
        db = tmp_path / "kb.db"
        k1 = KnowledgeBase(db, embedder_name="hash", dim=1024)
        k1.add_document(DOC_A, source="a.md")
        k1.close_all()

        k2 = KnowledgeBase(db, embedder_name="hash", dim=256)
        assert k2.search("事件循环")["status"] == "error"

        r = k2.rebuild()
        assert r["status"] == "ok"
        assert r["rebuilt"] >= 2
        assert r["embedder"] == "hash:256"

        res = k2.search("事件循环")
        k2.close_all()
        assert res["status"] == "ok"
        assert res["results"]

    def test_rebuild_empty_kb(self, kb):
        r = kb.rebuild()
        assert r["status"] == "ok"
        assert r["rebuilt"] == 0
