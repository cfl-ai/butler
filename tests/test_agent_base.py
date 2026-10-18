"""子 Agent 基类契约测试。

计划书依据：三、P4（只读全局状态、仅写入自有字段、user_id 不得硬编码、会话失效跳过执行）。
"""

from __future__ import annotations

import pytest

from ai_butler.agents.base import SubAgent, WriteScopeError
from ai_butler.agents.state import (
    REQUIRED_STATE_FIELDS,
    StateContractError,
    new_state,
    validate_state,
)
from ai_butler.devkit import testkit


class DemoAgent(SubAgent):
    name = "demo"
    writes = ("demo_result",)
    reads = ("intent",)

    def run(self, state):
        return {"demo_result": f"handled for {self.require_user_id(state)}"}


class RogueAgent(SubAgent):
    """故意越权写入，用于验证防护是活的。"""

    name = "rogue"
    writes = ("rogue_result",)

    def run(self, state):
        return {"user_id": "someone-else", "session_id": "hijacked"}


def test_required_state_fields_match_plan_section_p3():
    """计划书三、P3 逐字点名的五个必填字段。"""
    assert REQUIRED_STATE_FIELDS == (
        "user_id", "session_id", "login_status", "jev_decision_result", "wiki_digest",
    )


def test_missing_required_field_is_refused():
    state = new_state(user_id="u-1", session_id="s-1")
    del state["wiki_digest"]
    with pytest.raises(StateContractError) as ei:
        validate_state(state)
    assert "wiki_digest" in str(ei.value)


def test_blank_user_id_is_refused():
    for blank in ("", "   ", None):
        state = new_state(user_id="u-1", session_id="s-1")
        state["user_id"] = blank
        with pytest.raises(StateContractError):
            validate_state(state)


def test_agent_reads_user_id_from_state():
    agent = DemoAgent()
    updates = agent(new_state(user_id="u-42", session_id="s-1"))
    assert updates == {"demo_result": "handled for u-42"}
    testkit.assert_writes_within_scope(agent, updates)


def test_agent_cannot_write_fields_outside_its_scope():
    """计划书三、P4：仅写入自有字段。越权写入必须被拦下，而不是静默生效。"""
    agent = RogueAgent()
    with pytest.raises(WriteScopeError) as ei:
        agent(new_state(user_id="u-1", session_id="s-1"))
    assert "user_id" in str(ei.value) and "非自有字段" in str(ei.value)


def test_agent_skips_execution_when_session_invalid():
    """计划书三、P4：会话失效则跳过执行。"""
    agent = DemoAgent()
    assert agent(new_state(user_id="u-1", session_id="s-1", login_status=False)) == {}


def test_agent_has_no_way_to_delegate_to_another_agent():
    """计划书五、5：Agent 之间禁止直接互相调用。基类不得提供任何转交入口。"""
    surface = [n for n in dir(SubAgent) if not n.startswith("__")]
    forbidden = {"dispatch", "delegate", "call_agent", "handoff", "route", "forward"}
    assert not (set(surface) & forbidden), f"基类暴露了任务转交入口：{set(surface) & forbidden}"


def test_contract_suite_passes_for_a_compliant_agent():
    testkit.assert_contract_suite(DemoAgent())
