"""P8 智能支付 Agent 测试。

计划书依据：三、P8 七项开发步骤 + 固定触发流程 + 表注（强绑定 user_id / 行级隔离 / 私有 Vault）
+ 裁定 U07（三档预算阈值，开源借鉴 Actual Budget 规则模型）
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from ai_butler.agent_wiki.ontology import EVIDENCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.agents.base import resolve, SubAgent
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_PAYMENT,
    CAP_WIKI_WRITE,
    CapabilityError,
    grant,
)
from ai_butler.agents.facts import FactWriter
from ai_butler.agents.p6_content_qa import ACTION_BLOCK, ACTION_PASS
from ai_butler.agents.p8_payment import (
    DEFAULT_TIER,
    MODE_FREE,
    MODE_MANUAL,
    MODES,
    TIER_HIGH,
    TIER_LOW,
    TIER_MEDIUM,
    TIER_POLICIES,
    TIERS,
    Bill,
    PaymentAgent,
    aggregate_bills,
    build_three_tier_plan,
    decide_payment_mode,
)
from ai_butler.agents.state import new_state

TODAY = date(2026, 10, 16)


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


def _bill(bill_id="b1", *, amount=100.0, user_id="user-A", channel="alipay",
          discount=0.0, evidence=("ev-1",), payee="某商户"):
    return Bill(bill_id=bill_id, user_id=user_id, amount=amount, payee=payee,
                occurred_on=TODAY, channel=channel, discount=discount,
                evidence_ids=tuple(evidence))


# ============================== 档位与阈值（U07）============================== #
def test_tiers_and_modes_match_the_plan():
    assert TIERS == ("low", "medium", "high")
    assert MODES == ("free", "manual")
    assert DEFAULT_TIER == TIER_MEDIUM, "计划书：中预算…（默认）"


def test_thresholds_come_from_decision_u07_not_invented():
    """U07：中预算 单笔 ≤1000 免密 + 单日累计 ≤3000 封顶；低预算阈值 0；高预算用月剩余比例。"""
    low = TIER_POLICIES[TIER_LOW]
    assert low.per_transaction_free_limit == 0.0 and low.daily_cap == 0.0
    assert low.requires_manual_confirmation is True

    medium = TIER_POLICIES[TIER_MEDIUM]
    assert medium.per_transaction_free_limit == 1000.0
    assert medium.daily_cap == 3000.0
    assert medium.requires_manual_confirmation is False

    high = TIER_POLICIES[TIER_HIGH]
    assert high.per_transaction_free_limit is None, "高预算不设固定金额"
    assert high.uses_monthly_remaining_ratio is True


def test_unknown_tier_is_rejected():
    from ai_butler.agents.p8_payment import TierPolicy
    with pytest.raises(ValueError):
        TierPolicy(tier="luxury", per_transaction_free_limit=1, daily_cap=None,
                   requires_manual_confirmation=False,
                   uses_monthly_remaining_ratio=False, note="")


# ============================== 账单 ============================== #
def test_bill_requires_user_id_and_valid_amounts():
    with pytest.raises(ValueError):
        _bill(user_id="")
    with pytest.raises(ValueError):
        _bill(amount=-1)
    with pytest.raises(ValueError):
        _bill(amount=100, discount=200)


def test_bill_payable_applies_discount():
    assert _bill(amount=100, discount=30).payable == 70.0


def test_aggregate_sums_totals_and_channels():
    agg = aggregate_bills(
        [_bill("b1", amount=100, discount=10, channel="alipay"),
         _bill("b2", amount=50, channel="wechat")], user_id="user-A")
    assert agg.count == 2 and agg.total == 150.0
    assert agg.discount_total == 10.0 and agg.payable_total == 140.0
    assert agg.channels == ("alipay", "wechat")


def test_aggregate_refuses_another_accounts_bill():
    """计划书三、P8 表注：账单交易记录行级隔离。"""
    with pytest.raises(PermissionError) as ei:
        aggregate_bills([_bill(user_id="user-B")], user_id="user-A")
    assert "行级隔离" in str(ei.value)


# ============================== 支付模式判定 ============================== #
def test_low_tier_always_requires_manual_confirmation():
    d = decide_payment_mode(policy=TIER_POLICIES[TIER_LOW], amount=1.0)
    assert d.mode == MODE_MANUAL and d.requires_user_confirmation is True


def test_medium_tier_free_below_limit_and_manual_above():
    policy = TIER_POLICIES[TIER_MEDIUM]
    assert decide_payment_mode(policy=policy, amount=1000.0).mode == MODE_FREE
    above = decide_payment_mode(policy=policy, amount=1000.01)
    assert above.mode == MODE_MANUAL and above.requires_user_confirmation is True


def test_medium_tier_daily_cap_forces_confirmation():
    d = decide_payment_mode(policy=TIER_POLICIES[TIER_MEDIUM], amount=500.0,
                            spent_today=2800.0)
    assert d.mode == MODE_MANUAL and "单日封顶" in d.reason


def test_high_tier_uses_monthly_remaining_ratio():
    policy = TIER_POLICIES[TIER_HIGH]
    ok = decide_payment_mode(policy=policy, amount=500.0, monthly_budget=10000.0,
                             spent_this_month=1000.0)
    assert ok.mode == MODE_FREE and "聚合一键付清" in ok.reason

    over = decide_payment_mode(policy=policy, amount=500.0, monthly_budget=1000.0,
                               spent_this_month=900.0)
    assert over.mode == MODE_MANUAL and "月度预算剩余" in over.reason


def test_high_tier_without_monthly_budget_falls_back_to_manual():
    d = decide_payment_mode(policy=TIER_POLICIES[TIER_HIGH], amount=10.0)
    assert d.mode == MODE_MANUAL


def test_free_quota_is_an_account_private_switch():
    """计划书三、P8 步骤3「免密额度属于账号私有配置」。"""
    d = decide_payment_mode(policy=TIER_POLICIES[TIER_MEDIUM], amount=100.0,
                            free_quota_enabled=False)
    assert d.mode == MODE_MANUAL and "账号私有配置" in d.reason


def test_decide_rejects_negative_amount():
    with pytest.raises(ValueError):
        decide_payment_mode(policy=TIER_POLICIES[TIER_MEDIUM], amount=-1)


def test_three_tier_plan_covers_every_tier():
    agg = aggregate_bills([_bill(amount=500)], user_id="user-A")
    plans = build_three_tier_plan(aggregate=agg)
    assert set(plans) == set(TIERS)
    assert plans[TIER_LOW].decision.mode == MODE_MANUAL
    assert plans[TIER_MEDIUM].decision.mode == MODE_FREE
    assert all(p.total == 500.0 for p in plans.values())


# ============================== 支付能力授权模型 ============================== #
def test_vertical_agents_may_not_hold_payment_capability():
    class _Vertical(SubAgent):
        name = "travel_agent"
        writes = ("x",)
        capabilities = (CAP_PAYMENT,)

        def run(self, state):
            return {}

    with pytest.raises(CapabilityError) as ei:
        grant(_Vertical, {})
    assert "支付 Agent 中转" in str(ei.value)


def test_the_payment_agent_itself_may_hold_it():
    """禁令针对垂类 Agent；支付 Agent 是唯一合法持有者，须显式声明。"""
    assert PaymentAgent.may_hold_payment is True
    assert CAP_PAYMENT in PaymentAgent.capabilities
    grant(PaymentAgent, {})          # 不应抛 CapabilityError

    # 同为阶段 Agent，但没声明 may_hold_payment 的就不能拿支付能力
    class _Sibling(PaymentAgent):
        name = "some_other_phase"
        may_hold_payment = False

    with pytest.raises(CapabilityError):
        grant(_Sibling, {})


# ============================== 阶段 Agent ============================== #
@pytest.fixture()
def wiki(settings, workdir):
    store = WikiStore(workdir / "wiki", settings=settings)
    store.write_page(new_page(EVIDENCE, "ev-1", confidence=0.9, user_id="user-A",
                              source_id="src-1", source_text="账单来源",
                              text_unit_id="tu-1"), user_id="user-A")
    return store


def _agent(wiki, **over):
    available = {CAP_WIKI_WRITE: over.get("writer", FactWriter(wiki)),
                 CAP_DATA_READ: over.get("sdk"),
                 CAP_PAYMENT: over.get("gateway")}
    return PaymentAgent(settings=over.get("settings"),
                        tools=grant(PaymentAgent, available))


def _state(**extra):
    state = new_state(user_id="user-A", session_id="s1")
    state.update(extra)
    return state


def _qa(action=ACTION_PASS, alerts=()):
    return {"action": action, "alerts": list(alerts)}


def test_agent_blocks_when_session_is_invalid(wiki):
    assert _agent(wiki)(_state(login_status=False)) == {}


def test_agent_requires_a_qa_verdict(wiki):
    """固定触发流程要求「质检校验通过」；没有结论也不能跳过。"""
    out = _call(_agent(wiki), _state(bills=[{"bill_id": "b1", "amount": 100,
                                             "payee": "商户", "occurred_on": TODAY.isoformat()}]))
    payload = out["p8_payment"]
    assert payload["executed"] is False
    assert payload["qa_gate"]["passed"] is False
    assert "强制送检" in payload["qa_gate"]["reason"]


def test_agent_refuses_when_qa_blocks(wiki):
    out = _call(_agent(wiki), _state(
        bills=[{"bill_id": "b1", "amount": 100, "payee": "商户",
                "occurred_on": TODAY.isoformat()}],
        qa_verdict=_qa(ACTION_BLOCK, ["混入他人隐私"])))
    payload = out["p8_payment"]
    assert payload["executed"] is False and "质检拦截" in payload["qa_gate"]["reason"]


def test_agent_only_plans_when_no_tier_selected(wiki):
    out = _call(_agent(wiki), _state(
        bills=[{"bill_id": "b1", "amount": 500, "payee": "商户",
                "occurred_on": TODAY.isoformat()}],
        qa_verdict=_qa()))
    payload = out["p8_payment"]
    assert payload["executed"] is False
    assert set(payload["plans"]) == set(TIERS)
    assert "尚未选择档位" in payload["reason"]


def test_agent_executes_free_payment_and_writes_vault_claims(wiki):
    out = _call(_agent(wiki), _state(
        bills=[{"bill_id": "b1", "amount": 500, "payee": "某商户",
                "occurred_on": TODAY.isoformat(), "evidence_ids": ["ev-1"]}],
        qa_verdict=_qa(), selected_tier=TIER_MEDIUM))
    payload = out["p8_payment"]

    assert payload["executed"] is True and payload["mode"] == MODE_FREE
    assert payload["claims"], "消费事实应写入私有 Vault"
    claim_id = payload["claims"][0]["claim_page_id"]
    page = wiki.read_page(claim_id, user_id="user-A")
    assert page is not None and page.extra["evidence_ids"] == ["ev-1"]
    assert wiki.read_page(claim_id, user_id="user-B") is None, "不得写进他人 Vault"

    assert payload["profile_update"]["user_id"] == "user-A"
    assert "只读" in payload["profile_update"]["note"]


def test_agent_requires_confirmation_for_large_amount(wiki):
    state = _state(
        bills=[{"bill_id": "b1", "amount": 5000, "payee": "商户",
                "occurred_on": TODAY.isoformat(), "evidence_ids": ["ev-1"]}],
        qa_verdict=_qa(), selected_tier=TIER_MEDIUM)
    payload = _call(_agent(wiki), state)["p8_payment"]
    assert payload["executed"] is False
    assert payload["requires_user_confirmation"] is True
    assert "人工确认" in payload["reason"]

    state["user_confirmed"] = True
    payload2 = _call(_agent(wiki), state)["p8_payment"]
    assert payload2["executed"] is True and payload2["mode"] == MODE_MANUAL


def test_agent_low_tier_needs_confirmation_even_for_tiny_amounts(wiki):
    state = _state(
        bills=[{"bill_id": "b1", "amount": 1, "payee": "商户",
                "occurred_on": TODAY.isoformat(), "evidence_ids": ["ev-1"]}],
        qa_verdict=_qa(), selected_tier=TIER_LOW)
    payload = _call(_agent(wiki), state)["p8_payment"]
    assert payload["executed"] is False and payload["requires_user_confirmation"] is True


def test_agent_refuses_cross_account_bill(wiki):
    out = _call(_agent(wiki), _state(
        bills=[{"bill_id": "b1", "amount": 100, "payee": "商户",
                "occurred_on": TODAY.isoformat(), "user_id": "user-B"}],
        qa_verdict=_qa(), selected_tier=TIER_MEDIUM))
    payload = out["p8_payment"]
    assert payload["executed"] is False and "行级隔离" in payload["reason"]


def test_agent_reports_unknown_tier(wiki):
    out = _call(_agent(wiki), _state(
        bills=[{"bill_id": "b1", "amount": 100, "payee": "商户",
                "occurred_on": TODAY.isoformat()}],
        qa_verdict=_qa(), selected_tier="luxury"))
    assert "未知档位" in out["p8_payment"]["reason"]


def test_agent_without_evidence_does_not_fabricate_claims(wiki):
    """没有证据的账单不产出 Claim——不为了凑数造无证据声明。"""
    out = _call(_agent(wiki), _state(
        bills=[{"bill_id": "b1", "amount": 100, "payee": "商户",
                "occurred_on": TODAY.isoformat()}],
        qa_verdict=_qa(), selected_tier=TIER_MEDIUM))
    assert out["p8_payment"]["executed"] is True
    assert out["p8_payment"]["claims"] == []


def test_agent_writes_only_its_own_field(wiki):
    out = _call(_agent(wiki), _state(bills=[], qa_verdict=_qa()))
    assert set(out) <= set(PaymentAgent.writes)


def test_p8_is_now_marked_implemented():
    from ai_butler.core import phases
    spec = phases.phase("P8")
    assert spec.implemented is True
    assert spec.title == "智能支付 Agent｜全生态统一预算与结算体系"
