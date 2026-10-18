"""P2 素材中心 RAG 知识库｜事实依据底层。

计划书依据
----------
- 三、P2「素材中心 RAG 知识库｜事实依据底层」
- 三、P2「对外统一检索 API，供规划 Agent、质检 Agent、Jev 决策节点调用」

职责边界
--------
本 Agent 代表 P2 的检索能力面，并汇报本次检索的作用域与证据完备度。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar

from ai_butler.agents.capabilities import CAP_RETRIEVAL
from ai_butler.agents.phase import PhaseAgent


@dataclass
class MaterialCenterAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P2"
    writes: ClassVar[tuple[str, ...]] = ("p2_digest",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_RETRIEVAL,)

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        service = self.tools.require(CAP_RETRIEVAL)
        query = str(state.get("utterance") or "").strip()
        if not query:
            return {"p2_digest": {"user_id": uid, "phase": self.phase_code, "query": "",
                                  "hits": 0, "note": "无检索词"}}

        response = service.search(query, user_id=uid)
        with_evidence = sum(1 for h in response.hits if h.evidence_ids)
        return {"p2_digest": {
            "user_id": uid, "phase": self.phase_code, "query": query,
            "hits": response.count,
            "evidence_coverage": (with_evidence / response.count) if response.count else 0.0,
            "searched_scopes": list(response.searched_scopes),
            "reranked": response.reranked,
        }}
