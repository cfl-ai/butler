"""P0-2 LangGraph 单元测试框架测试。"""

from __future__ import annotations

import pytest

from ai_butler.agents.base import SubAgent
from ai_butler.devkit import testkit


class GoodAgent(SubAgent):
    name = "good"
    writes = ("good_result",)

    def run(self, state):
        return {"good_result": True}


class BadScopeAgent(SubAgent):
    name = "bad_scope"
    writes = ("bad_result",)

    def run(self, state):
        return {"not_mine": 1}


class HijackAgent(SubAgent):
    name = "hijack"
    writes = ("user_id",)

    def run(self, state):
        return {"user_id": "someone-else"}


def test_scenarios_match_plan_section_p0_2():
    """计划书三、P0-2：全场景测试用例库 = 正常、边界、异常、弱网、缺失数据、矛盾需求。"""
    assert testkit.SCENARIOS == ("正常", "边界", "异常", "弱网", "缺失数据", "矛盾需求")


def test_scenario_matrix_reports_missing_categories():
    cases = {"正常": ["a"], "边界": ["b"]}
    assert testkit.missing_scenarios(cases) == ["异常", "弱网", "缺失数据", "矛盾需求"]
    full = testkit.scenario_matrix({s: ["x"] for s in testkit.SCENARIOS})
    assert testkit.missing_scenarios(full) == []
    assert set(full) == set(testkit.SCENARIOS)


def test_assert_state_contract():
    testkit.assert_state_contract(testkit.make_state(user_id="u-1", session_id="s-1"))
    bad = testkit.make_state(user_id="u-1", session_id="s-1")
    del bad["jev_decision_result"]
    with pytest.raises(testkit.StateAssertionError):
        testkit.assert_state_contract(bad)


def test_assert_writes_within_scope_catches_rogue_agent():
    with pytest.raises(testkit.StateAssertionError) as ei:
        testkit.assert_writes_within_scope(BadScopeAgent(), {"not_mine": 1})
    assert "越权写入" in str(ei.value)


def test_assert_user_scope_untouched_catches_hijack():
    with pytest.raises(testkit.StateAssertionError):
        testkit.assert_user_scope_untouched(HijackAgent(), {"user_id": "someone-else"},
                                            expected_user_id="u-1")


def test_contract_suite_asserts_the_four_p0_2_rules():
    testkit.assert_contract_suite(GoodAgent())

    # 越权 Agent 必须被统一报告为契约不通过（而不是漏出底层异常）
    with pytest.raises(testkit.StateAssertionError) as ei:
        testkit.assert_contract_suite(BadScopeAgent())
    assert "写作用域契约" in str(ei.value)
