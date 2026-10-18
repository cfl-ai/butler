"""P0-1 AI Infra 架构底座 Agent —— 全生态通用底层。

计划书依据
----------
- 二、P0「1. AI Infra 架构底座（全生态通用底层）」
- 二、P0「搭建四环境隔离体系」「三层网络隔离」「建设统一 LLM 网关」
  「搭建数据库抽象层」「输出统一架构规范文档，全项目强制执行」

职责边界
--------
对外代表 P0-1 的底座能力面，并汇报当前进程实际加载到的底座事实
（环境、网络分区、中间件装配顺序、固化参数）。**不执行业务**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ai_butler.agents.phase import PhaseAgent


@dataclass
class InfraAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P0-1"
    writes: ClassVar[tuple[str, ...]] = ("p0_infra_digest",)
    capabilities: ClassVar[tuple[str, ...]] = ()

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        from ai_butler.core.environments import ENV_NAMES
        from ai_butler.core.network_zones import ZONES
        from ai_butler.middleware.global_middleware import (
            REQUEST_TIMEOUT_CODE,
        )

        s = self.settings
        return {"p0_infra_digest": {
            "user_id": uid, "phase": self.phase_code,
            "environment": getattr(s, "environment", ""),
            "environments_isolated": list(ENV_NAMES),
            "network_zones": list(ZONES),
            "timeout_code": REQUEST_TIMEOUT_CODE,
            "session_ttl_seconds": getattr(s, "session_ttl_seconds", None),
            "jev_confidence_threshold": getattr(s, "jev_confidence_threshold", None),
        }}
