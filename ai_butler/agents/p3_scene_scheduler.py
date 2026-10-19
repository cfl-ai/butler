"""P3 场景调度 Agent｜业务全局总指挥（阶段视图：真正的调度器实现在 ai_butler/scheduler/）。

计划书依据
----------
- 三、P3「场景调度 Agent｜业务全局总指挥」
- 一、项目概述「业务层：场景调度 Agent 全局唯一总指挥」
- 三、P3「全局最大回退次数 = 2 次」「禁止原路回退」「工具统一调度」「资源节流」

职责边界
--------
调度引擎本身在 ai_butler/scheduler/；本 Agent 是它的**阶段视图**：
向管家汇报本次调度实际走了哪一层、是否触发澄清、回退了几次、工具调用与配额状况。
不重复实现路由逻辑。
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

from ai_butler.agents.phase import PhaseAgent
from ai_butler.scheduler.graph import MAX_ROLLBACKS
from ai_butler.scheduler.router import LAYERS


@dataclass
class SceneSchedulerAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P3"
    writes: ClassVar[tuple[str, ...]] = ("p3_digest",)
    capabilities: ClassVar[tuple[str, ...]] = ()

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        decision = state.get("route_decision") or {}
        return {"p3_digest": {
            "user_id": uid, "phase": self.phase_code,
            "layers": list(LAYERS),
            "layer_used": decision.get("layer", ""),
            "confidence": decision.get("confidence", 0.0),
            "require_clarification": bool(state.get("clarification")),
            "rollback_count": int(state.get("rollback_count") or 0),
            "max_rollbacks": MAX_ROLLBACKS,
            "deep_link": "ai_butler.scheduler",
        }}

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
