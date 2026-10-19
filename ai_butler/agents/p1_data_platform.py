"""P1 数据中台｜全系统唯一可信数据源。

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

# 直接运行本文件（python xxx.py / PyCharm 右键）时，sys.path[0] 是本文件所在目录
# 而非项目根，`import ai_butler` 会直接 ModuleNotFoundError。此处把项目根补进去；
# 作为包被 import 时 __package__ 非空，这段不执行。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    del _sys, _Path

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

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
