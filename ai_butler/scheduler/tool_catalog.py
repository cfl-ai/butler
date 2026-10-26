"""真实工具清单的唯一装配点（U114 配套）。

为什么要单独一个模块
--------------------
`scheduler/tools.py` 只有注册表机制，**生产里一个工具都没注册** ——
`ToolRegistry()` 到处是空的。接通层（离散单元 → 裁剪工具 → 回路）因此
能查到调配结论却拿不到工具，永远走 `no_tools_for_capabilities`。
这就是「机制全在、链路空转」（E-86 家族）。

三条纪律
--------
1. **user_id 绝不进参数契约**：声明 `needs_user_id=True`，由调度器从 State 注入
   （`ToolDispatcher.dispatch` 会覆盖调用方传入的值）。让模型填 user_id 等于
   把「读谁的数据」交给概率决定。
2. **只读工具才入清单**：本目录只放 `L0_READ` 的读取类工具。写库、支付一律不在这里，
   它们要带审批档位（U59），不能和「查一下」混在一个清单里。
3. **返回给模型的东西要短**：模型读的是上下文预算，不是数据库。检索结果截断、
   限条数；截断这件事要在结果里**说清楚**，不能让模型以为看到了全文。
"""

from __future__ import annotations

from typing import Any

from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_READ
from ai_butler.core.permissions import CapabilityLevel
from ai_butler.scheduler.tools import ToolRegistry

#: 单条命中回给模型的正文上限（字符）。模型预算有限，长文由 Agent 自己按需再取。
HIT_TEXT_MAX_CHARS = 240
#: 一次检索最多回几条。上限写死，不接受模型传个 1000 把上下文撑爆。
MAX_HITS = 8


def _truncate(text: str, limit: int = HIT_TEXT_MAX_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _resolve_service(candidate: Any) -> Any:
    """把「服务本体」或「返回服务的零参函数」统一成服务本体。

    线上检索是**懒装配**的（`SearchWiring.service()` 首次调用才建向量库，
    缺权重时抛 `SearchUnavailable`）。启动时强建会把「模型没下载」升级成
    「服务起不来」。所以允许传工厂：真正用到工具时才建，失败只影响那一次工具调用。
    """
    if candidate is None:
        return None
    if hasattr(candidate, "search"):
        return candidate
    if callable(candidate):
        return candidate()
    raise TypeError(f"search_service 既不是服务也不是工厂：{type(candidate)!r}")


def build_tool_registry(*, search_service: Any = None, wiki_store: Any = None) -> ToolRegistry:
    """装配真实工具清单。**唯一装配点**。

    两个依赖都可缺省：缺哪个就少注册哪个，而不是注册一个「调用时爆炸」的空壳。
    清单为空是**可诊断的事实**（`registry.names() == []`），不是静默失败。
    """
    registry = ToolRegistry()

    if search_service is not None:
        def search_materials(query: str, top_k: int = 5, *, user_id: str = "") -> dict[str, Any]:
            """检索素材中心（P2）。query 必填；top_k 上限见 MAX_HITS。"""
            q = (query or "").strip()
            if not q:
                raise ValueError("query 不得为空")
            try:
                limit = max(1, min(int(top_k), MAX_HITS))
            except (TypeError, ValueError):
                limit = 5
            resp = _resolve_service(search_service).search(q, user_id=user_id, top_k=limit)
            hits = [{"doc_id": h.doc_id, "text": _truncate(h.text),
                     "score": round(float(h.score), 4),
                     "evidence_ids": list(h.evidence_ids)}
                    for h in resp.hits]
            return {"query": resp.query, "count": len(hits), "hits": hits,
                    "searched_scopes": list(resp.searched_scopes),
                    "truncated_text": True}

        registry.register(
            "search_materials", search_materials,
            description="在素材中心检索权威资料片段（规划/问答前的事实来源）",
            params_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索词，必填"},
                    "top_k": {"type": "integer", "description": f"返回条数，1-{MAX_HITS}，默认 5",
                              "minimum": 1, "maximum": MAX_HITS},
                },
                "required": ["query"],
            },
            capability=CAP_RETRIEVAL, required=CapabilityLevel.L0_READ,
            needs_user_id=True)

    if wiki_store is not None:
        def read_wiki_page(page_id: str, *, user_id: str = "") -> dict[str, Any]:
            """读取公共 Workspace / 本人 Vault 里的一页（P2）。"""
            pid = (page_id or "").strip()
            if not pid:
                raise ValueError("page_id 不得为空")
            page = wiki_store.read_page(pid, user_id=user_id)
            if page is None:
                # 「没找到」是**结论**，不是错误：让模型据此换路径，而不是重试同一个 id
                return {"page_id": pid, "found": False}
            return {"page_id": pid, "found": True, "kind": getattr(page, "kind", ""),
                    "title": getattr(page, "title", ""),
                    "text": _truncate(getattr(page, "text", "")),
                    "confidence": getattr(page, "confidence", None)}

        registry.register(
            "read_wiki_page", read_wiki_page,
            description="按 page_id 读取 Agent Wiki 的一页（公共或本人私有）",
            params_schema={
                "type": "object",
                "properties": {"page_id": {"type": "string",
                                           "description": "知识库页标识，必填"}},
                "required": ["page_id"],
            },
            capability=CAP_WIKI_READ, required=CapabilityLevel.L0_READ,
            needs_user_id=True)

    return registry


__all__ = ["HIT_TEXT_MAX_CHARS", "MAX_HITS", "build_tool_registry"]
