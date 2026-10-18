"""P2 切片 / Embedding / 向量库测试。

计划书依据：三、P2「统一切片规则、Embedding 入库规则；
Wiki 编译产出实体、声明同步写入向量库」；五、3「BM25 + ChromaDB 向量库」
"""

from __future__ import annotations

import math

import pytest

from ai_butler.material_center.chunking import (
    ChunkingError,
    ChunkingRule,
    chunk_digest,
    chunk_document,
    normalize_text,
)
from ai_butler.material_center.embedding import (
    META_SCOPE,
    META_USER_ID,
    SCOPE_PUBLIC,
    SCOPE_VAULT,
    BGEEmbedder,
    EmbeddingError,
    HashingEmbedder,
    chunk_metadata,
    embed_chunks,
)
from ai_butler.material_center.vector_store import ChromaVectorStore, VectorRecord

LONG = ("西湖位于浙江省杭州市。它是最著名的淡水湖之一。"
        "春季是西湖最佳游览季节，苏堤春晓尤为出名。"
        "景区开放时间随季节调整，建议出行前查询官方公告。")


# ------------------------------ 切片 ------------------------------ #
def test_rule_validates_parameters():
    with pytest.raises(ChunkingError):
        ChunkingRule(max_chars=0)
    with pytest.raises(ChunkingError):
        ChunkingRule(max_chars=100, overlap_chars=100)
    with pytest.raises(ChunkingError):
        ChunkingRule(max_chars=100, min_chars=-1)


def test_chunk_ids_are_deterministic_and_ordered():
    a = chunk_document("doc-1", LONG)
    b = chunk_document("doc-1", LONG)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert [c.index for c in a] == list(range(len(a)))
    assert chunk_digest(a) == chunk_digest(b)
    assert all(c.chunk_id.startswith("doc-1#") for c in a)


def test_chunks_respect_max_chars():
    rule = ChunkingRule(max_chars=60, overlap_chars=10, min_chars=5)
    chunks = chunk_document("doc-2", LONG * 3, rule=rule)
    assert len(chunks) > 1
    assert all(c.length <= rule.max_chars for c in chunks), [c.length for c in chunks]


def test_long_sentence_without_boundary_is_hard_split():
    text = "无标点长句" * 200          # 没有任何边界标点
    rule = ChunkingRule(max_chars=50, overlap_chars=0, min_chars=1)
    chunks = chunk_document("doc-3", text, rule=rule)
    assert len(chunks) > 1
    assert all(c.length <= 50 for c in chunks)


def test_tail_shorter_than_min_is_merged_into_previous():
    rule = ChunkingRule(max_chars=200, overlap_chars=0, min_chars=200)
    chunks = chunk_document("doc-4", "第一句。" * 10 + "尾。", rule=rule)
    assert len(chunks) == 1


def test_empty_and_blank_inputs():
    assert chunk_document("d", "") == []
    assert chunk_document("d", "   \n\n  ") == []
    with pytest.raises(ChunkingError):
        chunk_document("", LONG)


def test_normalize_collapses_whitespace():
    assert normalize_text("a\r\n\r\n\r\nb\t\tc\u3000d") == "a\n\nb c d"


# ------------------------------ Embedding ------------------------------ #
def test_hashing_embedder_is_deterministic_and_normalised():
    emb = HashingEmbedder(dim=32)
    v1 = emb.encode(["西湖春季最佳"])[0]
    v2 = emb.encode(["西湖春季最佳"])[0]
    assert v1 == v2
    assert len(v1) == 32
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-6)


def test_hashing_embedder_similarity_reflects_text_overlap():
    emb = HashingEmbedder(dim=128)
    a, b, c = emb.encode(["西湖春季最佳游览", "西湖春季最佳季节", "完全无关的股票行情"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(a, b) > dot(a, c), "相似文本的向量应更接近"


def test_embed_chunks_keeps_alignment_and_checks_dimension():
    emb = HashingEmbedder(dim=16)
    chunks = chunk_document("doc-5", LONG)
    out = embed_chunks(chunks, emb)
    assert [o.chunk.chunk_id for o in out] == [c.chunk_id for c in chunks]
    assert all(len(o.embedding) == 16 for o in out)
    assert embed_chunks([], emb) == []


class _BadEmbedder:
    name = "bad"
    dim = 8

    def encode(self, texts):
        return [[0.0] * 4 for _ in texts]        # 维度与声明不符


def test_embed_chunks_rejects_dimension_mismatch():
    with pytest.raises(EmbeddingError):
        embed_chunks(chunk_document("d", LONG), _BadEmbedder())


def test_chunk_metadata_enforces_scope_rules():
    chunk = chunk_document("doc-6", LONG)[0]
    pub = chunk_metadata(chunk, scope=SCOPE_PUBLIC)
    assert pub[META_SCOPE] == SCOPE_PUBLIC and pub[META_USER_ID] == ""

    with pytest.raises(EmbeddingError):
        chunk_metadata(chunk, scope=SCOPE_VAULT)            # 缺 user_id
    priv = chunk_metadata(chunk, scope=SCOPE_VAULT, user_id="user-A")
    assert priv[META_USER_ID] == "user-A"

    with pytest.raises(EmbeddingError):
        chunk_metadata(chunk, scope="unknown")


# ------------------------------ 向量库 ------------------------------ #
@pytest.fixture()
def store(workdir):
    return ChromaVectorStore(workdir / "chroma", collection="test_materials")


def _rec(rid, vec, text, *, scope, user_id="", **extra):
    meta = {META_SCOPE: scope, META_USER_ID: user_id}
    meta.update(extra)
    return VectorRecord(id=rid, embedding=vec, document=text, metadata=meta)


def test_upsert_query_and_delete(store):
    store.upsert([
        _rec("c1", [1.0, 0.0, 0.0], "西湖春季最佳", scope=SCOPE_PUBLIC),
        _rec("c2", [0.0, 1.0, 0.0], "黄山位于安徽", scope=SCOPE_PUBLIC),
    ])
    assert store.count() == 2
    hits = store.query([1.0, 0.0, 0.0], top_k=1)
    assert hits[0].id == "c1" and hits[0].score > 0.9
    assert store.delete(["c1"]) == 1 and store.count() == 1


def test_store_refuses_private_record_without_user_id(store):
    with pytest.raises(EmbeddingError):
        store.upsert([_rec("bad", [1.0, 0.0], "x", scope=SCOPE_VAULT)])
    with pytest.raises(EmbeddingError):
        store.upsert([VectorRecord(id="n", embedding=[1.0], document="x", metadata={})])


def test_scope_filter_isolation(store):
    """计划书三、P2：不会加载其他用户私有向量。"""
    store.upsert([
        _rec("pub", [1.0, 0.0, 0.0], "公共素材", scope=SCOPE_PUBLIC),
        _rec("a", [1.0, 0.0, 0.0], "A 的私有素材", scope=SCOPE_VAULT, user_id="user-A"),
        _rec("b", [1.0, 0.0, 0.0], "B 的私有素材", scope=SCOPE_VAULT, user_id="user-B"),
    ])
    unbound = {h.id for h in store.query([1.0, 0.0, 0.0], top_k=10)}
    assert unbound == {"pub"}, "未绑定账号只能拿到公共域"

    a_ids = {h.id for h in store.query([1.0, 0.0, 0.0], user_id="user-A", top_k=10)}
    assert a_ids == {"pub", "a"}, "A 只能看到公共域 + 自己的私有域"


def test_delete_document_only_touches_the_given_scope(store):
    """写操作用精确作用域：用户不得删除公共知识，也不得删除他人私有素材。"""
    store.upsert([
        _rec("pub-1", [1.0, 0.0], "公共素材", scope=SCOPE_PUBLIC, doc_id="d1"),
        _rec("a-1", [1.0, 0.0], "A 的私有素材", scope=SCOPE_VAULT, user_id="user-A", doc_id="d1"),
        _rec("b-1", [1.0, 0.0], "B 的私有素材", scope=SCOPE_VAULT, user_id="user-B", doc_id="d1"),
    ])
    assert store.delete_document("d1", user_id="user-A") == 1
    assert store.get("pub-1") is not None, "用户不得删除公共知识"
    assert store.get("b-1") is not None, "不得删除他人私有素材"
    assert store.get("a-1") is None


@pytest.mark.live
def test_live_bge_embedder_and_real_semantics(settings):
    """真实模型联调：bge-small-zh-v1.5（裁定 U10）。"""
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "models" / "bge-small-zh-v1.5"
    if not path.is_dir():
        pytest.skip("嵌入模型未下载")
    emb = BGEEmbedder(path)
    assert emb.dim == 512
    a, b, c = emb.encode(["西湖春季最佳游览季节", "春天去西湖最好", "股票今日大跌"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(a, b) > dot(a, c) + 0.1, "语义相近的中文句子应显著更接近"
