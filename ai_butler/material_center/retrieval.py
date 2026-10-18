"""P2 三重混合检索：BM25 + 向量 + Rerank。

计划书依据
----------
- 三、P2「搭建 **BM25 + 向量检索 + Rerank 三重混合检索服务**」
- 三、P2 表注「检索入参携带 user_id；合并返回公共 Workspace + 当前用户私有 Vault 内容；
  不会加载其他用户私有向量与 Wiki 页面；**返回附带声明置信度、证据 ID**」
- 三、P2「Jev 决策可直接读取声明置信度」

隔离设计（两层各自独立生效）
--------------------------
1. **BM25**：索引按作用域**物理分文件**存放（public / vault:<user_id>），
   查询时只打开「公共 + 本人」两个文件——他人的索引根本不会被触碰；
2. **向量**：单集合 + where 元数据过滤，读用「可见性过滤」、写用「精确作用域过滤」。

**存储键带作用域前缀**（`{scope_key}|{chunk_id}`），避免同一 doc_id 在公共域与
私有域并存时向量 id 相互覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from ai_butler.material_center.chunking import Chunk
from ai_butler.material_center.embedding import (
    META_CHUNK_INDEX,
    META_CONFIDENCE,
    META_DOC_ID,
    META_PAGE_ID,
    META_SCOPE,
    META_SOURCE_ID,
    META_USER_ID,
    SCOPE_PUBLIC,
    SCOPE_VAULT,
    Embedder,
    EmbeddingError,
)
from ai_butler.material_center.vector_store import ChromaVectorStore, VectorRecord

#: 证据 ID 在向量元数据里的存放键（Chroma 元数据不支持列表，用逗号连接）
META_EVIDENCE_IDS = "evidence_ids"

PUBLIC_SCOPE_KEY = "public"


def vault_scope_key(user_id: str) -> str:
    if not user_id or not str(user_id).strip():
        raise EmbeddingError("私有作用域必须携带 user_id")
    return f"vault:{user_id}"


def scoped_id(scope_key: str, chunk_id: str) -> str:
    """存储键 = 作用域前缀 + 逻辑切片 id。"""
    return f"{scope_key}|{chunk_id}"


@dataclass(frozen=True)
class RetrievalRule:
    """三重检索参数（计划书未给数值 → 实现参数）。"""

    candidate_k: int = 40
    top_k: int = 10
    bm25_weight: float = 0.4
    vector_weight: float = 0.6
    rerank_top_n: int = 20

    def __post_init__(self) -> None:
        if self.top_k <= 0 or self.candidate_k <= 0 or self.rerank_top_n <= 0:
            raise EmbeddingError("top_k / candidate_k / rerank_top_n 必须为正数")
        if abs(self.bm25_weight + self.vector_weight - 1.0) > 1e-6:
            raise EmbeddingError("BM25 与向量权重之和必须为 1")


# --------------------------------------------------------------------------- #
# BM25 索引（按作用域分文件）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BM25Row:
    chunk_id: str
    doc_id: str
    text: str
    scope: str
    user_id: str


class BM25Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Any] = {}

    def _path(self, scope_key: str) -> Path:
        return self.root / f"{scope_key.replace(':', '__')}.jsonl"

    def _read(self, scope_key: str) -> list[BM25Row]:
        path = self._path(scope_key)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(BM25Row(**json.loads(line)))
        return out

    def _write(self, scope_key: str, rows: Sequence[BM25Row]) -> None:
        with self._path(scope_key).open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.__dict__, ensure_ascii=False) + "\n")
        self._cache.pop(scope_key, None)

    def upsert(self, scope_key: str, rows: Sequence[BM25Row]) -> int:
        existing = {r.chunk_id: r for r in self._read(scope_key)}
        for row in rows:
            existing[row.chunk_id] = row
        self._write(scope_key, list(existing.values()))
        return len(rows)

    def delete_document(self, scope_key: str, doc_id: str) -> int:
        kept = [r for r in self._read(scope_key) if r.doc_id != doc_id]
        removed = len(self._read(scope_key)) - len(kept)
        self._write(scope_key, kept)
        return removed

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        import jieba
        return [t for t in jieba.lcut(text) if t.strip()]

    def _index(self, scope_key: str):
        if scope_key not in self._cache:
            rows = self._read(scope_key)
            if not rows:
                self._cache[scope_key] = None
            else:
                from rank_bm25 import BM25Okapi
                corpus = [self._tokenize(r.text) for r in rows]
                self._cache[scope_key] = (rows, BM25Okapi(corpus))
        return self._cache[scope_key]

    def search(self, scope_keys: Iterable[str], query: str, *, top_k: int
               ) -> dict[str, float]:
        """返回 {存储键: 归一分}。**只读取给定作用域的索引文件**。"""
        tokens = self._tokenize(query)
        if not tokens:
            return {}
        scored: dict[str, float] = {}
        for key in scope_keys:
            built = self._index(key)
            if not built:
                continue
            rows, bm25 = built
            scores = bm25.get_scores(tokens)
            for row, raw in zip(rows, scores):
                if raw > 0:
                    scored[row.chunk_id] = max(scored.get(row.chunk_id, 0.0), float(raw))
        if not scored:
            return {}
        ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        top = ranked[0][1] or 1.0
        return {cid: score / top for cid, score in ranked}

    def row(self, scope_keys: Sequence[str], chunk_id: str) -> BM25Row | None:
        for key in scope_keys:
            for row in self._read(key):
                if row.chunk_id == chunk_id:
                    return row
        return None


# --------------------------------------------------------------------------- #
# Rerank
# --------------------------------------------------------------------------- #
class Reranker(Protocol):
    name: str

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        ...


class CrossEncoderReranker:
    """bge-reranker-base（裁定 U10），从本地目录加载。"""

    def __init__(self, model_path: str | Path, *, max_length: int = 512) -> None:
        path = Path(model_path)
        if not path.is_dir():
            raise EmbeddingError(f"Rerank 模型目录不存在：{path}")
        from sentence_transformers import CrossEncoder

        self.name = f"bge-reranker-base@{path.name}"
        self._model = CrossEncoder(str(path), max_length=max_length)

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        if not documents:
            return []
        return [float(s) for s in self._model.predict([(query, d) for d in documents])]


class HeuristicReranker:
    """确定性替身：按字符 n-gram 重合度打分，供单测使用。"""

    name = "heuristic-overlap"

    def __init__(self, *, ngram: int = 2) -> None:
        self._n = ngram

    def _grams(self, text: str) -> set[str]:
        text = "".join(text.split())
        if len(text) < self._n:
            return {text} if text else set()
        return {text[i:i + self._n] for i in range(len(text) - self._n + 1)}

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        q = self._grams(query)
        if not q:
            return [0.0] * len(documents)
        return [len(q & self._grams(d)) / len(q) for d in documents]


# --------------------------------------------------------------------------- #
# 混合检索
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    text: str
    score: float
    bm25_score: float
    vector_score: float
    rerank_score: float | None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence(self) -> float | None:
        value = self.metadata.get(META_CONFIDENCE)
        return float(value) if value is not None else None

    @property
    def evidence_ids(self) -> list[str]:
        raw = str(self.metadata.get(META_EVIDENCE_IDS, "") or "")
        return [x for x in raw.split(",") if x]

    @property
    def page_id(self) -> str:
        return str(self.metadata.get(META_PAGE_ID, "") or "")

    @property
    def source_id(self) -> str:
        return str(self.metadata.get(META_SOURCE_ID, "") or "")


class HybridRetriever:
    def __init__(self, *, vector_store: ChromaVectorStore, bm25_store: BM25Store,
                 embedder: Embedder, reranker: Reranker | None = None,
                 rule: RetrievalRule | None = None) -> None:
        self.vector_store = vector_store
        self.bm25_store = bm25_store
        self.embedder = embedder
        self.reranker = reranker
        self.rule = rule or RetrievalRule()

    # ---------------- 入库 ----------------
    def index(self, chunks: Sequence[Chunk], *, user_id: str | None = None,
              page_id: str = "", source_id: str = "",
              confidence: float | None = None,
              evidence_ids: Sequence[str] = ()) -> int:
        if not chunks:
            return 0
        scope = SCOPE_VAULT if user_id else SCOPE_PUBLIC
        scope_key = vault_scope_key(user_id) if user_id else PUBLIC_SCOPE_KEY

        vectors = self.embedder.encode([c.text for c in chunks])
        records, rows = [], []
        for chunk, vec in zip(chunks, vectors):
            sid = scoped_id(scope_key, chunk.chunk_id)
            meta: dict[str, Any] = {
                META_SCOPE: scope, META_USER_ID: user_id or "",
                META_DOC_ID: chunk.doc_id, META_CHUNK_INDEX: chunk.index,
            }
            if page_id:
                meta[META_PAGE_ID] = page_id
            if source_id:
                meta[META_SOURCE_ID] = source_id
            if confidence is not None:
                meta[META_CONFIDENCE] = float(confidence)
            if evidence_ids:
                meta[META_EVIDENCE_IDS] = ",".join(evidence_ids)
            records.append(VectorRecord(id=sid, embedding=vec, document=chunk.text,
                                        metadata=meta))
            rows.append(BM25Row(chunk_id=sid, doc_id=chunk.doc_id, text=chunk.text,
                                scope=scope, user_id=user_id or ""))
        written = self.vector_store.upsert(records)
        self.bm25_store.upsert(scope_key, rows)
        return written

    def remove_document(self, doc_id: str, *, user_id: str | None = None) -> int:
        """删除某素材的切片。只作用于调用方自己的那一份作用域。"""
        scope_key = vault_scope_key(user_id) if user_id else PUBLIC_SCOPE_KEY
        self.bm25_store.delete_document(scope_key, doc_id)
        return self.vector_store.delete_document(doc_id, user_id=user_id)

    # ---------------- 检索 ----------------
    def retrieve(self, query: str, *, user_id: str,
                 top_k: int | None = None) -> list[RetrievedChunk]:
        """三重混合检索。**user_id 必填**（计划书表注「检索入参携带 user_id」）。"""
        if not user_id or not str(user_id).strip():
            raise EmbeddingError(
                "检索必须携带 user_id（计划书三、P2 表注）；禁止不带用户身份的裸检索")
        rule = self.rule
        final_k = top_k or rule.top_k
        scope_keys = [PUBLIC_SCOPE_KEY, vault_scope_key(user_id)]

        bm25_scores = self.bm25_store.search(scope_keys, query, top_k=rule.candidate_k)
        query_vec = self.embedder.encode([query])[0]
        vector_hits = self.vector_store.query(query_vec, user_id=user_id,
                                              top_k=rule.candidate_k)
        vector_scores = {h.id: h.score for h in vector_hits}
        by_id = {h.id: h for h in vector_hits}

        candidate_ids = set(bm25_scores) | set(vector_scores)
        if not candidate_ids:
            return []

        fused = sorted(
            ((cid, rule.bm25_weight * bm25_scores.get(cid, 0.0)
              + rule.vector_weight * vector_scores.get(cid, 0.0)) for cid in candidate_ids),
            key=lambda kv: kv[1], reverse=True)[:rule.rerank_top_n]

        texts: list[str] = []
        for cid, _ in fused:
            hit = by_id.get(cid)
            if hit is not None:
                texts.append(hit.document)
            else:
                row = self.bm25_store.row(scope_keys, cid)
                texts.append(row.text if row else "")

        rerank_scores = (self.reranker.rerank(query, texts)
                         if self.reranker is not None and texts else None)

        results: list[RetrievedChunk] = []
        for idx, (cid, fused_score) in enumerate(fused):
            hit = by_id.get(cid)
            row = None if hit is not None else self.bm25_store.row(scope_keys, cid)
            metadata = dict(hit.metadata) if hit is not None else {
                META_SCOPE: row.scope, META_USER_ID: row.user_id,
                META_DOC_ID: row.doc_id,
            }
            metadata.setdefault(META_DOC_ID, (row.doc_id if row else cid.split("|")[-1].split("#")[0]))
            rr = rerank_scores[idx] if rerank_scores else None
            results.append(RetrievedChunk(
                chunk_id=cid, doc_id=str(metadata.get(META_DOC_ID, "")),
                text=hit.document if hit is not None else texts[idx],
                score=float(rr) if rr is not None else float(fused_score),
                bm25_score=float(bm25_scores.get(cid, 0.0)),
                vector_score=float(vector_scores.get(cid, 0.0)),
                rerank_score=float(rr) if rr is not None else None,
                metadata=metadata))

        results.sort(key=lambda r: r.rerank_score if r.rerank_score is not None else r.score,
                     reverse=True)
        return results[:final_k]
