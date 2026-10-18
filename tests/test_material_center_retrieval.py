"""P2 三重混合检索与统一检索 API 测试。

计划书依据：三、P2「BM25 + 向量检索 + Rerank 三重混合检索服务」+
表注「检索入参携带 user_id；合并返回公共 Workspace + 当前用户私有 Vault 内容；
不会加载其他用户私有向量与 Wiki 页面；返回附带声明置信度、证据 ID」
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_butler.material_center.chunking import ChunkingRule
from ai_butler.material_center.embedding import HashingEmbedder
from ai_butler.material_center.retrieval import (
    PUBLIC_SCOPE_KEY,
    HeuristicReranker,
    HybridRetriever,
    BM25Store,
    RetrievalRule,
    scoped_id,
    vault_scope_key,
)
from ai_butler.material_center.service import SearchService
from ai_butler.material_center.vector_store import ChromaVectorStore
from ai_butler.material_center.embedding import EmbeddingError

PUBLIC_TEXT = "西湖景区开放时间调整为每日六点半，春季是最佳游览季节，门票可在线预约。"
A_TEXT = "我的行程偏好：春季去西湖，避开清明假期，交通选择高铁。"
B_TEXT = "私人笔记：黄山秋季旅游攻略，门票与交通信息，仅供参考。"


def _build(workdir, *, reranker=None, rule=None):
    embedder = HashingEmbedder(dim=128)
    retriever = HybridRetriever(
        vector_store=ChromaVectorStore(workdir / "chroma", collection="materials"),
        bm25_store=BM25Store(workdir / "bm25"),
        embedder=embedder, reranker=reranker, rule=rule,
    )
    return SearchService(retriever=retriever,
                         chunking=ChunkingRule(max_chars=200, overlap_chars=0, min_chars=1))


def _seed(service):
    service.index_text(doc_id="pub-1", text=PUBLIC_TEXT, url="https://e.com/pub",
                       confidence=0.95, evidence_ids=["ev-pub"], page_id="claim-pub")
    service.index_text(doc_id="a-1", text=A_TEXT, url="https://e.com/a", user_id="user-A",
                       confidence=0.9, evidence_ids=["ev-a"], page_id="claim-a")
    service.index_text(doc_id="b-1", text=B_TEXT, url="https://e.com/b", user_id="user-B",
                       confidence=0.88, evidence_ids=["ev-b"], page_id="claim-b")


# ------------------------------ 隔离 ------------------------------ #
def test_retrieval_requires_user_id(workdir):
    service = _build(workdir)
    with pytest.raises(EmbeddingError) as ei:
        service.retriever.retrieve("西湖", user_id="")
    assert "必须携带 user_id" in str(ei.value)
    with pytest.raises(EmbeddingError):
        service.search("西湖", user_id="")


def test_user_cannot_see_another_users_private_chunks(workdir):
    """计划书三、P2：不会加载其他用户私有向量与 Wiki 页面。"""
    service = _build(workdir)
    _seed(service)

    a_docs = {h.doc_id for h in service.search("黄山秋季旅游攻略门票", user_id="user-A").hits}
    assert "b-1" not in a_docs, "A 不得检索到 B 的私有素材"

    b_docs = {h.doc_id for h in service.search("我的行程偏好西湖高铁", user_id="user-B").hits}
    assert "a-1" not in b_docs, "B 不得检索到 A 的私有素材"


def test_public_knowledge_is_visible_to_everyone(workdir):
    """计划书三、P1/P2：公共域全账号可读。"""
    service = _build(workdir)
    _seed(service)
    query = "西湖景区开放时间门票预约"
    for uid in ("user-A", "user-B", "user-C"):
        docs = {h.doc_id for h in service.search(query, user_id=uid).hits}
        assert "pub-1" in docs, f"{uid} 应能看到公共素材"


def test_searched_scopes_are_echoed_truthfully(workdir):
    service = _build(workdir)
    _seed(service)
    response = service.search("西湖", user_id="user-A")
    assert response.searched_scopes == [PUBLIC_SCOPE_KEY, vault_scope_key("user-A")]
    assert response.user_id == "user-A"


def test_same_doc_id_in_public_and_vault_do_not_collide(workdir):
    """存储键带作用域前缀：同名素材在公共域与私有域并存时不互相覆盖。"""
    service = _build(workdir)
    service.index_text(doc_id="same", text="公共版本：西湖景区开放时间。")
    service.index_text(doc_id="same", text="私有版本：我的西湖行程偏好。",
                       user_id="user-A")

    retriever = service.retriever
    assert retriever.vector_store.count() == 2
    assert retriever.vector_store.get(scoped_id(PUBLIC_SCOPE_KEY, "same#0000")) is not None
    assert retriever.vector_store.get(scoped_id(vault_scope_key("user-A"), "same#0000")) is not None


# ------------------------------ 融合与重排 ------------------------------ #
def test_hits_carry_confidence_and_evidence_ids(workdir):
    """计划书三、P2：返回附带声明置信度、证据 ID。"""
    service = _build(workdir)
    _seed(service)
    hits = service.search("西湖景区开放时间", user_id="user-A").hits
    assert hits, "应召回公共素材"
    top = next(h for h in hits if h.doc_id == "pub-1")
    assert top.confidence == pytest.approx(0.95)
    assert top.evidence_ids == ["ev-pub"]
    assert top.page_id == "claim-pub"
    assert top.source_id == "https://e.com/pub"


def test_hybrid_fuses_bm25_and_vector(workdir):
    service = _build(workdir)
    _seed(service)
    hit = next(h for h in service.search("西湖景区开放时间", user_id="user-A").hits
               if h.doc_id == "pub-1")
    rule = service.retriever.rule
    assert hit.score == pytest.approx(
        rule.bm25_weight * hit.bm25_score + rule.vector_weight * hit.vector_score, abs=1e-6)


def test_reranker_reorders_and_is_reported(workdir):
    service = _build(workdir, reranker=HeuristicReranker())
    _seed(service)
    response = service.search("西湖景区开放时间门票预约", user_id="user-A")
    assert response.reranked is True
    assert all(h.rerank_score is not None for h in response.hits)
    scores = [h.rerank_score for h in response.hits]
    assert scores == sorted(scores, reverse=True), "重排后必须按重排分降序"


def test_rule_validates_weights():
    with pytest.raises(EmbeddingError):
        RetrievalRule(bm25_weight=0.5, vector_weight=0.6)
    with pytest.raises(EmbeddingError):
        RetrievalRule(top_k=0)


def test_top_k_limits_results(workdir):
    service = _build(workdir)
    _seed(service)
    assert len(service.search("西湖", user_id="user-A", top_k=1).hits) <= 1


def test_empty_index_returns_no_hits(workdir):
    service = _build(workdir)
    assert service.search("任何查询", user_id="user-A").hits == []


def test_blank_query_is_refused(workdir):
    service = _build(workdir)
    with pytest.raises(EmbeddingError):
        service.search("   ", user_id="user-A")


# ------------------------------ 增量替换 ------------------------------ #
def test_remove_document_only_affects_own_scope(workdir):
    service = _build(workdir)
    service.index_text(doc_id="shared", text="公共版本：西湖景区开放时间。")
    service.index_text(doc_id="shared", text="私有版本：我的西湖行程偏好。", user_id="user-A")

    removed = service.retriever.remove_document("shared", user_id="user-A")
    assert removed == 1
    assert service.retriever.vector_store.get(
        scoped_id(PUBLIC_SCOPE_KEY, "shared#0000")) is not None, "公共素材不得被用户删除"
    assert service.retriever.vector_store.get(
        scoped_id(vault_scope_key("user-A"), "shared#0000")) is None


# ------------------------------ Jev 接口 ------------------------------ #
def test_search_for_jev_returns_only_structured_fields(workdir):
    """计划书三、P2「Jev 决策可直接读取声明置信度」——只给 id/置信度/证据，不给正文。"""
    service = _build(workdir)
    _seed(service)
    rows = service.search_for_jev("西湖景区开放时间", user_id="user-A")
    assert rows
    for row in rows:
        assert set(row) == {"claim_page_id", "confidence", "evidence_ids", "source_id", "score"}
        assert "text" not in row, "Jev 不应收到自由文本"


@pytest.mark.live
def test_live_bge_reranker_reorders(workdir):
    """真实 Rerank 联调（裁定 U10：bge-reranker-base）。"""
    from ai_butler.material_center.embedding import BGEEmbedder
    from ai_butler.material_center.retrieval import CrossEncoderReranker

    base = Path(__file__).resolve().parents[1] / "models"
    if not (base / "bge-small-zh-v1.5").is_dir() or not (base / "bge-reranker-base").is_dir():
        pytest.skip("本地模型未下载")

    retriever = HybridRetriever(
        vector_store=ChromaVectorStore(workdir / "chroma", collection="live"),
        bm25_store=BM25Store(workdir / "bm25"),
        embedder=BGEEmbedder(base / "bge-small-zh-v1.5"),
        reranker=CrossEncoderReranker(base / "bge-reranker-base"))
    service = SearchService(retriever=retriever)
    service.index_text(doc_id="rel", text="春季是西湖最佳游览季节，苏堤春晓最为出名。")
    service.index_text(doc_id="irr", text="今日股市大跌，投资者情绪谨慎。")

    response = service.search("西湖什么时候去最好", user_id="user-A")
    assert response.hits and response.hits[0].doc_id == "rel"
    assert response.hits[0].rerank_score is not None
