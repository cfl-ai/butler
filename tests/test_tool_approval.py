"""工具调度的一次性审批与协作式超时守护（U59 / U60）。

为什么要有它
------------
Tier1/Tier2 挂起后此前**无人放行**——「需人工审批」是终点不是中点；
handler 执行也没有超时与错误码归因，评测维度四（稳定性与异常）
拿不到「超时是接口故障还是任务本身」的判据。本文件每条断言对应一种
具体的失效形状：

- 放行一次、复用一生（一次性凭据的背面）
- 批准了调用 A、拿去执行调用 B（签名绑定）
- 换身份消费票（主体绑定，U51 的延伸）
- 票挂着永远有效（无人应答即拒）
- 重复 approve / approve 后 reject（状态机单向）
- 匿名放行（不可审计）
- 审批服务没接就放行（U45 纪律不回归）
- 超时后谎称已终止（协作式语义：如实标注后台残留）
- 错误码在产生地最完整（透传而非吞掉）

台账依据：core/decisions.py U59（审批）、U60（超时）。
"""

from __future__ import annotations

import pytest

from ai_butler.core.approval import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    ApprovalError,
    ApprovalService,
)
from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    ToolPermission,
    UserRole,
)
from ai_butler.scheduler.tools import (
    CODE_HANDLER_ERROR,
    CODE_TASK_TIMEOUT,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
    ToolTimeout,
)


# ------------------------------ 装配辅助 ------------------------------ #
def _ctx(*, role: UserRole = UserRole.MEMBER,
         level: CapabilityLevel = CapabilityLevel.L2_SIDE_EFFECT,
         sensitivity: DataSensitivity = DataSensitivity.L2_INTERNAL) -> ToolPermission:
    return ToolPermission(
        agent=AgentIdentity(key="p3_scene_scheduler", level=level),
        role=role, sensitivity=sensitivity)


def _echo():
    calls = []

    def handler(**kwargs):
        calls.append(kwargs)
        return {"echo": True}

    return handler, calls


def _dispatcher(*, approval: ApprovalService | None = None,
                role: UserRole = UserRole.ADMIN,
                required: CapabilityLevel = CapabilityLevel.L2_SIDE_EFFECT,
                **spec_kw) -> ToolDispatcher:
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("echo", handler, required=required, **spec_kw)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=role), approval=approval)
    return dispatcher, calls


# ============================== 审批服务单体 ============================== #
def test_full_lifecycle_request_approve_consume():
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    assert t.status == PENDING
    assert svc.approve(t.approval_id, by="alice").status == APPROVED
    consumed = svc.consume(t.approval_id, tool="echo", signature="sig1",
                           agent="a1", role="ADMIN")
    assert consumed.consumed is True


def test_ticket_is_single_use():
    """一次性：消费第二次即拒——放行一次不能复用一生。"""
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    svc.approve(t.approval_id, by="alice")
    svc.consume(t.approval_id, tool="echo", signature="sig1", agent="a1", role="ADMIN")
    with pytest.raises(ApprovalError, match="已消费"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a1", role="ADMIN")


def test_ticket_is_bound_to_signature():
    """批准调用 A 执行不了调用 B。"""
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig-A", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    svc.approve(t.approval_id, by="alice")
    with pytest.raises(ApprovalError, match="不符"):
        svc.consume(t.approval_id, tool="echo", signature="sig-B",
                    agent="a1", role="ADMIN")


def test_ticket_is_bound_to_subject():
    """换身份消费即拒（U51 不可传递的延伸）。"""
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    svc.approve(t.approval_id, by="alice")
    with pytest.raises(ApprovalError, match="主体不符"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a2", role="ADMIN")
    with pytest.raises(ApprovalError, match="主体不符"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a1", role="MEMBER")


def test_expired_ticket_is_dead_without_answer():
    """无人应答即拒：TTL 到期自动作废，不存在永远有效的票。"""
    now = [1000.0]
    svc = ApprovalService(ttl_seconds=30, clock=lambda: now[0])
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    now[0] += 31
    assert svc.get(t.approval_id).status == EXPIRED
    with pytest.raises(ApprovalError, match="过期"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a1", role="ADMIN")


def test_approved_ticket_also_expires():
    """批准≠永久有效：批准后拖过 TTL 才来消费，同样作废。

    曾经只对 pending 态做过期——批准后的票成了永久票，
    「无人应答即拒」被绕开。此测试锁住该修复。
    """
    now = [1000.0]
    svc = ApprovalService(ttl_seconds=30, clock=lambda: now[0])
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    svc.approve(t.approval_id, by="alice")
    now[0] += 31
    assert svc.get(t.approval_id).status == EXPIRED
    with pytest.raises(ApprovalError, match="过期"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a1", role="ADMIN")


def test_state_machine_is_one_way():
    """重复决策 / approve 后 reject 一律拒（幂等不等于放任）。"""
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    svc.approve(t.approval_id, by="alice")
    with pytest.raises(ApprovalError, match="重复决策"):
        svc.approve(t.approval_id, by="bob")
    with pytest.raises(ApprovalError, match="重复决策"):
        svc.reject(t.approval_id, by="bob")


def test_rejected_ticket_cannot_be_consumed():
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    svc.reject(t.approval_id, by="alice")
    assert svc.get(t.approval_id).status == REJECTED
    with pytest.raises(ApprovalError, match="驳回"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a1", role="ADMIN")


def test_pending_ticket_cannot_be_consumed():
    """没批准就消费——拒。"""
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    with pytest.raises(ApprovalError, match="尚未获批准"):
        svc.consume(t.approval_id, tool="echo", signature="sig1",
                    agent="a1", role="ADMIN")


def test_anonymous_approval_is_rejected():
    """匿名放行不可审计。"""
    svc = ApprovalService(ttl_seconds=60)
    t = svc.request(tool="echo", signature="sig1", agent="a1", role="ADMIN",
                    tier=1, reason="测试")
    with pytest.raises(ApprovalError, match="署名"):
        svc.approve(t.approval_id, by="  ")


def test_unknown_ticket_is_rejected():
    svc = ApprovalService(ttl_seconds=60)
    with pytest.raises(ApprovalError, match="不存在"):
        svc.approve("nope", by="alice")


def test_pending_list_excludes_decided():
    svc = ApprovalService(ttl_seconds=60)
    t1 = svc.request(tool="a", signature="s", agent="x", role="ADMIN", tier=1, reason="")
    t2 = svc.request(tool="b", signature="s", agent="x", role="ADMIN", tier=1, reason="")
    svc.approve(t1.approval_id, by="alice")
    assert [t.approval_id for t in svc.pending()] == [t2.approval_id]


def test_bad_ttl_is_rejected():
    with pytest.raises(ApprovalError):
        ApprovalService(ttl_seconds=0)


# ============================== dispatch 接线 ============================== #
def test_dispatch_suspends_with_ticket_and_does_not_execute():
    """无票：挂起 + 携带 approval_id，handler 不执行。"""
    svc = ApprovalService(ttl_seconds=60)
    dispatcher, calls = _dispatcher(approval=svc)

    result = dispatcher.dispatch(ToolCall("echo"))

    assert result.ok is False and result.needs_approval is True
    assert result.approval_id
    assert calls == [], "审批放行前不得执行"
    assert "待人工审批" in result.note
    # 票确实建了，且在待审批列表里
    assert svc.get(result.approval_id).status == PENDING


def test_dispatch_executes_with_approved_ticket():
    """带票（已批准）：放行执行，票被消费。"""
    svc = ApprovalService(ttl_seconds=60)
    dispatcher, calls = _dispatcher(approval=svc)

    held = dispatcher.dispatch(ToolCall("echo"))
    svc.approve(held.approval_id, by="alice")
    result = dispatcher.dispatch(ToolCall("echo"), approval_id=held.approval_id)

    assert result.ok is True and calls == [{}]
    assert svc.get(held.approval_id).consumed is True


def test_dispatch_rejects_reused_ticket():
    """复用同一票执行第二次——拒（一次性）。"""
    svc = ApprovalService(ttl_seconds=60)
    dispatcher, calls = _dispatcher(approval=svc)

    held = dispatcher.dispatch(ToolCall("echo"))
    svc.approve(held.approval_id, by="alice")
    first = dispatcher.dispatch(ToolCall("echo"), approval_id=held.approval_id)
    assert first.ok is True
    second = dispatcher.dispatch(ToolCall("echo"), approval_id=held.approval_id)
    assert second.ok is False and "已消费" in second.note
    assert len(calls) == 1, "复用票不得触发第二次执行"


def test_dispatch_rejects_ticket_of_another_call():
    """批准了调用 A，拿去执行调用 B——拒。"""
    svc = ApprovalService(ttl_seconds=60)
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("echo", handler, required=CapabilityLevel.L2_SIDE_EFFECT)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.ADMIN),
                                approval=svc)

    held = dispatcher.dispatch(ToolCall("echo", {"x": 1}))
    svc.approve(held.approval_id, by="alice")
    other = dispatcher.dispatch(ToolCall("echo", {"x": 2}),
                                approval_id=held.approval_id)
    assert other.ok is False and "不符" in other.note
    assert calls == []


def test_dispatch_rejects_expired_ticket():
    now = [1000.0]
    svc = ApprovalService(ttl_seconds=30, clock=lambda: now[0])
    dispatcher, calls = _dispatcher(approval=svc)

    held = dispatcher.dispatch(ToolCall("echo"))
    svc.approve(held.approval_id, by="alice")
    now[0] += 31
    result = dispatcher.dispatch(ToolCall("echo"), approval_id=held.approval_id)
    assert result.ok is False and "过期" in result.note
    assert calls == []


def test_dispatch_without_approval_service_never_executes_tier1():
    """审批服务未绑定：Tier1/Tier2 一律挂起不执行（U45 纪律不回归）。"""
    dispatcher, calls = _dispatcher(approval=None)
    result = dispatcher.dispatch(ToolCall("echo"))
    assert result.ok is False and result.needs_approval is True
    assert "审批服务未绑定" in result.note
    assert calls == []


def test_dispatch_rejects_ticket_when_service_not_bound():
    """带了票但调度器没接审批服务：无从验证即不执行。"""
    dispatcher, calls = _dispatcher(approval=None)
    result = dispatcher.dispatch(ToolCall("echo"), approval_id="whatever")
    assert result.ok is False and "未绑定审批服务" in result.note
    assert calls == []


def test_dispatch_rejects_ticket_from_rejected_decision():
    svc = ApprovalService(ttl_seconds=60)
    dispatcher, calls = _dispatcher(approval=svc)
    held = dispatcher.dispatch(ToolCall("echo"))
    svc.reject(held.approval_id, by="alice")
    result = dispatcher.dispatch(ToolCall("echo"), approval_id=held.approval_id)
    assert result.ok is False and "驳回" in result.note
    assert calls == []


def test_tier0_tool_runs_without_ticket():
    """L0 只读工具照常执行——审批闸不挡正常读取。"""
    handler, calls = _echo()
    reg = ToolRegistry()
    reg.register("rag.search", handler, required=CapabilityLevel.L0_READ)
    svc = ApprovalService(ttl_seconds=60)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.MEMBER),
                                approval=svc)
    result = dispatcher.dispatch(ToolCall("rag.search"))
    assert result.ok is True and calls == [{}]


def test_snapshot_reports_approval_binding():
    svc = ApprovalService(ttl_seconds=60)
    bound, _ = _dispatcher(approval=svc)
    assert bound.snapshot()["approval_bound"] is True
    unbound, _ = _dispatcher(approval=None)
    assert unbound.snapshot()["approval_bound"] is False


# ============================== 协作式超时 ============================== #
def test_timeout_returns_error_code_and_marks_background():
    """超时：ok=False + TASK_TIMEOUT + 如实标注执行体可能仍在后台。"""
    import time

    def slow(**kwargs):
        time.sleep(0.5)
        return {"late": True}

    reg = ToolRegistry()
    reg.register("slow", slow, required=CapabilityLevel.L0_READ,
                 timeout_seconds=0.05)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.MEMBER))

    result = dispatcher.dispatch(ToolCall("slow"))

    assert result.ok is False and result.timed_out is True
    assert result.error_code == CODE_TASK_TIMEOUT
    assert "后台" in result.note


def test_no_timeout_leaves_behavior_unchanged():
    """不限时不开线程：行为与接线前一致。"""
    dispatcher, calls = _dispatcher(role=UserRole.MEMBER,
                                    required=CapabilityLevel.L0_READ)
    result = dispatcher.dispatch(ToolCall("echo"))
    assert result.ok is True and calls == [{}]
    assert result.error_code == ""


def test_handler_error_code_is_transparent():
    """判责信息在产生地最完整：handler 自带 code 即透传，不吞。"""
    class PaymentTimeout(Exception):
        code = "PAYMENT_TIMEOUT"

    def pay(**kwargs):
        raise PaymentTimeout("支付网关超时")

    reg = ToolRegistry()
    reg.register("pay", pay, required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.MEMBER))
    result = dispatcher.dispatch(ToolCall("pay"))
    assert result.ok is False and result.error_code == "PAYMENT_TIMEOUT"


def test_unclassified_error_gets_handler_code():
    def boom(**kwargs):
        raise ValueError("炸了")

    reg = ToolRegistry()
    reg.register("boom", boom, required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.MEMBER))
    result = dispatcher.dispatch(ToolCall("boom"))
    assert result.error_code == CODE_HANDLER_ERROR


def test_tool_timeout_exception_carries_code():
    exc = ToolTimeout("x")
    assert exc.code == CODE_TASK_TIMEOUT


def test_bad_timeout_is_rejected_at_registration():
    reg = ToolRegistry()
    with pytest.raises(Exception):
        reg.register("t", lambda **kw: None, timeout_seconds=0)


def test_snapshot_aggregates_error_codes():
    """失败调用不缓存（可重试），两次真实执行都计入错误码分布。"""
    def boom(**kwargs):
        raise ValueError("炸了")

    reg = ToolRegistry()
    reg.register("boom", boom, required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(reg, permission=_ctx(role=UserRole.MEMBER))
    dispatcher.dispatch(ToolCall("boom"))
    dispatcher.dispatch(ToolCall("boom"))
    codes = dispatcher.snapshot()["error_codes"]
    assert codes.get(CODE_HANDLER_ERROR) == 2
