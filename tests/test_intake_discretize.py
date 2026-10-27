"""U113：用户输入离散化（去状态化）与 Agent / 工具调配的守护。

守的是**语义**，不是实现细节：
1. 有状态 → 无状态：指代必须被补全成自包含值，且**留痕**；
2. 没上下文时**不许猜**：如实标 unresolved，而不是拿默认值顶上；
3. 离散化：一句多件事要拆开，但不能切出无法独立理解的碎片；
4. 调配：查表，**查不到就不派发**，不给「大概归它管」留默认值。
"""

from __future__ import annotations

import pytest

from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_EXTERNAL_READ,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CAP_WIKI_WRITE,
)
from ai_butler.scheduler.intake import (
    ASSIGNMENTS,
    Assignment,
    InformationUnit,
    IntakeContext,
    SlotKind,
    assign,
    detect_slot_kind,
    discretize,
    plan_assignments,
    resolve_references,
    split_units,
)


_CTX = IntakeContext(location="杭州", topic="西湖", prior="上次想去成都",
                     entity_id="ev-1", today="2026-10-27")


# --------------------------------------------------------------------------- #
# ① 去状态化
# --------------------------------------------------------------------------- #
def test_pronoun_is_resolved_and_traced():
    """「那里」必须被补成杭州，且**留痕**说明它原本是个指代。"""
    text, resolved, unresolved, slots = resolve_references("那里天气怎么样", _CTX)
    assert text == "杭州天气怎么样"
    assert resolved == ("那里→杭州",)
    assert unresolved == ()
    assert slots["location"] == "杭州"


def test_no_context_means_unresolved_not_guessed():
    """没有上下文时**如实标未解决**，绝不拿默认值顶上 —— 这是本模块的核心纪律。"""
    text, resolved, unresolved, slots = resolve_references("那里天气怎么样",
                                                           IntakeContext())
    assert unresolved == ("那里",)
    assert resolved == ()
    assert slots == {}
    assert text == "那里天气怎么样"          # 原样保留，不许偷偷替换


def test_relative_date_needs_today():
    """「明天」只有在给了 today 时才翻成日期；没给就如实留着。"""
    text, resolved, _u, slots = resolve_references("明天出发", _CTX)
    assert slots["date"] == "2026-10-28"
    assert "明天→2026-10-28" in resolved

    _t, _r, unresolved, slots2 = resolve_references("明天出发", IntakeContext())
    assert unresolved == ("明天",)
    assert slots2 == {}


# --------------------------------------------------------------------------- #
# ② 离散化
# --------------------------------------------------------------------------- #
def test_multi_intent_is_split():
    units = split_units("查一下天气，顺便帮我订个酒店")
    assert len(units) == 2, units


def test_short_noise_is_not_split():
    """过切会让单元无法独立理解 —— 太短的片段不许单独成单元。"""
    assert split_units("查天气") == ("查天气",)
    assert split_units("顺便") == ("顺便",)


# --------------------------------------------------------------------------- #
# ③ 类型判定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,kind", [
    ("杭州天气怎么样", SlotKind.LOCATION),      # 单字「天」不能把天气判成数量
    ("门票多少钱", SlotKind.QUANTITY),
    ("几点开放", SlotKind.TIME),
    ("订单号 12345 到哪了", SlotKind.ENTITY_ID),
    ("我上次说的那个需求", SlotKind.PRIVATE),
    ("我不吃辣", SlotKind.PREFERENCE),
])
def test_slot_kind_detection(text, kind):
    assert detect_slot_kind(text) is kind


def test_unrecognizable_is_none_not_guessed():
    assert detect_slot_kind("嗯") is SlotKind.NONE


# --------------------------------------------------------------------------- #
# ④ 整体：离散化后的单元自包含
# --------------------------------------------------------------------------- #
def test_units_are_self_contained():
    """带指代的输入在离散化后，每个单元都必须脱离会话也能被理解。"""
    units = discretize("那里天气怎么样", context=_CTX)
    assert len(units) == 1
    unit = units[0]
    assert unit.self_contained is True
    assert "杭州" in unit.text
    assert unit.unresolved == ()


def test_units_without_context_are_flagged():
    units = discretize("那里天气怎么样", context=IntakeContext())
    assert units[0].self_contained is False
    assert units[0].confidence < 1.0          # 有悬空指代 → 置信度下调


def test_empty_input_yields_nothing():
    assert discretize("", context=_CTX) == ()
    assert discretize("   ", context=_CTX) == ()


def test_private_recall_is_treated_as_private_even_without_hint():
    """分类判出「私有回忆」但信息类型判不出时，按**保守侧**归为私有。"""
    units = discretize("个人回忆查询", context=_CTX, intent_label="personal_recall")
    assert units[0].is_private is True


# --------------------------------------------------------------------------- #
# ⑤ 调配
# --------------------------------------------------------------------------- #
def test_assignment_is_found_for_known_combination():
    unit = InformationUnit(text="订单号 12345", origin="", slot_kind=SlotKind.ENTITY_ID,
                           category="identity_lookup")
    got = assign(unit)
    assert got is not None
    assert got.agent == "planner_agent"
    assert CAP_DATA_READ in got.capabilities


def test_unknown_combination_is_not_dispatched():
    """表上没有的组合**返回 None** —— 不给「大概归它管」留默认值。"""
    unit = InformationUnit(text="x", origin="", slot_kind=SlotKind.TOPIC,
                           category="small_talk")
    assert assign(unit) is None


def test_unknown_category_gets_the_full_read_only_set():
    """「未知任务 + 未知信息」给**只读全集**，且**不授予任何写能力**。

    口径在 U119 改过一次，原因如实写在这里：原先只给 `CAP_RETRIEVAL`，
    于是「杭州明天会下雨吗」（判成 unknown/none）拿到一份只有素材检索的清单，
    模型据此回答「我没有天气查询工具」—— 工具注册着、能力也有，被裁剪掉了。
    判不出的语义本来就是「不知道该归谁」，这时给只读全集才对；
    命中具体格子的组合仍然按最小权限给（见其他用例）。
    """
    unit = InformationUnit(text="嗯", origin="", slot_kind=SlotKind.NONE,
                           category="unknown")
    got = assign(unit)
    assert got is not None
    assert set(got.capabilities) == {CAP_RETRIEVAL, CAP_WIKI_READ, CAP_EXTERNAL_READ}
    assert CAP_WIKI_WRITE not in got.capabilities
    assert "payment" not in got.capabilities


def test_private_recall_never_gets_public_write():
    """私有回忆类**绝不允许**拿到写公共知识库的能力。"""
    unit = InformationUnit(text="我上次的需求", origin="", slot_kind=SlotKind.PRIVATE,
                           category="personal_recall")
    got = assign(unit)
    assert got is not None
    assert CAP_WIKI_WRITE not in got.capabilities
    assert CAP_DATA_READ in got.capabilities


def test_plan_assignments_pairs_every_unit():
    units = discretize("我上次说的那个需求", context=_CTX)
    pairs = plan_assignments(units)
    assert len(pairs) == len(units)
    assert all(isinstance(u, InformationUnit) for u, _ in pairs)


def test_assignment_table_uses_only_known_capabilities():
    """调配表里不许出现编造的能力名或 Agent —— 真值源是 capabilities 模块。"""
    from ai_butler.agents.capabilities import CAPABILITIES

    for (_cat, _slot), item in ASSIGNMENTS.items():
        assert isinstance(item, Assignment)
        assert item.agent, "调配必须给出 Agent"
        for cap in item.capabilities:
            assert cap in CAPABILITIES, f"调配表引用了不存在的能力：{cap}"
