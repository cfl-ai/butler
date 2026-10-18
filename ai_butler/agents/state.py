"""LangGraph 全局 State 契约。

计划书依据
----------
- 三、P3「定义完整 LangGraph 全局 State 结构体；新增必填字段
  user_id、session_id、login_status、jev_decision_result、wiki_digest」
- 三、P3「注册全局计数器、熔断轮次、工具调用历史；全部记录关联 user_id」
- 三、P4「所有子 Agent 只读全局状态、仅写入自有字段、无跨模块越权调用」

字段分为两类
-----------
- REQUIRED_STATE_FIELDS：计划书点名的必填字段，缺失即拒绝执行；
- DERIVED_STATE_FIELDS：计划书 P3 提到但未命名的「全局计数器、熔断轮次、工具调用历史」，
  字段名属实现层命名，不改变语义。
"""

from __future__ import annotations

from typing import Any, TypedDict

#: 计划书三、P3 逐字点名的必填字段
REQUIRED_STATE_FIELDS: tuple[str, ...] = (
    "user_id", "session_id", "login_status", "jev_decision_result", "wiki_digest",
)

#: 计划书三、P3「全局计数器、熔断轮次、工具调用历史」——字段名为实现层命名
DERIVED_STATE_FIELDS: tuple[str, ...] = (
    "task_id", "intent", "rollback_count", "loop_count", "tool_call_history",
    # --- P3 主状态机所需（计划书三、P3「定义完整 LangGraph 全局 State 结构体」）---
    # 计划书只规定了必填字段与「计数器/轮次/历史」，主状态机的流转字段属实现层命名。
    "utterance", "route_decision", "clarification", "agent_results", "degradation",
    # 风控结论（台账 U41）：输入是否被标为不可信。**受保护字段**，
    # 子 Agent 不得改写 —— 否则「不得沉淀为事实」这条约束可被绕过。
    "input_untrusted",
)


class StateContractError(RuntimeError):
    """State 不满足计划书要求的必填契约。"""


class GraphState(TypedDict, total=False):
    """全局状态。total=False 表示可选字段可不填，必填性由 validate_state 运行时强制。"""

    # --- 必填（计划书三、P3）---
    user_id: str
    session_id: str
    login_status: bool
    jev_decision_result: Any
    wiki_digest: Any

    # --- 派生（计划书三、P3 提及）---
    task_id: str
    intent: str | None
    rollback_count: int
    loop_count: int
    tool_call_history: list[Any]

    # --- P3 主状态机流转字段（实现层命名）---
    utterance: str
    route_decision: Any
    clarification: Any
    agent_results: dict[str, Any]
    degradation: list[str]
    input_untrusted: bool


def validate_state(state: dict[str, Any]) -> None:
    """校验必填字段。计划书：禁止不带用户身份的裸数据访问。"""
    missing = [f for f in REQUIRED_STATE_FIELDS if f not in state]
    if missing:
        raise StateContractError(
            f"State 缺少计划书三、P3 规定的必填字段：{missing}"
        )
    uid = state.get("user_id")
    if not isinstance(uid, str) or not uid.strip():
        raise StateContractError(
            "State.user_id 不得为空：计划书三、P4「读写私有数据必须从 LangGraph State 获取 "
            "user_id，不允许硬编码、不能为空」"
        )


def new_state(*, user_id: str, session_id: str, login_status: bool = True,
              jev_decision_result: Any = None, wiki_digest: Any = None,
              task_id: str = "", **extra: Any) -> dict[str, Any]:
    """构造一个满足契约的最小 State。"""
    state: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "login_status": login_status,
        "jev_decision_result": jev_decision_result,
        "wiki_digest": wiki_digest,
        "task_id": task_id or session_id,
        "rollback_count": 0,
        "loop_count": 0,
        "tool_call_history": [],
    }
    state.update(extra)
    validate_state(state)
    return state
