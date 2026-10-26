"""对话入口的 function calling 承接层（U114 在 `/api/chat/stream` 上的落点）。

为什么不是直接复用 `SceneScheduler`
----------------------------------
`SceneScheduler` 是**任务分发者**，它决定「哪条链去执行」；而对话入口是
**一次问答**，路由、检索、复检都已由 `ChatOrchestrator` 负责。为了走一次工具
就整体搬一套状态机进来，等于把两条主干拧在一起。这里只复用**回路本身**
（`FunctionCallLoop` + `ToolDispatcher`），装配边界反而更清楚。

三条纪律
--------
1. **每次提问新建一个 `ToolDispatcher`**：调度器带本次任务的调用计数、重复拦截、
   无增量判定 —— 共享一个实例会让不同用户的计数互相污染（`ChatOrchestrator`
   本身是无状态的，同一个实例会并发处理多个用户）。
2. **权限按入口身份绑定**：入口身份 `chat_entry` 固定 `L1_WRITE_OWN`，
   所以能读检索类工具，不能碰 L2 以上的进程外动作。工具清单里也只有 L0 读取类。
3. **能力不支持就如实跳过，不抛给用户**：本机 `qwen2.5vl:3b` 没有工具能力，
   这时应当安静退回普通问答并留下原因，而不是让一次提问变成 500。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_butler.core.config import get_settings
from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    ToolPermission,
    UserRole,
)
from ai_butler.scheduler.intake import IntakeReport, run_units
from ai_butler.scheduler.tools import ToolDispatcher, ToolRegistry

#: 对话入口的身份。**编排者不直接触发进程外动作**（L1），与调度器同一纪律。
CHAT_AGENT_KEY = "chat_entry"


@dataclass
class ToolAnswer:
    """对话入口的工具回路结果。`skipped` 非空 = 这一轮没走回路（含原因）。"""

    report: IntakeReport
    skipped: str = ""

    @property
    def ran(self) -> bool:
        return self.report.ran_count > 0

    @property
    def text(self) -> str:
        """取第一个真正跑出答案的单元。

        多个单元时**不拼接**：拼接会把两件事揉成一段，用户没法分辨哪句答的是哪件；
        入口只回第一单元的答案，其余单元在 `tools` 事件里如实列出（留给后续多轮）。
        """
        for outcome in self.report.outcomes:
            if outcome.ran and outcome.result is not None and outcome.result.text:
                return outcome.result.text
        return ""


def build_tool_dispatcher(registry: ToolRegistry, *,
                          role: UserRole = UserRole.MEMBER) -> ToolDispatcher:
    """给对话入口装一个**独立的**工具调度器（含权限上下文）。"""
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key=CHAT_AGENT_KEY, level=CapabilityLevel.L1_WRITE_OWN),
        role=role))
    return dispatcher


async def answer_with_tools(*, gateway: Any, registry: ToolRegistry,
                            user_id: str, utterance: str,
                            intent_label: str = "",
                            role: UserRole = UserRole.MEMBER,
                            settings: Any = None,
                            model_kwargs: dict[str, Any] | None = None) -> ToolAnswer:
    """在对话链路里跑一次「离散化 → 裁剪工具 → 回路」。"""
    cfg = settings or get_settings()
    if not getattr(cfg, "function_calling_enable", False):
        return ToolAnswer(IntakeReport(disabled=True), skipped="function_calling_enable=false")
    if gateway is None:
        return ToolAnswer(IntakeReport(disabled=True), skipped="未装配大模型网关")
    if not registry.names():
        # 空注册表是最容易被忽略的失效形态：回路能跑，却一个工具都看不见
        return ToolAnswer(IntakeReport(), skipped="工具清单为空：未装配任何可下发给模型的工具")

    capable, why = (gateway.tool_capability() if hasattr(gateway, "tool_capability")
                    else (True, ""))
    if not capable:
        return ToolAnswer(IntakeReport(), skipped=f"模型不具备工具能力：{why}")

    dispatcher = build_tool_dispatcher(registry, role=role)
    kwargs = dict(model_kwargs or {})
    report = await run_units(
        gateway=gateway, dispatcher=dispatcher, registry=registry,
        user_id=user_id, utterance=utterance, intent_label=intent_label or None,
        settings=cfg, chat_kwargs=kwargs)
    if report.ran_count == 0:
        # 回路没产出答案（多为调配表查不到）：退回普通问答，原因如实带出去
        reasons = sorted({o.skipped for o in report.outcomes if o.skipped})
        return ToolAnswer(report, skipped="回路未产出答案：" + "、".join(reasons or ["未知"]))
    return ToolAnswer(report)


__all__ = ["CHAT_AGENT_KEY", "ToolAnswer", "answer_with_tools", "build_tool_dispatcher"]
