"""P2 对外统一检索 API。

计划书依据
----------
- 三、P2「对外统一检索 API，供规划 Agent、质检 Agent、Jev 决策节点调用；
  **API 强制校验用户身份上下文**」
- 三、P2 表注「合并返回公共 Workspace + 当前用户私有 Vault 内容；
  不会加载其他用户私有向量与 Wiki 页面；**返回附带声明置信度、证据 ID**」
- 三、P2 触发时机「每次用户生成方案、解析需求自动检索权威资料；
  返回结果携带 Agent-Wiki 证据 ID、置信度」
- 三、P2「Jev 决策可直接读取声明置信度」
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from ai_butler.rag.chunking import Chunk, ChunkingRule, chunk_document
from ai_butler.rag.embedding import Embedder, EmbeddingError
from ai_butler.rag.retrieval import PUBLIC_SCOPE_KEY, HybridRetriever, vault_scope_key


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    doc_id: str
    text: str
    score: float
    confidence: float | None
    evidence_ids: list[str]
    page_id: str
    source_id: str
    scope: str
    bm25_score: float = 0.0
    vector_score: float = 0.0
    rerank_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResponse:
    query: str
    user_id: str
    searched_scopes: list[str]
    hits: list[SearchHit] = field(default_factory=list)
    reranked: bool = False

    @property
    def count(self) -> int:
        return len(self.hits)

    def as_dict(self) -> dict[str, Any]:
        return {"query": self.query, "user_id": self.user_id,
                "searched_scopes": self.searched_scopes, "reranked": self.reranked,
                "count": self.count, "hits": [h.as_dict() for h in self.hits]}


class SearchService:
    """统一检索 API —— 所有需要权威资料的模块都从这里取数。"""

    def __init__(self, *, retriever: HybridRetriever, chunking: ChunkingRule | None = None
                 ) -> None:
        self.retriever = retriever
        self.chunking = chunking or ChunkingRule()

    # ---------------- 入库 ----------------
    def index_text(self, *, doc_id: str, text: str, url: str = "",
                   user_id: str | None = None, page_id: str = "",
                   confidence: float | None = None,
                   evidence_ids: Sequence[str] = ()) -> int:
        """把一段素材切片后写入向量库与 BM25 索引。

        user_id 为空 → 公共 Workspace；非空 → 该账号私有 Vault。
        """
        chunks = chunk_document(doc_id, text, rule=self.chunking)
        return self.retriever.index(chunks, user_id=user_id, page_id=page_id,
                                    source_id=url, confidence=confidence,
                                    evidence_ids=evidence_ids)

    # ---------------- 检索 ----------------
    def search(self, query: str, *, user_id: str, top_k: int | None = None) -> SearchResponse:
        """强制校验用户身份上下文。"""
        uid = self._require_user(user_id)
        if not query or not query.strip():
            raise EmbeddingError("检索词不得为空")

        hits = self.retriever.retrieve(query, user_id=uid, top_k=top_k)
        return SearchResponse(
            query=query.strip(),
            user_id=uid,
            # 如实回显实际检索的作用域，供调用方与测试核对隔离
            searched_scopes=[PUBLIC_SCOPE_KEY, vault_scope_key(uid)],
            hits=[SearchHit(
                chunk_id=h.chunk_id, doc_id=h.doc_id, text=h.text, score=h.score,
                confidence=h.confidence, evidence_ids=h.evidence_ids,
                page_id=h.page_id, source_id=h.source_id,
                scope=str(h.metadata.get("scope", "")),
                bm25_score=h.bm25_score, vector_score=h.vector_score,
                rerank_score=h.rerank_score) for h in hits],
            reranked=self.retriever.reranker is not None,
        )

    def search_for_jev(self, question: str, *, user_id: str, top_k: int = 5
                       ) -> list[dict[str, Any]]:
        """计划书三、P2「Jev 决策可直接读取声明置信度」。

        只返回 Jev 需要的精简结构：声明 id、置信度、证据 ID —— 不含正文，
        避免把自由文本塞进结构化决策节点（Skill-01：Jev 只吃枚举与数值）。
        """
        response = self.search(question, user_id=user_id, top_k=top_k)
        return [{"claim_page_id": h.page_id, "confidence": h.confidence,
                 "evidence_ids": h.evidence_ids, "source_id": h.source_id,
                 "score": h.score} for h in response.hits]

    @staticmethod
    def _require_user(user_id: str | None) -> str:
        if not user_id or not str(user_id).strip():
            raise EmbeddingError(
                "统一检索 API 必须校验用户身份上下文（计划书三、P2）；"
                "禁止不带 user_id 的检索"
            )
        return str(user_id)
