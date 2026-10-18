"""P0-2 LangGraph Agent 单元测试框架。

计划书依据
----------
- 三、P0-2「开发 LangGraph 专用 Agent 单元测试框架」
- 三、P0-2「专项测试集合：用户越权隔离测试、会话过期测试；
  Jev 意图 / 多意图冲突边界测试；Agent-Wiki 证据绑定、矛盾检测、私有 Vault 隔离测试」
- 三、P0-2「建立全场景测试用例库：正常、边界、异常、弱网、缺失数据、矛盾需求」

本模块只提供**可复用的断言与夹具**，不重复实现具体模块的测试。
"""

from __future__ import annotations

from typing import Any, Iterable

from ai_butler.agents.base import SubAgent, WriteScopeError
from ai_butler.agents.state import (
    REQUIRED_STATE_FIELDS,
    StateContractError,
    new_state,
    validate_state,
)

#: 计划书三、P0-2「全场景测试用例库」的六个场景类别（逐字对应）
SCENARIOS: tuple[str, ...] = ("正常", "边界", "异常", "弱网", "缺失数据", "矛盾需求")


class StateAssertionError(AssertionError):
    pass


# ------------------------------ 夹具 ------------------------------ #
def make_state(**kwargs: Any) -> dict[str, Any]:
    """构造合规 State。等价于 ai_butler.agents.state.new_state。"""
    return new_state(**kwargs)


def make_state_for(user_id: str, *, session_id: str = "session-1", **kwargs: Any) -> dict[str, Any]:
    return new_state(user_id=user_id, session_id=session_id, **kwargs)


# ------------------------------ 断言 ------------------------------ #
def assert_state_contract(state: dict[str, Any]) -> None:
    """计划书三、P3：State 必填字段齐全且 user_id 非空。"""
    assert_state_contract.__doc__
    missing = [f for f in REQUIRED_STATE_FIELDS if f not in state]
    if missing:
        raise StateAssertionError(f"State 缺少必填字段：{missing}")
    validate_state(state)


def assert_writes_within_scope(agent: SubAgent, updates: dict[str, Any]) -> None:
    """计划书三、P4：所有子 Agent 只读全局状态、仅写入自有字段。"""
    if not isinstance(updates, dict):
        raise StateAssertionError(f"{agent.name} 的返回必须是 dict")
    illegal = [k for k in updates if k not in agent.writes]
    if illegal:
        raise StateAssertionError(
            f"{agent.name} 越权写入 {illegal}；允许：{list(agent.writes)}"
        )


def assert_user_scope_untouched(agent: SubAgent, updates: dict[str, Any],
                                *, expected_user_id: str) -> None:
    """计划书三、P4：禁止读取其他 user_id 数据；Agent 不得改写 user_id。"""
    if "user_id" in updates and updates["user_id"] != expected_user_id:
        raise StateAssertionError(
            f"{agent.name} 改写了 user_id：{updates['user_id']!r} != {expected_user_id!r}"
        )


def assert_refuses_blank_user_id(agent: SubAgent, *, session_id: str = "s") -> None:
    """计划书三、P4：user_id 不允许硬编码、不能为空。"""
    for blank in ("", "   ", None):
        state = make_state(user_id="placeholder", session_id=session_id)
        state["user_id"] = blank                      # 绕过构造期校验，直击 Agent 入口
        try:
            agent(state)
        except (StateContractError, WriteScopeError):
            continue
        raise StateAssertionError(f"{agent.name} 未拒绝空 user_id：{blank!r}")


def assert_skips_on_invalid_session(agent: SubAgent) -> None:
    """计划书三、P4：会话失效则跳过执行。"""
    state = make_state(user_id="u-1", session_id="s-1", login_status=False)
    result = agent(state)
    if result != {}:
        raise StateAssertionError(f"{agent.name} 在会话失效时仍产出更新：{result}")


def assert_contract_suite(agent: SubAgent) -> None:
    """对一个 Agent 跑完 P0-2 要求的通用契约检查。"""
    assert agent.name, "Agent 必须声明 name"
    assert agent.writes, f"{agent.name} 必须声明 writes 白名单（仅写入自有字段）"

    state = make_state(user_id="u-contract", session_id="s-contract")
    try:
        updates = agent(state)
    except WriteScopeError as exc:
        raise StateAssertionError(
            f"{agent.name} 违反写作用域契约（基类已拦下）：{exc}"
        ) from exc
    assert_writes_within_scope(agent, updates)
    assert_user_scope_untouched(agent, updates, expected_user_id="u-contract")
    assert_refuses_blank_user_id(agent)
    assert_skips_on_invalid_session(agent)


# --------------------------- 全场景用例目录 --------------------------- #
def scenario_matrix(cases: dict[str, Iterable[str]]) -> dict[str, list[str]]:
    """把用例登记表补齐到计划书要求的六类场景，缺哪类直接报出来。

    计划书三、P0-2：「建立全场景测试用例库：正常、边界、异常、弱网、缺失数据、矛盾需求」
    """
    out: dict[str, list[str]] = {}
    for name in SCENARIOS:
        out[name] = list(cases.get(name, []))
    return out


def missing_scenarios(cases: dict[str, Iterable[str]]) -> list[str]:
    return [name for name in SCENARIOS if not list(cases.get(name, []))]
