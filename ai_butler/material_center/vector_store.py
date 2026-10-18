"""P2 向量库。

计划书依据
----------
- 五、3「检索能力：**BM25 + ChromaDB 向量库** + Agent-Wiki 编译流水线」
- 三、P2 表注「检索入参携带 user_id；合并返回公共 Workspace + 当前用户私有 Vault 内容；
  **不会加载其他用户私有向量与 Wiki 页面**」

隔离是硬要求：向量库查询**必须**带上作用域过滤，
因此本模块只暴露 scope 明确的查询入口，不提供「无过滤查全库」的方法。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ai_butler.material_center.embedding import (
    META_SCOPE,
    META_USER_ID,
    SCOPE_PUBLIC,
    SCOPE_VAULT,
    EmbeddingError,
)


@dataclass(frozen=True)
class VectorRecord:
    id: str
    embedding: list[float]
    document: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class VectorHit:
    id: str
    document: str
    metadata: dict[str, Any]
    distance: float

    @property
    def score(self) -> float:
        """余弦距离转相似度分数，便于与 BM25 分数一起做融合。"""
        return max(0.0, 1.0 - float(self.distance))


class ChromaVectorStore:
    """ChromaDB 持久化向量库（计划书点名的实现）。"""

    def __init__(self, path: str | Path, *, collection: str = "aibutler_materials") -> None:
        import chromadb

        # ChromaDB 要求 3~512 字符、仅 [a-zA-Z0-9._-] 且首尾为字母数字；
        # 提前给出可读报错，而不是把底层校验错误抛给调用方。
        import re as _re
        if not (3 <= len(collection) <= 512) or not _re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]", collection):
            raise EmbeddingError(
                f"向量库集合名 {collection!r} 不合法：需 3~512 字符，"
                f"仅允许 [a-zA-Z0-9._-] 且首尾为字母或数字")

        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection
        self._client = chromadb.PersistentClient(path=str(self.path))
        self._collection = self._client.get_or_create_collection(
            collection, metadata={"hnsw:space": "cosine"})

    # ---------------- 写入 ----------------
    def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0
        for r in records:
            scope = r.metadata.get(META_SCOPE)
            if scope not in (SCOPE_PUBLIC, SCOPE_VAULT):
                raise EmbeddingError(f"记录 {r.id} 缺少合法的作用域元数据")
            if scope == SCOPE_VAULT and not r.metadata.get(META_USER_ID):
                raise EmbeddingError(f"记录 {r.id} 属于私有域却未绑定 user_id")
        self._collection.upsert(
            ids=[r.id for r in records],
            embeddings=[r.embedding for r in records],
            documents=[r.document for r in records],
            metadatas=[r.metadata for r in records],
        )
        return len(records)

    def delete(self, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        self._collection.delete(ids=list(ids))
        return len(ids)

    @staticmethod
    def _exact_scope_filter(user_id: str | None) -> dict[str, Any]:
        """写操作使用的**精确**作用域过滤。

        与读操作的可见性过滤不同：可见性是「公共 OR 本人」，而删除必须只作用于
        自己那一份——否则用户能把**公共知识**或**他人私有向量**一起删掉。
        """
        if user_id:
            return {"$and": [{META_SCOPE: SCOPE_VAULT}, {META_USER_ID: user_id}]}
        return {META_SCOPE: SCOPE_PUBLIC}

    def delete_document(self, doc_id: str, *, user_id: str | None = None) -> int:
        """按素材删除其全部切片（增量融合时使用）。**只作用于调用方自己的那一份**。"""
        where = {"$and": [self._exact_scope_filter(user_id), {"doc_id": doc_id}]}
        found = self._collection.get(where=where)
        ids = list(found.get("ids") or [])
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        return int(self._collection.count())

    # ---------------- 查询 ----------------
    @staticmethod
    def _scope_filter(user_id: str | None) -> dict[str, Any] | None:
        """作用域过滤条件。

        - 传 user_id：公共域 **或** 本人私有域；
        - 不传：**只有公共域**（绝不返回任何私有向量）。
        """
        if user_id:
            return {"$or": [{META_SCOPE: SCOPE_PUBLIC},
                            {"$and": [{META_SCOPE: SCOPE_VAULT}, {META_USER_ID: user_id}]}]}
        return {META_SCOPE: SCOPE_PUBLIC}

    def query(self, embedding: list[float], *, user_id: str | None = None,
              top_k: int = 10, extra_where: dict[str, Any] | None = None) -> list[VectorHit]:
        if top_k <= 0:
            raise EmbeddingError("top_k 必须为正数")
        scope = self._scope_filter(user_id)
        where = {"$and": [scope, extra_where]} if extra_where else scope

        result = self._collection.query(query_embeddings=[embedding], n_results=top_k,
                                        where=where,
                                        include=["documents", "metadatas", "distances"])
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        return [VectorHit(id=i, document=d, metadata=dict(m or {}), distance=float(x))
                for i, d, m, x in zip(ids, docs, metas, dists)]

    def get(self, record_id: str) -> VectorHit | None:
        found = self._collection.get(ids=[record_id], include=["documents", "metadatas"])
        ids = list(found.get("ids") or [])
        if not ids:
            return None
        return VectorHit(id=ids[0], document=(found.get("documents") or [""])[0],
                         metadata=dict((found.get("metadatas") or [{}])[0]), distance=0.0)
