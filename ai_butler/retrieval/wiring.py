"""检索装配：把 P2 的三重混合检索接到文字查询链路上。

为什么要单独一个模块
------------------
`app.py` 是装配根，但不该知道 Chroma 的集合名、BM25 的落地目录、embedding 模型在哪。
这些**基础设施细节**集中在这里，`app.py` 只拿一个 `SearchFn`。

三条如实标注（缺一不可）
----------------------
1. **懒加载**：BGE 模型与 Chroma 客户端都在第一次检索时才初始化。冷启动约数秒，
   因此 `/api/chat/` 的首字节超时单独放宽（见 app.py 的 path_timeouts）。
2. **rerank 默认关**：`bge-reranker-base` 约 1.07GB，加载与推理都重。
   关掉时 `HybridRetriever` 只用 BM25 + 向量融合，**`SearchResponse.reranked` 会如实返回
   False**，不声称走完了三重检索。需要时用 `search_reranker_enable=true` 打开。
3. **空库不等于故障**：公共库没素材时检索返回 0 命中，这是正常结果；
   只有「模型加载失败 / 依赖缺失」才抛 `SearchUnavailable`。两者必须能区分。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from ai_butler.core.config import Settings
from ai_butler.material_center.embedding import BGEEmbedder, EmbeddingError
from ai_butler.material_center.retrieval import BM25Store, HybridRetriever
from ai_butler.material_center.service import SearchService
from ai_butler.material_center.vector_store import ChromaVectorStore
from ai_butler.retrieval.chat import SearchUnavailable

#: 检索函数： (query, user_id|None) → 命中字典列表
SearchFn = Callable[[str, str | None], Sequence[dict[str, Any]]]

_lock = threading.RLock()


class SearchWiring:
    """持有一次装配的检索服务，供全进程复用。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service: SearchService | None = None
        self._error: str | None = None
        self._embedder_error: str | None = None

    # ---------------- 构建 ----------------
    def _build(self) -> SearchService:
        base = Path(self.settings.storage_root).parent      # data/<env>
        model_dir = Path("models") / "bge-small-zh-v1.5"
        if not (model_dir / "config.json").is_file():
            raise SearchUnavailable(
                f"Embedding 模型缺失：{model_dir}；运行 scripts/download_models.py")
        try:
            embedder = BGEEmbedder(model_dir)
        except (EmbeddingError, ImportError, OSError) as exc:
            # 依赖没装（requirements-ml.txt）与权重缺失要给不同指引
            raise SearchUnavailable(f"Embedding 初始化失败：{exc}") from exc

        reranker = None
        if self.settings.search_reranker_enable:
            rerank_dir = Path("models") / "bge-reranker-base"
            if (rerank_dir / "config.json").is_file():
                from ai_butler.material_center.retrieval import CrossEncoderReranker

                reranker = CrossEncoderReranker(rerank_dir)
            else:
                # 明确记账：开了 rerank 却没有权重，就**如实报告未 rerank**
                self._embedder_error = f"rerank 权重缺失：{rerank_dir}"

        retriever = HybridRetriever(
            vector_store=ChromaVectorStore(base / "chat_vector"),
            bm25_store=BM25Store(base / "chat_bm25"),
            embedder=embedder, reranker=reranker)
        return SearchService(retriever=retriever)

    def service(self) -> SearchService:
        with _lock:
            if self._service is not None:
                return self._service
            if self._error is not None:
                raise SearchUnavailable(self._error)
            try:
                self._service = self._build()
            except SearchUnavailable as exc:
                self._error = str(exc)
                raise
            return self._service

    # ---------------- 对外 ----------------
    def search(self, query: str, user_id: str | None) -> list[dict[str, Any]]:
        """执行一次检索。**guest 一律只查公共域**（U64：私有库需登录）。"""
        service = self.service()
        # SearchService 强制要求 user_id；访客用一个明确的公共作用域标识，
        # 而不是伪造一个真实账号 —— 后者的私有向量会被顺带检索到。
        uid = user_id or self.settings.guest_public_scope
        response = service.search(query, user_id=uid)
        hits = [hit.as_dict() for hit in response.hits]
        if not response.reranked:
            # 未 rerank 时把「这不是最终排序」写进每条命中，避免下游当成精排结果
            for hit in hits:
                hit["reranked"] = False
        return hits


def build_search_fn(settings: Settings) -> SearchFn:
    wiring = SearchWiring(settings)
    return wiring.search
