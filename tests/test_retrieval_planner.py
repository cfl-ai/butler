"""U66 守护：查询分类 + 工具选择（「具体分类、方法选合适工具」）。

判据三条，都能被证伪：
1. **分类要说得出理由**（`decided_by` + `signals`），不是「模型觉得」；
2. **工具序列由分类唯一决定**，同一分类不允许两处各写一套；
3. **层级裁掉工具必须记账**（`dropped_tools`），不能静默消失。
"""

from __future__ import annotations

import pytest

from ai_butler.core.access import AccessTier
from ai_butler.retrieval import (
    CATEGORY_TOOLS,
    TOOLS,
    PlanError,
    QueryCategory,
    classify,
    describe_categories,
    describe_tools,
    plan,
    tools_for,
)
from ai_butler.retrieval.planner import RetrievalMethod
from ai_butler.material_center.routing import QueryRoute


@pytest.mark.parametrize("query,category", [
    ("订单号 12345678 到哪了", QueryCategory.IDENTITY_LOOKUP),
    ("ERR5002 是什么意思", QueryCategory.IDENTITY_LOOKUP),
    ("v2.3.1 更新了什么", QueryCategory.IDENTITY_LOOKUP),
    ("公司年假几天", QueryCategory.FACT_QUESTION),
    ("净利润同比增长多少", QueryCategory.FACT_QUESTION),
    ("出差能不能报销打车费", QueryCategory.POLICY_RULE),
    ("年假怎么申请", QueryCategory.PROCEDURE),
    ("A 和 B 哪个好", QueryCategory.COMPARISON),
    ("帮我挑个吹风机", QueryCategory.RECOMMENDATION),
    ("我上次说的那个方案", QueryCategory.PERSONAL_RECALL),
    ("你好呀", QueryCategory.SMALL_TALK),
])
def test_classification_is_specific(query, category):
    """每类查询都能落到一个**会改变工具选择**的具体分类。"""
    resolved, reason, signals = classify(query)
    assert resolved is category, f"{query!r} → {resolved}，期望 {category}；理由：{reason}"


def test_classification_always_explains_itself():
    _, reason, signals = classify("订单号 12345678 到哪了")
    assert reason and signals
    assert "编号" in "".join(signals)


def test_empty_query_cannot_be_planned():
    with pytest.raises(PlanError):
        plan("   ")


def test_multi_intent_must_ask_instead_of_picking_one():
    """计划书三、P3：多意图**不自动选择优先级**，必须向用户确认（JEV-003）。"""
    resolved = plan("年假几天？另外怎么报销？", tier=AccessTier.MEMBER)
    assert resolved.category is QueryCategory.MULTI_INTENT
    assert resolved.needs_clarification is True
    assert resolved.clarify_question
    assert resolved.steps[0].method is RetrievalMethod.CLARIFY


def test_identity_lookup_bypasses_llm_on_first_step():
    """精确标识直查是**唯一可能完全不过大模型**的路径。"""
    resolved = plan("订单号 12345678 到哪了", tier=AccessTier.MEMBER)
    assert resolved.steps[0].tool == "structured.lookup"
    assert resolved.bypasses_llm is True
    # 但 fallback 会用到模型：这是「万一编号记错」的补救，不是主路径
    assert resolved.may_use_llm is True


def test_guest_cannot_reach_private_vault_but_plan_still_works():
    guest = plan("我上次说的那个方案", tier=AccessTier.GUEST)
    member = plan("我上次说的那个方案", tier=AccessTier.MEMBER)

    assert "vault.search" in member.tools
    assert "vault.search" not in guest.tools
    # 必须**记账**，否则用户只会看到「查不到」，而不是「去登录」
    assert [d.tool for d in guest.dropped_tools] == ["vault.search"]
    assert "guest" in guest.dropped_tools[0].reason
    # 裁掉 primary 后要有能跑的第一步（降级，而不是空步骤列表）
    assert guest.steps and guest.steps[0].role == "primary"


def test_policy_question_always_needs_evidence():
    """规则类必须引条文：needs_evidence 为真，链路才会在无证据时弃答。"""
    resolved = plan("出差能不能报销打车费", tier=AccessTier.MEMBER)
    assert resolved.category is QueryCategory.POLICY_RULE
    assert resolved.needs_evidence is True


def test_small_talk_does_not_retrieve():
    resolved = plan("你好呀", tier=AccessTier.MEMBER)
    assert resolved.tools == ("model.direct",)
    assert resolved.needs_evidence is False


def test_tool_table_is_the_single_source_of_truth():
    """分类引用的工具必须都在注册表里；注册表里的每个工具都说得清用途。"""
    for category, steps in CATEGORY_TOOLS.items():
        assert steps, f"{category} 没有任何步骤"
        assert steps[0][1] == "primary"
        for tool, role in steps:
            assert tool in TOOLS
            assert role in {"primary", "fallback", "synthesize", "verify"}
    for spec in describe_tools():
        assert spec["description"], f"{spec['name']} 缺用途说明"


def test_route_from_u46_is_consumed_not_rewritten():
    """U46 的路线判定原样带出；本模块不改写它。"""
    resolved = plan("公司年假几天", tier=AccessTier.MEMBER)
    assert resolved.route is not None
    assert resolved.route.route in set(QueryRoute)


def test_llm_fallback_intent_layer_forces_unknown():
    """五层路由已降级到 llm_fallback 时，分类不得装作确定。"""
    resolved, reason, signals = classify("那个东西怎么样了", intent_layer="llm_fallback")
    assert resolved is QueryCategory.UNKNOWN
    assert "llm_fallback" in "".join(signals)


def test_guest_tool_list_excludes_member_only_tools():
    assert "vault.search" not in tools_for(AccessTier.GUEST)
    assert "vault.search" in tools_for(AccessTier.MEMBER)
    # 访客仍有检索能力：不能因为登录墙把文字查询也一起挡了
    assert "retrieval.hybrid_rerank" in tools_for(AccessTier.GUEST)


def test_describe_categories_covers_all_categories():
    described = describe_categories()
    assert set(described) == {c.value for c in QueryCategory}
    for name, payload in described.items():
        assert payload["steps"], f"{name} 没有步骤说明"
