"""Agent 独立运行入口的守护测试（U48）。

核心命题：**「可独立跑动」不等于「可绕过风控」**。

独立入口省掉的是 LangGraph 会话编排，不是任何一道校验。
本文件的断言全部围绕「哪些东西不许被省掉」：
user_id、session_id、State 契约、三层权限、写作用域、审批标记、审计留痕。
"""

from __future__ import annotations

import pytest

from ai_butler.agents.standalone import (
    StandaloneAudit,
    StandaloneContext,
    StandaloneError,
    build_state,
    run_standalone,
)
from ai_butler.core.permissions import (
    CapabilityLevel,
    DataSensitivity,
    PermissionError,
    UserRole,
)


class _FakeAgent:
    """最小可用的阶段 Agent：只写自有字段。"""

    name = "p1_data_platform"
    writes = ("p1_digest",)

    def __init__(self, updates: dict | None = None) -> None:
        self._updates = updates if updates is not None else {"p1_digest": {"ok": True}}
        self.seen: dict | None = None

    def __call__(self, state: dict) -> dict:
        self.seen = dict(state)
        return dict(self._updates)


def _ctx(**kw) -> StandaloneContext:
    base = {"user_id": "u-1", "session_id": "s-1"}
    base.update(kw)
    return StandaloneContext(**base)


# ---------------------------- 身份不可省 ---------------------------- #

def test_user_id_is_mandatory():
    """没有用户上下文的写入无法做行级隔离 —— 独立调用也必须带 user_id。"""
    with pytest.raises(StandaloneError):
        StandaloneContext(user_id="", session_id="s-1")


def test_session_id_is_mandatory():
    """session_id 也不可省：否则审计无法追溯这批操作属于哪一次。"""
    with pytest.raises(StandaloneError):
        StandaloneContext(user_id="u-1", session_id="")


# ---------------------------- State 契约不可省 ---------------------------- #

def test_built_state_satisfies_p3_contract():
    state = build_state(_ctx())
    for f in ("user_id", "session_id", "login_status", "jev_decision_result", "wiki_digest"):
        assert f in state


def test_extra_cannot_override_required_state_fields():
    """extra 想覆盖必填字段 = 绕过隔离，必须拒。"""
    with pytest.raises(StandaloneError):
        build_state(_ctx(), extra={"user_id": "someone-else"})


def test_extra_business_fields_are_passed_through():
    state = build_state(_ctx(), extra={"utterance": "帮我加一条画像"})
    assert state["utterance"] == "帮我加一条画像"


# ---------------------------- 权限不可省 ---------------------------- #

def test_permission_is_checked_before_running():
    """权限不足时，Agent **根本不该被执行**。"""
    agent = _FakeAgent()
    with pytest.raises(PermissionError):
        run_standalone(agent, ctx=_ctx(role=UserRole.AUDITOR,
                                       required=CapabilityLevel.L1_WRITE_OWN))
    assert agent.seen is None, "权限未过却执行了 Agent —— 独立入口成了权限旁路"


def test_l4_secret_data_never_allows_side_effect_calls():
    ctx = _ctx(role=UserRole.OWNER, sensitivity=DataSensitivity.L4_SECRET,
               required=CapabilityLevel.L2_SIDE_EFFECT)
    with pytest.raises(PermissionError):
        run_standalone(_FakeAgent(), ctx=ctx)


# ---------------------------- 正常路径 ---------------------------- #

def test_successful_call_returns_constrained_updates():
    agent = _FakeAgent()
    result = run_standalone(agent, ctx=_ctx())
    assert result.updates == {"p1_digest": {"ok": True}}
    assert result.user_id == "u-1"
    assert result.session_id == "s-1"
    assert not result.needs_approval


def test_agent_receives_a_contract_valid_state():
    agent = _FakeAgent()
    run_standalone(agent, ctx=_ctx(wiki_digest={"d": 1}))
    assert agent.seen is not None
    assert agent.seen["user_id"] == "u-1"
    assert agent.seen["wiki_digest"] == {"d": 1}
    assert agent.seen["login_status"] is True


def test_high_risk_call_is_flagged_for_approval():
    """L2 及以上要留待审批标记 —— 独立调用不等于自动放行。"""
    result = run_standalone(
        _FakeAgent(),
        ctx=_ctx(role=UserRole.OWNER, required=CapabilityLevel.L2_SIDE_EFFECT))
    assert result.needs_approval
    assert result.approval_tier >= 1


# ---------------------------- 审计不可省 ---------------------------- #

def test_audit_tracks_calls_and_pending_approvals():
    audit = StandaloneAudit()
    audit.record(run_standalone(_FakeAgent(), ctx=_ctx()))
    audit.record(run_standalone(
        _FakeAgent(), ctx=_ctx(required=CapabilityLevel.L2_SIDE_EFFECT,
                               role=UserRole.OWNER)))

    assert len(audit.records) == 2
    assert len(audit.pending_approvals()) == 1
    assert len(audit.by_user("u-1")) == 2
    assert len(audit.by_user("other")) == 0
