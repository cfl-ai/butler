"""P2 素材中心 RAG 知识库｜事实依据底层。

----------
- 三、P2「素材中心 RAG 知识库｜事实依据底层」
- 三、P2「对外统一检索 API，供规划 Agent、质检 Agent、Jev 决策节点调用」

职责边界
--------
本 Agent 代表 P2 的检索能力面，并汇报本次检索的作用域与证据完备度。
"""

from __future__ import annotations

# 直接运行本文件（python xxx.py / PyCharm 右键）时，sys.path[0] 是本文件所在目录
# 而非项目根，`import ai_butler` 会直接 ModuleNotFoundError。此处把项目根补进去；
# 作为包被 import 时 __package__ 非空，这段不执行。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    del _sys, _Path

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

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
