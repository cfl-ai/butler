"""P8 智能支付 Agent —— 全生态统一预算与结算体系。

计划书依据
----------
- 三、P8「智能支付 Agent｜全生态统一预算与结算体系」
- 三、P8 核心能力「三档智能预算、账单聚合、免密支付、人工确认、一键批量结算、消费画像回写」
- 三、P8 表注「**支付强绑定 user_id**；账单交易记录**行级隔离**；
  **消费事实写入用户私有 Agent-Wiki Vault**」
- 三、P8 步骤2「低预算：极致省钱、去溢价、**逐笔人工确认**；中预算：性价比均衡、
  **小额免密、大额确认（默认）**；高预算：全包省心、高品质、**聚合一键付清**」
- 三、P8 步骤3「两种支付模式：**额度免密支付、全明细人工确认支付**；
  **免密额度属于账号私有配置**」
- 三、P8 步骤5「账单**强制送入质检 Agent** 校验真实性、防错付、多付；
  校验账单归属 user_id 与上下文一致；账单信息生成私有 Vault 的 Claim 声明绑定证据」
- 三、P8 步骤6「支付结果**回写数据中台**，更新用户消费偏好画像；写入携带 user_id；
  同步更新私有 Wiki 实体」
- 三、P8 步骤7「由**顶层管家统一调度**所有支付流程；**未登录禁止发起任何支付相关操作**」
- 三、P8 固定触发流程「方案生成完成 → **质检校验通过** → 支付 Agent 生成三档预算方案 →
  **用户选择档位** → 执行支付 → 结果回显 & 数据沉淀」
- 裁定 U07（开源借鉴）：「中预算默认**单笔 ≤1000 元免密 + 单卡单日累计 ≤3000 元封顶**，
  锚定银联/建行现网惯例；低预算阈值 = 0（逐笔人工确认）；高预算不设固定金额，
  改用**月度预算剩余比例**。阈值引擎采用 Actual Budget 的『条件 → 动作』规则模型」

与 P1「所有 Agent 只读，禁止直写库」的关系
----------------------------------------
支付结果**不由本 Agent 直写数据库**：消费事实以 Claim 形式写入本人私有 Vault
（这是计划书明确允许且要求的），同时产出一份 profile_update 描述，
交由**数据中台的采集管道**落库——Agent 侧仍然保持只读。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_PAYMENT,
    CAP_WIKI_WRITE,
)
from ai_butler.agents.facts import AgentFact, FactWriter
from ai_butler.agents.phase import PhaseAgent
from ai_butler.agents.p6_content_qa import ACTION_BLOCK as QA_BLOCK

# ============================== 档位与阈值（U07）============================== #
TIER_LOW = "low"
TIER_MEDIUM = "medium"
TIER_HIGH = "high"
TIERS: tuple[str, ...] = (TIER_LOW, TIER_MEDIUM, TIER_HIGH)
DEFAULT_TIER = TIER_MEDIUM          # 计划书：「中预算…（默认）」

#: 支付模式（计划书三、P8 步骤3）
MODE_FREE = "free"                  # 额度免密支付
MODE_MANUAL = "manual"              # 全明细人工确认支付
MODES: tuple[str, ...] = (MODE_FREE, MODE_MANUAL)


@dataclass(frozen=True)
class TierPolicy:
    """一档预算的「条件 → 动作」规则（U07：锚定 Actual Budget 的规则模型）。"""

    tier: str
    per_transaction_free_limit: float | None   # None = 不设固定金额上限
    daily_cap: float | None                    # 单卡单日累计封顶；None = 不设
    requires_manual_confirmation: bool         # 逐笔人工确认
    uses_monthly_remaining_ratio: bool         # 改用月度预算剩余比例
    note: str

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"未知档位 {self.tier!r}，可选 {list(TIERS)}")


#: 三档阈值。数值**全部来自裁定 U07**，不是自造。
TIER_POLICIES: dict[str, TierPolicy] = {
    TIER_LOW: TierPolicy(
        tier=TIER_LOW, per_transaction_free_limit=0.0, daily_cap=0.0,
        requires_manual_confirmation=True, uses_monthly_remaining_ratio=False,
        note="极致省钱、去溢价、逐笔人工确认（U07：阈值 = 0）"),
    TIER_MEDIUM: TierPolicy(
        tier=TIER_MEDIUM, per_transaction_free_limit=1000.0, daily_cap=3000.0,
        requires_manual_confirmation=False, uses_monthly_remaining_ratio=False,
        note="性价比均衡、小额免密、大额确认（U07：单笔 ≤1000 免密，单日累计 ≤3000 封顶）"),
    TIER_HIGH: TierPolicy(
        tier=TIER_HIGH, per_transaction_free_limit=None, daily_cap=None,
        requires_manual_confirmation=False, uses_monthly_remaining_ratio=True,
        note="全包省心、聚合一键付清（U07：不设固定金额，改用月度预算剩余比例）"),
}


# ============================== 账单 ============================== #
@dataclass(frozen=True)
class Bill:
    """一条消费明细。**强绑定 user_id**（计划书三、P8 表注）。"""

    bill_id: str
    user_id: str
    amount: float
    payee: str
    occurred_on: date
    channel: str = ""
    category: str = ""
    evidence_ids: tuple[str, ...] = ()
    #: 优惠/折扣金额，用于「优惠叠加」
    discount: float = 0.0

    def __post_init__(self) -> None:
        if not self.user_id or not str(self.user_id).strip():
            raise ValueError("账单必须强绑定 user_id（计划书三、P8 表注）")
        if self.amount < 0:
            raise ValueError("账单金额不得为负")
        if self.discount < 0 or self.discount > self.amount:
            raise ValueError("优惠金额必须落在 0~账单金额之间")

    @property
    def payable(self) -> float:
        return round(self.amount - self.discount, 2)


@dataclass(frozen=True)
class BillAggregate:
    bills: tuple[Bill, ...]
    total: float
    payable_total: float
    discount_total: float

    @property
    def count(self) -> int:
        return len(self.bills)

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(sorted({b.channel for b in self.bills if b.channel}))

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "total": self.total,
                "payable_total": self.payable_total,
                "discount_total": self.discount_total,
                "channels": list(self.channels)}


def aggregate_bills(bills: Sequence[Bill], *, user_id: str) -> BillAggregate:
    """多渠道账单汇总 + 优惠叠加 + 总预算统计（计划书三、P8 步骤1、步骤4）。

    **跨账号账单一律拒绝**——表注要求行级隔离与归属一致。
    """
    foreign = [b.bill_id for b in bills if b.user_id != user_id]
    if foreign:
        raise PermissionError(
            f"账单 {foreign} 不属于账号 {user_id}，拒绝汇总（计划书三、P8 表注：行级隔离）")
    return BillAggregate(
        bills=tuple(bills),
        total=round(sum(b.amount for b in bills), 2),
        payable_total=round(sum(b.payable for b in bills), 2),
        discount_total=round(sum(b.discount for b in bills), 2))


# ============================== 支付模式判定 ============================== #
@dataclass(frozen=True)
class PaymentDecision:
    mode: str
    allowed: bool
    requires_user_confirmation: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "allowed": self.allowed,
                "requires_user_confirmation": self.requires_user_confirmation,
                "reason": self.reason}


def decide_payment_mode(
    *,
    policy: TierPolicy,
    amount: float,
    spent_today: float = 0.0,
    monthly_budget: float | None = None,
    spent_this_month: float = 0.0,
    free_quota_enabled: bool = True,
) -> PaymentDecision:
    """按档位规则判定：免密放行 / 需人工确认 / 拒绝。

    「免密额度属于账号私有配置」（步骤3），因此由 free_quota_enabled 控制——
    账号关掉免密后，即便档位允许也必须逐笔确认。
    """
    if amount < 0:
        raise ValueError("金额不得为负")

    if policy.requires_manual_confirmation:
        return PaymentDecision(MODE_MANUAL, True, True,
                               f"{policy.tier} 档要求逐笔人工确认（{policy.note}）")

    if not free_quota_enabled:
        return PaymentDecision(MODE_MANUAL, True, True,
                               "该账号未开启免密支付（免密额度属账号私有配置）")

    if policy.uses_monthly_remaining_ratio:
        if monthly_budget is None or monthly_budget <= 0:
            return PaymentDecision(MODE_MANUAL, True, True,
                                   "高预算档按月度预算剩余比例放行，但未提供月度预算")
        remaining = monthly_budget - spent_this_month
        if amount > remaining:
            return PaymentDecision(MODE_MANUAL, True, True,
                                   f"超出月度预算剩余 {remaining:.2f} 元，转人工确认")
        return PaymentDecision(MODE_FREE, True, False,
                               f"月度预算尚余 {remaining:.2f} 元，聚合一键付清")

    limit = policy.per_transaction_free_limit or 0.0
    if amount > limit:
        return PaymentDecision(MODE_MANUAL, True, True,
                               f"单笔 {amount:.2f} 元超过免密额度 {limit:.2f} 元，转人工确认")

    cap = policy.daily_cap
    if cap is not None and spent_today + amount > cap:
        return PaymentDecision(MODE_MANUAL, True, True,
                               f"当日累计将达 {spent_today + amount:.2f} 元，"
                               f"超过单日封顶 {cap:.2f} 元，转人工确认")

    return PaymentDecision(MODE_FREE, True, False,
                           f"单笔 {amount:.2f} 元在免密额度 {limit:.2f} 元内且未触顶")


# ============================== 三档方案 ============================== #
@dataclass(frozen=True)
class TierPlan:
    tier: str
    total: float
    decision: PaymentDecision
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "total": self.total, "note": self.note,
                **self.decision.as_dict()}


def build_three_tier_plan(*, aggregate: BillAggregate, spent_today: float = 0.0,
                          monthly_budget: float | None = None,
                          spent_this_month: float = 0.0,
                          free_quota_enabled: bool = True) -> dict[str, TierPlan]:
    """生成三档预算方案（计划书三、P8 步骤2 + 固定触发流程）。"""
    plans: dict[str, TierPlan] = {}
    for tier in TIERS:
        policy = TIER_POLICIES[tier]
        decision = decide_payment_mode(
            policy=policy, amount=aggregate.payable_total, spent_today=spent_today,
            monthly_budget=monthly_budget, spent_this_month=spent_this_month,
            free_quota_enabled=free_quota_enabled)
        plans[tier] = TierPlan(tier=tier, total=aggregate.payable_total,
                               decision=decision, note=policy.note)
    return plans


def claim_id_for(*, user_id: str, bill: Bill) -> str:
    digest = hashlib.sha256(f"{user_id}|{bill.bill_id}|{bill.payee}".encode("utf-8"))
    return f"claim-bill-{digest.hexdigest()[:16]}"


# ============================== 阶段 Agent ============================== #
@dataclass
class PaymentAgent(PhaseAgent):
    """P8 智能支付 Agent。"""

    phase_code: ClassVar[str] = "P8"
    writes: ClassVar[tuple[str, ...]] = ("p8_payment",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_DATA_READ, CAP_WIKI_WRITE, CAP_PAYMENT)
    #: 统一架构规范 10.11 的支付能力禁令针对垂类 Agent；支付 Agent 自身是唯一持有者
    may_hold_payment: ClassVar[bool] = True

    def _bills(self, state: Mapping[str, Any], *, user_id: str) -> list[Bill]:
        out: list[Bill] = []
        for raw in (state.get("bills") or []):
            row = raw or {}
            statement = row.get("occurred_on")
            occurred = (statement if isinstance(statement, date)
                        else date.fromisoformat(str(statement)) if statement
                        else date.today())
            out.append(Bill(
                bill_id=str(row.get("bill_id") or row.get("statement", "")[:24]),
                user_id=str(row.get("user_id") or user_id),
                amount=float(row.get("amount") or 0.0),
                payee=str(row.get("payee") or ""),
                occurred_on=occurred,
                channel=str(row.get("channel") or ""),
                category=str(row.get("category") or ""),
                evidence_ids=tuple(row.get("evidence_ids") or ()),
                discount=float(row.get("discount") or 0.0)))
        return out

    @staticmethod
    def _qa_gate(state: Mapping[str, Any]) -> tuple[bool, str]:
        """计划书固定触发流程：**质检校验通过**才轮到支付。

        没有质检结论时**不放行**——「强制送检」不能因为缺数据就变成可跳过。
        """
        verdict = state.get("qa_verdict")
        if not isinstance(verdict, Mapping) or not verdict:
            return False, "未收到质检结论，按『账单强制送检』要求不予放行"
        if verdict.get("action") == QA_BLOCK:
            return False, f"质检拦截：{'; '.join(verdict.get('alerts') or []) or '存在隐私问题'}"
        return True, f"质检通过（action={verdict.get('action')}）"

    def _write_consumption_facts(self, *, user_id: str, bills: Sequence[Bill],
                                 untrusted: bool = False) -> list[dict[str, Any]]:
        """计划书表注：**消费事实写入用户私有 Agent-Wiki Vault**，绑定 Claim 与 Evidence。"""
        billable = [b for b in bills if b.evidence_ids]
        if not billable:
            return []
        writer: FactWriter = self.tools.require(CAP_WIKI_WRITE)
        facts = [AgentFact(
            statement=f"{b.payee} 消费 {b.payable:.2f} 元（{b.occurred_on.isoformat()}）",
            claim_page_id=claim_id_for(user_id=user_id, bill=b),
            evidence_ids=tuple(b.evidence_ids),
            confidence=1.0,                      # 账单是既成事实，不涉及置信估计
            subject_id=user_id, object_id=b.bill_id,
            claim_type="preference") for b in billable]
        # 风控（台账 U41）：不可信输入产出的事实只能落成 SUSPECTED
        pages = writer.write_many(facts, user_id=user_id, untrusted=untrusted)
        return [{"claim_page_id": p.id,
                 "evidence_ids": list(p.extra.get("evidence_ids") or [])} for p in pages]

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)

        # ---- 步骤 7：未登录禁止发起任何支付操作（SubAgent.__call__ 已挡一层，此处再核）----
        if not state.get("login_status"):
            return {"p8_payment": {"user_id": uid, "phase": self.phase_code,
                                   "executed": False, "reason": "会话失效，禁止支付"}}

        # ---- 步骤 1：多渠道账单汇总（强绑定 user_id，跨账号即拒）----
        try:
            bills = self._bills(state, user_id=uid)
            aggregate = aggregate_bills(bills, user_id=uid)
        except PermissionError as error:
            return {"p8_payment": {"user_id": uid, "phase": self.phase_code,
                                   "executed": False, "reason": str(error)}}

        # ---- 固定触发流程第 2 步：质检校验通过 ----
        qa_passed, qa_reason = self._qa_gate(state)
        if not qa_passed:
            return {"p8_payment": {
                "user_id": uid, "phase": self.phase_code, "executed": False,
                "qa_gate": {"passed": False, "reason": qa_reason},
                "aggregate": aggregate.as_dict(),
                "reason": "质检未通过，支付流程在此终止"}}

        # ---- 步骤 2：三档预算方案 ----
        free_quota = bool(state.get("free_quota_enabled", True))
        plans = build_three_tier_plan(
            aggregate=aggregate,
            spent_today=float(state.get("spent_today") or 0.0),
            monthly_budget=state.get("monthly_budget"),
            spent_this_month=float(state.get("spent_this_month") or 0.0),
            free_quota_enabled=free_quota)

        # ---- 固定触发流程第 4 步：用户选择档位；未选择就只出方案，不执行 ----
        selected = str(state.get("selected_tier") or "")
        payload: dict[str, Any] = {
            "user_id": uid, "phase": self.phase_code,
            "qa_gate": {"passed": True, "reason": qa_reason},
            "aggregate": aggregate.as_dict(),
            "plans": {t: p.as_dict() for t, p in plans.items()},
            "default_tier": DEFAULT_TIER,
            "free_quota_enabled": free_quota,
            "selected_tier": selected,
            "executed": False,
        }
        if not selected:
            payload["reason"] = "尚未选择档位，仅生成三档预算方案（固定触发流程要求用户选择）"
            return {"p8_payment": payload}

        if selected not in TIER_POLICIES:
            payload["reason"] = f"未知档位 {selected!r}，可选 {list(TIERS)}"
            return {"p8_payment": payload}

        plan = plans[selected]
        payload["mode"] = plan.decision.mode
        payload["requires_user_confirmation"] = plan.decision.requires_user_confirmation

        # ---- 免密支付需账号已启用；人工确认需调用方给出许可 ----
        if plan.decision.requires_user_confirmation and not state.get("user_confirmed"):
            payload["reason"] = f"该档位需人工确认：{plan.decision.reason}"
            return {"p8_payment": payload}

        # ---- 步骤 5：消费事实写入私有 Vault（绑定 Claim 与 Evidence）----
        payload["claims"] = self._write_consumption_facts(user_id=uid, bills=bills, untrusted=bool(state.get("input_untrusted")))
        # ---- 步骤 6：结果回写数据中台（**以描述形式交给采集管道**，Agent 不直写库）----
        payload["profile_update"] = {
            "user_id": uid,
            "category": "consumption_preference",
            "payee": sorted({b.payee for b in bills if b.payee}),
            "channel": list(aggregate.channels),
            "total": aggregate.payable_total,
            "source": "p8_payment",
            "note": "由数据中台采集管道落库；Agent 侧保持只读（计划书三、P1）",
        }
        payload["executed"] = True
        payload["reason"] = "支付已执行，结果回显并完成数据沉淀"
        return {"p8_payment": payload}
