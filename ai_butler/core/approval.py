"""P4 人工审批服务（裁定 U59）。

计划书依据
----------
- 三、P8「付款动作必须经人工确认」（U45 的 Tier1/Tier2 即由此派生）
- 三、P10 离线链路「…人工审批后台审核 → 审批通过自动版本升级，驳回终止迭代」

计划书未定义项（U59）
--------------------
计划书只规定「必须经人工确认」，没有定义确认的**机制形态**。本模块按
「开源借鉴」实现一次性凭据模型，边界如下：

1. **一次性凭据**：批准不直接触发执行。dispatch 凭 approval_id 消费
   （consume）后才放行，且**用一次即焚**——复用同一张票执行第二次一律拒。
   防的是「放行一次、缓存复用一生」（与 U53 权限闸先于重复拦截同一立场）。
2. **签名绑定**：票与 (工具名 + 规范化入参签名) 绑定。批准了调用 A，
   拿不出 A 的签名就执行 B —— 票只对**它被批准的那一次调用**有效。
3. **主体绑定**：票与 (agent, role) 绑定。换身份消费即拒（U51 不可传递）。
4. **无人应答即拒**：TTL 到期未决的申请自动作废（expired），
   不存在「挂着永远有效」的票。clock 为可注入端口，测试不睡真时间。
5. **状态机单向**：pending → approved / rejected / expired，
   任何状态都不允许回退；重复 approve/reject 一律拒（幂等不等于放任）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Callable

#: 状态机（单向）
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"

TERMINAL_STATUSES = (APPROVED, REJECTED, EXPIRED)


class ApprovalError(RuntimeError):
    """审批流程序错误（状态不符 / 签名不符 / 主体不符 / 已消费 / 已过期）。"""


@dataclass(frozen=True)
class ApprovalRequest:
    """一张审批票。frozen——状态迁移经 replace() 产生新实例，原票不改。"""

    approval_id: str
    tool: str
    signature: str
    agent: str
    role: str
    tier: int
    reason: str
    status: str = PENDING
    created_at: float = 0.0
    expires_at: float = 0.0
    decided_by: str = ""
    decided_at: float = 0.0
    #: 一次性凭据：批准后尚未消费为 False；消费后为 True，再消费即拒
    consumed: bool = False

    @property
    def pending(self) -> bool:
        return self.status == PENDING

    @property
    def usable(self) -> bool:
        """已批准且未消费。过期判定由 ApprovalService 按 clock 执行。"""
        return self.status == APPROVED and not self.consumed

    def as_dict(self) -> dict:
        return {"approval_id": self.approval_id, "tool": self.tool,
                "signature": self.signature, "agent": self.agent,
                "role": self.role, "tier": self.tier, "reason": self.reason,
                "status": self.status, "created_at": self.created_at,
                "expires_at": self.expires_at, "decided_by": self.decided_by,
                "decided_at": self.decided_at, "consumed": self.consumed}


class ApprovalService:
    """一次性人工审批服务。

    进程内实现（内存态）。生产环境可同构替换为持久实现——**接口纪律不变**：
    一次性、签名绑定、主体绑定、TTL 即拒。
    """

    def __init__(self, *, ttl_seconds: float, clock: Callable[[], float] = time.time) -> None:
        if ttl_seconds <= 0:
            raise ApprovalError("ttl_seconds 必须为正")
        self.ttl = float(ttl_seconds)
        self._clock = clock
        self._tickets: dict[str, ApprovalRequest] = {}

    # ---------------- 申请 ---------------- #
    def request(self, *, tool: str, signature: str, agent: str, role: str,
                tier: int, reason: str) -> ApprovalRequest:
        now = self._clock()
        ticket = ApprovalRequest(
            approval_id=uuid.uuid4().hex, tool=tool, signature=signature,
            agent=agent, role=role, tier=int(tier), reason=reason,
            status=PENDING, created_at=now, expires_at=now + self.ttl)
        self._tickets[ticket.approval_id] = ticket
        return ticket

    # ---------------- 人工决策 ---------------- #
    def approve(self, approval_id: str, *, by: str) -> ApprovalRequest:
        return self._decide(approval_id, APPROVED, by)

    def reject(self, approval_id: str, *, by: str) -> ApprovalRequest:
        return self._decide(approval_id, REJECTED, by)

    def _decide(self, approval_id: str, status: str, by: str) -> ApprovalRequest:
        ticket = self._live(approval_id)
        if ticket.status != PENDING:
            raise ApprovalError(
                f"票 {approval_id} 已处于终态 {ticket.status}，拒绝重复决策"
                f"（状态机单向，U59）")
        if not by or not str(by).strip():
            raise ApprovalError("审批必须署名（decided_by 不得为空）——匿名放行不可审计")
        now = self._clock()
        decided = replace(ticket, status=status, decided_by=str(by), decided_at=now)
        self._tickets[approval_id] = decided
        return decided

    # ---------------- 消费（dispatch 侧） ---------------- #
    def consume(self, approval_id: str, *, tool: str, signature: str,
                agent: str, role: str) -> ApprovalRequest:
        """取票放行。任何不符都拒——**不部分放行**。"""
        ticket = self._live(approval_id)
        if ticket.status == EXPIRED:
            raise ApprovalError(f"票 {approval_id} 已过期（无人应答即拒，U59）")
        if ticket.status == REJECTED:
            raise ApprovalError(f"票 {approval_id} 已被驳回，不得执行")
        if ticket.status != APPROVED:
            raise ApprovalError(f"票 {approval_id} 尚未获批准（{ticket.status}），不得执行")
        if ticket.consumed:
            raise ApprovalError(
                f"票 {approval_id} 已消费过——一次性凭据，用一次即焚（U59）")
        if ticket.tool != tool or ticket.signature != signature:
            raise ApprovalError(
                f"票 {approval_id} 与本次调用不符（批准的是 {ticket.tool}/"
                f"{ticket.signature[:12]}，本次是 {tool}/{signature[:12]}）——签名绑定")
        if ticket.agent != agent or ticket.role != role:
            raise ApprovalError(
                f"票 {approval_id} 与本次主体不符（批准的是 {ticket.agent}/"
                f"{ticket.role}，本次是 {agent}/{role}）——主体绑定，权限不可传递")
        now = self._clock()
        consumed = replace(ticket, consumed=True, decided_at=now)
        self._tickets[approval_id] = consumed
        return consumed

    # ---------------- 查询 ---------------- #
    def get(self, approval_id: str) -> ApprovalRequest | None:
        ticket = self._tickets.get(approval_id)
        if ticket is None:
            return None
        return self._apply_expiry(ticket)

    def pending(self) -> tuple[ApprovalRequest, ...]:
        live = [self._apply_expiry(t) for t in self._tickets.values()]
        return tuple(t for t in live if t.status == PENDING)

    # ---------------- 内部 ---------------- #
    def _live(self, approval_id: str) -> ApprovalRequest:
        ticket = self._tickets.get(approval_id)
        if ticket is None:
            raise ApprovalError(f"审批票不存在：{approval_id}")
        return self._apply_expiry(ticket)

    def _apply_expiry(self, ticket: ApprovalRequest) -> ApprovalRequest:
        """TTL 到期即作废（无人应答即拒）。

        作用于**未决与已批准未消费**两种状态：批准后拖得太久才来消费，
        同样说明审批流程本身出了问题——票的 expires_at 是绝对时刻，
        不存在「批准了就永久有效」。已消费 / 已驳回的不动。
        """
        if ticket.status not in (PENDING, APPROVED) or ticket.consumed:
            return ticket
        if self._clock() >= ticket.expires_at:
            expired = replace(ticket, status=EXPIRED)
            self._tickets[ticket.approval_id] = expired
            return expired
        return ticket
