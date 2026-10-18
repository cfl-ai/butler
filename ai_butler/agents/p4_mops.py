"""P4 Mops 多智能体集群 Agent —— 阶段视图。

计划书依据
----------
- 三、P4「Mops 多智能体集群｜感知与基础执行单元」
- 三、P4「包含子 Agent：设备感知、天气感知、时间进度、增量抽取、路线方案、
  备选预案、代码迭代 Agent」

职责边界
--------
集群成员在 ai_butler/agents/mops.py；本 Agent 是它的**阶段视图**：
向管家汇报集群名册、成员权责与触发约束，不重复实现成员逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ai_butler.agents.mops import ROSTER, roster_manifest
from ai_butler.agents.phase import PhaseAgent


@dataclass
class MopsClusterAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P4"
    writes: ClassVar[tuple[str, ...]] = ("p4_digest",)
    capabilities: ClassVar[tuple[str, ...]] = ()

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        return {"p4_digest": {
            "user_id": uid, "phase": self.phase_code,
            "cluster_size": len(ROSTER),
            "roster": roster_manifest(),
            # 计划书三、P4 触发时机
            "trigger": "全局状态变更自动触发对应子任务执行；会话失效则跳过执行",
            "dispatched_by": "p3_scene_scheduler",   # 业务子 Agent 由 P3 管辖
        }}
