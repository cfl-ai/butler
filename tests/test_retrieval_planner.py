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


# --------------------------------------------------------------------------- #
# L1 的一轮实测优化（2026-10-25，U76 前置）
# --------------------------------------------------------------------------- #
#: 这一批全部来自一次 62 句实测里**判错的句子**，每一类都能说出语言依据。
#: 把它们钉成用例，是因为上一轮的错误全都「看起来很像对的」——
#: 没有断言守着，下一次改正则时它们会悄无声息地退回原样。
@pytest.mark.parametrize("query,category,why", [
    # 裸「我的」是限定语，不是回忆诉求：它指向的是对象，不是「上次说过什么」
    ("为什么我的订单还没到", QueryCategory.FACT_QUESTION, "「我的」不得抢成 personal_recall"),
    # 是非问句问的是规则（许不许可），而不是这条订单的状态
    ("订单 2024101200001 能改收货地址吗", QueryCategory.POLICY_RULE, "规则问句须先于标识"),
    ("超过七天还能退货吗", QueryCategory.POLICY_RULE, "「能…吗」是规则诉求"),
    ("那个 5 块钱的券还能用吗", QueryCategory.POLICY_RULE, "「能用吗」是规则诉求"),
    # 「然后」是显式顺承，两件事就是两个诉求
    ("帮我推荐一下，然后怎么下单", QueryCategory.MULTI_INTENT, "顺承连词算并列证据"),
    ("我想改地址，以及查一下物流", QueryCategory.MULTI_INTENT, "「以及」+ 两类诉求"),
    # 「怎么样才能」确实是问步骤，不能因为「怎么样」问状况就把整类排掉
    ("怎么样才能申请退款", QueryCategory.PROCEDURE, "「怎么样才能」= 问步骤"),
    ("发票在哪开", QueryCategory.PROCEDURE, "「在哪开」是操作动词"),
    ("北京到上海高铁要多久", QueryCategory.FACT_QUESTION, "「多久」是事实要素"),
    ("顺丰和京东物流哪个快", QueryCategory.COMPARISON, "「哪个快」是比较诉求"),
    ("给我个方案，怎么部署内网服务", QueryCategory.RECOMMENDATION, "请求型诉求容纳后续细节"),
    ("预算三千买什么手机好", QueryCategory.RECOMMENDATION, "预算约束型推荐"),
    ("你建议我选哪个", QueryCategory.RECOMMENDATION, "「建议我选」是推荐"),
    ("有没有便宜点的", QueryCategory.RECOMMENDATION, "「便宜点」是隐性推荐"),
])
def test_regressions_from_the_2026_10_25_measurement(query, category, why):
    resolved, reason, _ = classify(query)
    assert resolved is category, f"{query!r} → {resolved}，期望 {category}（{why}）；理由：{reason}"


def test_naked_my_never_means_personal_recall():
    """裸「我的」**不再**触发私有回忆——这是本轮最重要的一条收窄。

    「我的发票什么时候能开出来」问的是流程，判成 personal_recall 会去查
    私有 Vault（访客还会被裁掉工具），方向整个错。现在它落到事实要素，
    虽仍不是最优的 procedure，但**不再是方向性错误**。
    """
    for query in ("为什么我的订单还没到", "我的发票什么时候能开出来"):
        resolved, _, _ = classify(query)
        assert resolved is not QueryCategory.PERSONAL_RECALL, query


def test_recall_phrases_still_mean_personal_recall():
    """收窄不能收掉真回忆：带时间指代/回忆动词的仍必须是私有回忆。"""
    for query in ("我上次说的那个方案", "我之前提的那个需求", "我说过要报销的"):
        assert classify(query)[0] is QueryCategory.PERSONAL_RECALL, query


def test_bare_comma_is_deliberately_not_a_multi_intent_separator():
    """裸逗号**刻意不收**：净收益为 0 而体验变差。

    收进来能把「这个多少钱，怎么付款」判成多意图（+1），
    但会把「给我个方案，怎么部署内网服务」也判成多意图（-1）——
    后者是一个诉求加方案细节，反问一次等于白打断用户。
    这条用例的作用是**把这个取舍钉住**，防止后人「顺手」加回逗号。
    """
    assert classify("这个多少钱，怎么付款")[0] is QueryCategory.PROCEDURE
    assert classify("给我个方案，怎么部署内网服务")[0] is QueryCategory.RECOMMENDATION
