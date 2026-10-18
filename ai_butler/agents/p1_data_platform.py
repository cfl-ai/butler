"""P1 数据中台 Agent —— 全系统唯一可信数据源。

计划书依据
----------
- 三、P1「数据中台｜全系统唯一可信数据源」
- 三、P1「封装全局统一数据 SDK（所有 Agent 只读，禁止直写库）」

职责边界
--------
本 Agent 是**阶段 Agent**：对外代表 P1 的能力面（数据 SDK 只读通道 + 治理参数），
并汇报本账号在数据中台的数据footprint。**不做业务决策**——那是业务子 Agent 的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ai_butler.agents.capabilities import CAP_DATA_READ
from ai_butler.agents.phase import PhaseAgent


@dataclass
class DataPlatformAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P1"
    writes: ClassVar[tuple[str, ...]] = ("p1_digest",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_DATA_READ,)

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        # 必须异步：数据 SDK 走 asyncpg，连接与事件循环绑定（见 agents/runtime.py）
        uid = self.require_user_id(state)
        sdk = self.tools.require(CAP_DATA_READ)
        threshold = getattr(self.settings, "jev_confidence_threshold", None)

        profile = await sdk.read_profile(user_id=uid, limit=50)
        knowledge = await sdk.read_knowledge(user_id=uid, limit=50)
        counts = {"profile_rows": len(profile), "knowledge_rows": len(knowledge)}

        return {"p1_digest": {
            "user_id": uid,
            "phase": self.phase_code,
            "profile_rows": counts["profile_rows"],
            "knowledge_rows": counts["knowledge_rows"],
            "governance_threshold": threshold,
            "readonly": True,          # 计划书三、P1「所有 Agent 只读，禁止直写库」
        }}
