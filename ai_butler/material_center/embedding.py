"""P2 Embedding 入库规则与向量化实现。

计划书依据
----------
- 三、P2「统一切片规则、**Embedding 入库规则**；Wiki 编译产出实体、声明同步写入向量库」
- 三、P1/P2「检索入参携带 user_id…不会加载其他用户私有向量与 Wiki 页面」

模型选型
--------
裁定 U10（人工授权）：Embedding 用 **bge-small-zh-v1.5**（512 维），Rerank 用 bge-reranker-base。
本机 torch 为 CPU 版，嵌入在 CPU 推理（实测约 900 条/秒，冷启动 <1s）。

测试替身
--------
HashingEmbedder 是**确定性的**无模型实现，供单元测试使用：不依赖网络与权重文件，
但保留「文本越相似、向量越接近」的性质。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from ai_butler.material_center.chunking import Chunk

#: 向量元数据的字段约定（写入向量库的 metadata）
META_SCOPE = "scope"            # "public" | "vault"
META_USER_ID = "user_id"        # 公共域为空串（ChromaDB 不接受 None）
META_DOC_ID = "doc_id"
META_CHUNK_INDEX = "chunk_index"
META_PAGE_ID = "page_id"        # 对应 Agent-Wiki 页面 id
META_SOURCE_ID = "source_id"    # 对应素材溯源库 source_key
META_CONFIDENCE = "confidence"  # 声明置信度（计划书：返回附带声明置信度）

SCOPE_PUBLIC = "public"
SCOPE_VAULT = "vault"


class EmbeddingError(RuntimeError):
    pass


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class BGEEmbedder:
    """bge-small-zh-v1.5，从本地目录加载（不联网）。"""

    def __init__(self, model_path: str | Path, *, device: str = "cpu",
                 batch_size: int = 32) -> None:
        path = Path(model_path)
        if not path.is_dir():
            raise EmbeddingError(f"嵌入模型目录不存在：{path}")
        from sentence_transformers import SentenceTransformer

        self.name = f"bge-small-zh-v1.5@{path.name}"
        self._model = SentenceTransformer(str(path), device=device)
        # 新版 sentence-transformers 改名了该方法，兼容两种命名
        getter = getattr(self._model, "get_embedding_dimension", None) or \
            self._model.get_sentence_embedding_dimension
        self.dim = int(getter())
        self._batch = batch_size

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(list(texts), normalize_embeddings=True,
                                  batch_size=self._batch)
        return [list(map(float, v)) for v in vecs]


class HashingEmbedder:
    """确定性替身：字符 n-gram 哈希到固定维度后 L2 归一化。"""

    def __init__(self, dim: int = 64, *, ngrams: tuple[int, ...] = (1, 2)) -> None:
        if dim <= 0:
            raise EmbeddingError("dim 必须为正数")
        self.name = f"hashing-{dim}"
        self.dim = dim
        self._ngrams = ngrams

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        text = text.strip()
        if not text:
            return vec
        for n in self._ngrams:
            for i in range(max(1, len(text) - n + 1)):
                gram = text[i:i + n]
                digest = hashlib.sha256(gram.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vec[idx] += sign
        return _l2_normalize(vec)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


@dataclass(frozen=True)
class EmbeddedChunk:
    chunk: Chunk
    embedding: list[float]


def embed_chunks(chunks: Sequence[Chunk], embedder: Embedder) -> list[EmbeddedChunk]:
    """Embedding 入库规则：按 chunk 顺序批量编码，逐条保留原 chunk。"""
    if not chunks:
        return []
    vectors = embedder.encode([c.text for c in chunks])
    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"编码结果条数 {len(vectors)} 与切片条数 {len(chunks)} 不一致"
        )
    for vec in vectors:
        if len(vec) != embedder.dim:
            raise EmbeddingError(f"向量维度 {len(vec)} 与声明的 {embedder.dim} 不一致")
    return [EmbeddedChunk(chunk=c, embedding=v) for c, v in zip(chunks, vectors)]


def chunk_metadata(chunk: Chunk, *, scope: str, user_id: str | None = None,
                   page_id: str = "", source_id: str = "",
                   confidence: float | None = None) -> dict[str, object]:
    """按统一约定组装向量元数据。私有域必须给出 user_id。"""
    if scope not in (SCOPE_PUBLIC, SCOPE_VAULT):
        raise EmbeddingError(f"未知作用域 {scope!r}")
    if scope == SCOPE_VAULT and not user_id:
        raise EmbeddingError("私有域向量必须绑定 user_id（计划书：不会加载其他用户私有向量）")
    meta: dict[str, object] = {
        META_SCOPE: scope,
        META_USER_ID: user_id or "",
        META_DOC_ID: chunk.doc_id,
        META_CHUNK_INDEX: chunk.index,
    }
    if page_id:
        meta[META_PAGE_ID] = page_id
    if source_id:
        meta[META_SOURCE_ID] = source_id
    if confidence is not None:
        meta[META_CONFIDENCE] = float(confidence)
    return meta
