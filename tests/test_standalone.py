"""Agent 独立运行入口的守护测试（U48）。

核心命题：**「可独立跑动」不等于「可绕过风控」**。

独立入口省掉的是 LangGraph 会话编排，不是任何一道校验。
本文件的断言全部围绕「哪些东西不许被省掉」：
user_id、session_id、State 契约、三层权限、写作用域、审批标记、审计留痕。
"""

from __future__ import annotations

import asyncio

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


class _AsyncFakeAgent:
    """异步 Agent：`agent(state)` 只返回协程对象，必须被跑到完。

    真实项目里有 7 个这样的 Agent（P1/P5/P6/P8/P9/P10 + P4 集群全体成员）。
    曾经的 bug：run_standalone 创建协程后不 await，updates 静默变成 {}，
    调用方却看到「已独立执行（0 ms）」——伪装成功比失败更糟。
    """

    name = "p9_evaluation"
    writes = ("p9_evaluation",)
    capabilities = ()

    def __init__(self) -> None:
        self.entered = False

    def __call__(self, state: dict):
        return self._run(state)          # 只造协程，不执行

    async def _run(self, state: dict) -> dict:
        self.entered = True
        return {"p9_evaluation": {"board": "built"}}


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


# ---------------------------- 异步 Agent 必须被跑到完 ---------------------------- #

def test_async_agent_coroutine_is_driven_to_completion():
    """异步 Agent 的协程不许「创建了不 await」。

    判据是 **run() 真的进去了**（entered）且 updates 是真实返回值——
    曾经的 bug 形态是 updates 静默变 {}、调用方却看到执行成功。
    """
    agent = _AsyncFakeAgent()
    result = run_standalone(agent, ctx=_ctx())
    assert agent.entered, "协程从未执行 —— run_standalone 只创建了它"
    assert result.updates == {"p9_evaluation": {"board": "built"}}


def test_async_agent_works_inside_a_running_loop():
    """调用方自己已在事件循环里时（异步框架 / async 测试）也要能跑。

    实现走「工作线程起新循环」分支，不劫持调用方的循环。
    """

    async def scenario():
        return run_standalone(_AsyncFakeAgent(), ctx=_ctx())

    result = asyncio.run(scenario())
    assert result.updates == {"p9_evaluation": {"board": "built"}}


def test_non_dict_result_is_rejected_not_silently_swallowed():
    """返回值不是 dict 时明确报错，不许静默变成 {}。"""

    class _BadAgent(_FakeAgent):
        def __call__(self, state: dict):
            return ["not", "a", "dict"]

    with pytest.raises(StandaloneError, match="不是 dict"):
        run_standalone(_BadAgent(), ctx=_ctx())


# ---------------------------- 能力绑定不可省 ---------------------------- #

def test_declared_capabilities_without_tools_is_rejected_cleanly():
    """声明了能力却没绑工具面 → 干净的 StandaloneError。

    曾经的形态：一路放到 run() 里崩 `'NoneType' object has no attribute
    'require'` —— 难看的崩溃不是干净的拒绝（U48：省编排不省校验）。
    """

    class _NeedsTools(_FakeAgent):
        capabilities = ("wiki_read",)

    with pytest.raises(StandaloneError, match="声明了能力"):
        run_standalone(_NeedsTools(), ctx=_ctx())


def test_capability_guard_does_not_block_undeclared_agents():
    """没声明能力的 Agent 不受该校验影响（守卫只治「声明了没接上」）。"""
    agent = _FakeAgent()
    assert getattr(agent, "capabilities", ()) == ()
    result = run_standalone(agent, ctx=_ctx())
    assert result.updates == {"p1_digest": {"ok": True}}
