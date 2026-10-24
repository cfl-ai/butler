"""U63/U64 守护：访问层级矩阵、访客额度、声纹判定。

判据不是「函数能跑」，而是**边界不许越过**：
- 未登录只能文字（多余能力一律不批）；
- 声纹取不到分数等于不匹配（不允许 None 当通过）；
- 未登记的能力必须报错而不是默认放行（fail-closed）。
"""

from __future__ import annotations

import pytest

from ai_butler.core import access


def test_guest_only_has_text_query():
    caps = access.capabilities_of(access.AccessTier.GUEST)
    assert caps == (access.Capability.TEXT_QUERY,)
    # 逐项点名：这些能力都在验收标准 11 的「未登录拒绝」范围内
    for capability in (access.Capability.VOICE_INPUT, access.Capability.VOICE_OUTPUT,
                       access.Capability.WAKE_WORD, access.Capability.SPEAKERPRINT,
                       access.Capability.VISION_INPUT,
                       access.Capability.PRIVATE_RETRIEVAL,
                       access.Capability.DEVICE_REPORT):
        assert not access.authorize(access.AccessTier.GUEST, capability).allowed


def test_member_has_every_capability():
    assert set(access.capabilities_of(access.AccessTier.MEMBER)) == set(access.Capability)


def test_require_raises_with_machine_readable_code():
    with pytest.raises(access.AccessDenied) as caught:
        access.require(access.AccessTier.GUEST, access.Capability.VOICE_INPUT)
    payload = caught.value.as_dict()
    assert payload["code"] == "capability_required"
    assert payload["capability"] == "voice_input"
    assert payload["need_tier"] == "member"
    assert "登录" in str(caught.value)


def test_tier_is_derived_from_session_identity_not_from_request_body():
    """层级只从 user_id 派生 —— 客户端改 body 改不了身份。"""
    assert access.AccessTier.from_user_id("guest:abc") is access.AccessTier.GUEST
    assert access.AccessTier.from_user_id("user-A") is access.AccessTier.MEMBER
    # 空身份按最严处理：没身份不是「免检」，是访客
    assert access.AccessTier.from_user_id("") is access.AccessTier.GUEST
    assert access.AccessTier.from_user_id(None) is access.AccessTier.GUEST


def test_unknown_capability_is_rejected_instead_of_default_allow():
    """忘登记的新能力必须报错，不能默认放行（fail-closed）。

    故意传一个不在表里的字符串：这模拟「有人加了能力却忘了登记矩阵」。
    如果实现写成 `return True`，这里会静默通过 —— 那正是要防的。
    """
    with pytest.raises(KeyError):
        access.authorize(access.AccessTier.MEMBER, "ghost")  # type: ignore[arg-type]


def test_new_guest_id_is_prefixed_and_unique():
    first, second = access.new_guest_id(), access.new_guest_id()
    assert first.startswith(access.GUEST_ID_PREFIX)
    assert first != second


def test_guest_budget_stops_at_limit():
    clock = [0.0]
    budget = access.GuestBudget(limit=2, clock=lambda: clock[0])
    assert budget.consume("guest:1") == (True, 1)
    assert budget.consume("guest:1") == (True, 0)
    allowed, remaining = budget.consume("guest:1")
    assert (allowed, remaining) == (False, 0)
    # 额度是**按身份**算的，不是全局限额
    assert budget.consume("guest:2")[0] is True


def test_guest_budget_expires_after_ttl():
    clock = [0.0]
    budget = access.GuestBudget(limit=1, clock=lambda: clock[0], ttl_seconds=60)
    assert budget.consume("guest:1")[0] is True
    assert budget.consume("guest:1")[0] is False
    clock[0] = 61.0
    assert budget.consume("guest:1")[0] is True


def test_budget_rejects_non_positive_cost():
    with pytest.raises(ValueError):
        access.GuestBudget().consume("guest:1", cost=0)


def test_voiceprint_missing_score_is_never_a_pass():
    """U63 红线：拿不到分数就判不匹配。None 当通过等于没有声纹。"""
    verdict = access.judge_voiceprint(None)
    assert verdict.matched is False
    assert verdict.score == 0.0
    assert "未取得" in verdict.reason


def test_voiceprint_threshold_boundary():
    below = access.judge_voiceprint(0.349, threshold=0.35, speaker_id="user-A")
    at = access.judge_voiceprint(0.35, threshold=0.35, speaker_id="user-A")
    assert below.matched is False
    assert at.matched is True
    assert at.is_self is True
    # 通过但没给 speaker_id，不算「确认是本人」
    assert access.judge_voiceprint(0.9).is_self is False


def test_matrix_is_reported_truthfully():
    matrix = access.describe_matrix()
    assert matrix["guest"] == ["text_query"]
    assert len(matrix["member"]) == len(access.Capability)
