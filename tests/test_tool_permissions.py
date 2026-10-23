"""工具统一调度的权限闸守护（U43 / U44 / U45 / U51 + 台账 U53）。

为什么要有它
------------
三层权限此前只接在**离线独立入口**（run_standalone）与 CLI 参数枚举上，
在线编排路径的 `ToolDispatcher.dispatch` 零求值 —— 「声明了权限」和
「权限真的生效」是两回事（MEMORY E-21 / E-29 / E-33）。本文件的每条断言
都对应一种**具体的越权手法**，而不是「函数能跑通」：

- 不绑定权限上下文就放行（无从判定即拒绝的背面）
- 用角色冒充能力（member 想调外部工具）
- 靠重复调用缓存绕过权限（放行一次、复用一生）
- 借道提权（用别人的身份求值）
- 高危动作不等人审就先执行（Tier1/Tier2 必须挂起）
- 机密数据上的副作用动作（L4 只许读）

台账依据：core/decisions.py U53（接线位置与默认值）。
"""

from __future__ import annotations

import pytest

from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    PermissionError,
    ToolPermission,
    UserRole,
)
from ai_butler.scheduler.graph import DispatchPlan, SceneScheduler
from ai_butler.scheduler.router import HardRuleLayer, IntentRouter
from ai_butler.scheduler.tools import ToolCall, ToolDispatcher, ToolRegistry


# ------------------------------ 装配辅助 ------------------------------ #
def _registry(**handlers) -> ToolRegistry:
    reg = ToolRegistry()
    for name, fn in handlers.items():
        reg.register(name, fn)
    return reg


def _ctx(*, role: UserRole = UserRole.MEMBER,
         level: CapabilityLevel = CapabilityLevel.L1_WRITE_OWN,
         sensitivity: DataSensitivity = DataSensitivity.L2_INTERNAL) -> ToolPermission:
    return ToolPermission(
        agent=AgentIdentity(key="p2_material_center", level=level),
        role=role, sensitivity=sensitivity)


def _echo():
    """带执行痕迹的 handler：用来证明「拒绝时 handler 真的没跑」。"""
    calls: list[str] = []

    def handler(**_):
        calls.append("ran")
        return "ok"

    return handler, calls


# ------------------------------ 默认拒绝 ------------------------------ #
def test_dispatch_without_permission_context_is_refused():
    """未绑定权限上下文的分发一律拒绝 —— 没有「未绑定时放行」的分支。"""
    handler, calls = _echo()
    dispatcher = ToolDispatcher(_registry(echo=handler))

    result = dispatcher.dispatch(ToolCall("echo"))

    assert result.ok is False
    assert "未绑定权限上下文" in result.note
    assert calls == [], "拒绝时 handler 不得执行"
    assert dispatcher.snapshot()["permission_bound"] is False


def test_denied_call_is_recorded_for_audit():
    """拒绝必须留痕：审计要能看见「谁在什么时候被哪层拒了」。"""
    handler, calls = _echo()
    dispatcher = ToolDispatcher(_registry(echo=handler), permission=_ctx())
    dispatcher.dispatch(ToolCall("echo"))

    assert len(dispatcher.history()) == 1
    assert dispatcher.history()[0].ok is False
    assert dispatcher.snapshot()["denied"] >= 1


# ------------------------------ 第一/三层：能力与角色 ------------------------------ #
def test_role_ceiling_blocks_side_effect_for_member():
    """member 的角色天花板是 L1，调外部工具（L2）必须被拒。

    Agent 自身能力给足（L2），确保拒它的**只能是角色层** ——
    否则两层同时失败，说不清是哪层拦的。
    """
    handler, calls = _echo()
    dispatcher = ToolDispatcher(
        _registry(echo=handler),
        permission=_ctx(role=UserRole.MEMBER, level=CapabilityLevel.L2_SIDE_EFFECT))

    result = dispatcher.dispatch(ToolCall("echo"))

    assert result.ok is False
    assert "user_role" in result.note, "要说清是角色天花板这一层拒的（E-53）"
    assert calls == []


def test_admin_may_trigger_side_effect_but_payment_still_needs_approval():
    """admin 天花板 L2：调外部工具放行，但 Tier1 挂起等人审。"""
    handler, calls = _echo()
    dispatcher = ToolDispatcher(
        _registry(echo=handler),
        permission=_ctx(role=UserRole.ADMIN, level=CapabilityLevel.L2_SIDE_EFFECT))

    result = dispatcher.dispatch(ToolCall("echo"))

    assert result.ok is False and result.needs_approval is True
    assert "需人工审批" in result.note
    assert calls == [], "审批放行前不得执行"


def test_read_only_tool_passes_for_member():
    """只读检索类工具（L0）对 member 放行 —— 权限闸不挡正常读取。"""
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("rag.search", handler, required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(reg, permission=_ctx())

    result = dispatcher.dispatch(ToolCall("rag.search", {"q": "西湖"}))

    assert result.ok is True and result.output == "ok"
    assert calls == ["ran"]


def test_agent_identity_changes_the_verdict_per_call():
    """同一个工具、同一个分发器：身份不同结论不同 —— 求值是**每次调用**做的。"""
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("echo", handler, required=CapabilityLevel.L2_SIDE_EFFECT)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.OWNER))

    weak = dispatcher.dispatch(
        ToolCall("echo"), agent=AgentIdentity(key="p6_content_qa",
                                              level=CapabilityLevel.L0_READ))
    strong = dispatcher.dispatch(
        ToolCall("echo"), agent=AgentIdentity(key="p8_smart_payment",
                                              level=CapabilityLevel.L2_SIDE_EFFECT))

    assert weak.ok is False and "agent_capability" in weak.note
    # 强身份allowed但 Tier1 挂起（OWNER 角色下 L2 → Tier1）
    assert strong.ok is False and strong.needs_approval is True
    assert calls == []


# ------------------------------ 重复调用缓存不得绕过权限 ------------------------------ #
def test_denied_call_cannot_reuse_cached_result():
    """放行一次不得复用一生：角色切换后，同签名调用必须重新求值。"""
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("echo", handler, required=CapabilityLevel.L1_WRITE_OWN)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.OWNER))

    first = dispatcher.dispatch(ToolCall("echo"))
    assert first.ok is True

    # 角色降级为 auditor（天花板 L0）：同一签名再来一次，必须被拒，
    # 不得从缓存拿结果 —— 权限闸位于重复拦截之前，顺序即安全。
    dispatcher.bind_permission(_ctx(role=UserRole.AUDITOR))
    second = dispatcher.dispatch(ToolCall("echo"))

    assert second.ok is False and second.duplicate is False
    assert calls == ["ran"], "拒绝后不得因缓存命中而放行"


# ------------------------------ 密级层 ------------------------------ #
def test_l4_secret_data_denies_side_effect_outright():
    """L4 机密数据上的副作用动作：三层直接拒，不是挂起审批。

    能力与角色都给足，确保拒它的**只能是密级层**。
    """
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("echo", handler, required=CapabilityLevel.L2_SIDE_EFFECT)
    dispatcher = ToolDispatcher(
        reg, permission=_ctx(role=UserRole.OWNER,
                             level=CapabilityLevel.L2_SIDE_EFFECT,
                             sensitivity=DataSensitivity.L4_SECRET))

    result = dispatcher.dispatch(ToolCall("echo"))

    assert result.ok is False
    assert "data_sensitivity" in result.note
    assert calls == []


def test_tool_level_sensitivity_overrides_context():
    """工具自行声明的密级优先于上下文密级（U44：看见之后怎么处理，由数据定）。"""
    handler, calls = _echo()
    reg = ToolRegistry()
    # 工具接触 L3 数据：即便动作只是 L1，审批档也被抬到 Tier1
    reg.register("profile.read", handler, required=CapabilityLevel.L1_WRITE_OWN,
                 sensitivity=DataSensitivity.L3_SENSITIVE)
    dispatcher = ToolDispatcher(
        reg, permission=_ctx(role=UserRole.OWNER,
                             sensitivity=DataSensitivity.L1_PUBLIC))

    result = dispatcher.dispatch(ToolCall("profile.read"))

    assert result.ok is False and result.needs_approval is True
    assert calls == []


# ------------------------------ 无增量与快照 ------------------------------ #
def test_repeated_denials_count_as_no_progress():
    """连续被拒同样判无增量 —— 拒绝循环不该无限烧下去。"""
    handler, calls = _echo()
    dispatcher = ToolDispatcher(_registry(echo=handler), permission=_ctx())

    for _ in range(3):
        assert dispatcher.dispatch(ToolCall("echo")).ok is False

    assert dispatcher.no_progress is True
    assert calls == []


def test_snapshot_reports_permission_binding_and_holds():
    handler, _ = _echo()
    reg = ToolRegistry()
    reg.register("echo", handler)
    dispatcher = ToolDispatcher(
        reg, permission=_ctx(role=UserRole.OWNER,
                             level=CapabilityLevel.L2_SIDE_EFFECT))
    dispatcher.dispatch(ToolCall("echo"))

    snap = dispatcher.snapshot()
    assert snap["permission_bound"] is True
    assert snap["approval_held"] == 1
    assert snap["denied"] == 1


# ------------------------------ 调度器接线 ------------------------------ #
def test_scene_scheduler_binds_a_permission_context():
    """在线编排路径必须绑定权限上下文 —— 这是本次接线的核心断言。"""
    scheduler = SceneScheduler(
        router=IntentRouter([HardRuleLayer([])]), agents={},
        plan=DispatchPlan(chains={}))

    bound = scheduler.dispatcher.permission
    assert bound is not None, "SceneScheduler 未绑定 ToolPermission：权限闸形同虚设"
    assert bound.role == UserRole.MEMBER, "角色缺省必须是最保守的已登录身份"
    assert bound.agent.key == "p3_scene_scheduler"


def test_scheduler_run_rebinds_role_per_task():
    """角色按任务重绑：run(role=...) 必须改变当次生效的权限上下文。"""
    scheduler = SceneScheduler(
        router=IntentRouter([HardRuleLayer([])]), agents={},
        plan=DispatchPlan(chains={}), role=UserRole.MEMBER)

    import asyncio

    async def _exercise():
        await scheduler.run(user_id="user-A", session_id="s-1", utterance="订酒店",
                            labels=("book_hotel",), login_status=True,
                            role=UserRole.AUDITOR)

    asyncio.run(_exercise())
    assert scheduler.dispatcher.permission.role == UserRole.AUDITOR


def test_standalone_entry_still_enforces_permissions():
    """离线独立入口的权限闸不得因本次接线被绕开（回归守护）。"""
    from ai_butler.agents.standalone import StandaloneContext, run_standalone

    class _Agent:
        name = "p2_material_center"

        def __call__(self, state):
            return {"material_indexed": True}

    with pytest.raises(PermissionError):
        run_standalone(_Agent(), ctx=StandaloneContext(
            user_id="user-A", session_id="s-1", role=UserRole.AUDITOR,
            sensitivity=DataSensitivity.L1_PUBLIC,
            required=CapabilityLevel.L2_SIDE_EFFECT))
