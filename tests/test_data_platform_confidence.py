"""P1 置信度打分与权重衰减测试。

计划书依据：三、P1「实现数据置信度打分算法；置信分数融合 Agent-Wiki 证据权重」
            三、P1「开发用户偏好权重衰减机制，同步更新私有 Vault 实体属性」
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from ai_butler.agent_wiki.ontology import ENTITY, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.data_platform.confidence import (
    EVIDENCE_INFLUENCE_CAP,
    ConfidenceError,
    EvidenceRef,
    aggregate_evidence_score,
    score_confidence,
)
from ai_butler.data_platform.decay import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_MIN_WEIGHT,
    WEAK_WEIGHT_THRESHOLD,
    DecayError,
    apply_decay,
    decay_preferences,
    decay_weight,
    elapsed_days_since,
    sync_weights_to_vault,
)


# ------------------------------ 置信度打分 ------------------------------ #
def test_no_evidence_means_confidence_equals_base():
    """证据不足时回落到基础分——不做任何推测性抬升。"""
    r = score_confidence(base=0.6, evidences=[])
    assert r.confidence == 0.6
    assert r.influence == 0.0 and r.coverage == 0.0
    assert r.evidence_missing is True


def test_evidence_pulls_confidence_toward_evidence_score():
    strong = [EvidenceRef("e1", weight=3.0, reliability=0.95)]
    r = score_confidence(base=0.3, evidences=strong)
    assert r.coverage == 1.0                                     # 权重达到饱和
    assert r.influence == EVIDENCE_INFLUENCE_CAP                 # 影响力打满上限
    assert r.confidence > 0.3, "高可信证据应当抬升置信度"
    assert r.confidence == pytest.approx(0.3 * (1 - EVIDENCE_INFLUENCE_CAP)
                                         + 0.95 * EVIDENCE_INFLUENCE_CAP)
    assert r.evidence_count == 1


def test_evidence_fusion_can_lift_a_moderate_base_over_the_persist_threshold(settings):
    """设计不变量：中等基础分 + 高可信证据必须能跨过【八】的入库阈值。

    否则「置信分数融合 Agent-Wiki 证据权重」（三、P1）就是空话。
    """
    threshold = settings.jev_confidence_threshold
    r = score_confidence(base=0.5, evidences=[EvidenceRef("e", weight=3.0, reliability=0.95)])
    assert r.confidence >= threshold, f"证据融合后 {r.confidence} 仍低于阈值 {threshold}"


def test_weak_base_cannot_be_rescued_by_evidence_alone(settings):
    """反面不变量：基础分过低时，证据不得单独把结论顶过入库阈值。"""
    threshold = settings.jev_confidence_threshold
    r = score_confidence(base=0.2, evidences=[EvidenceRef("e", weight=3.0, reliability=1.0)])
    assert r.confidence < threshold, "证据不应凭空造出高置信结论"


def test_more_evidence_weight_means_more_influence():
    low = score_confidence(base=0.5, evidences=[EvidenceRef("e1", weight=0.5, reliability=1.0)])
    high = score_confidence(base=0.5, evidences=[EvidenceRef("e1", weight=3.0, reliability=1.0)])
    assert low.influence < high.influence
    assert low.confidence < high.confidence


def test_zero_weight_evidence_is_treated_as_no_evidence():
    r = score_confidence(base=0.4, evidences=[EvidenceRef("e1", weight=0.0, reliability=1.0)])
    assert r.confidence == 0.4 and r.coverage == 0.0


def test_confidence_inputs_are_validated():
    with pytest.raises(ConfidenceError):
        score_confidence(base=1.5)
    with pytest.raises(ConfidenceError):
        EvidenceRef("e", reliability=2.0)
    with pytest.raises(ConfidenceError):
        EvidenceRef("e", weight=-1)


def test_aggregate_evidence_score():
    assert aggregate_evidence_score([]) == 0.0
    refs = [EvidenceRef("a", weight=1, reliability=0.8), EvidenceRef("b", weight=3, reliability=0.4)]
    assert aggregate_evidence_score(refs) == pytest.approx((0.8 + 1.2) / 4)


# ------------------------------ 权重衰减 ------------------------------ #
def test_half_life_halves_the_weight():
    assert decay_weight(1.0, elapsed_days=DEFAULT_HALF_LIFE_DAYS,
                        half_life_days=DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.5)
    assert decay_weight(1.0, elapsed_days=DEFAULT_HALF_LIFE_DAYS * 2,
                        half_life_days=DEFAULT_HALF_LIFE_DAYS) == pytest.approx(0.25)


def test_weight_never_falls_below_the_floor():
    very_old = decay_weight(1.0, elapsed_days=100000, half_life_days=1)
    assert very_old == DEFAULT_MIN_WEIGHT


def test_no_elapsed_time_means_no_decay():
    assert decay_weight(0.7, elapsed_days=0, half_life_days=30) == 0.7
    assert decay_weight(0.7, elapsed_days=-5, half_life_days=30) == 0.7


def test_decay_inputs_are_validated():
    with pytest.raises(DecayError):
        decay_weight(-1, elapsed_days=1, half_life_days=30)
    with pytest.raises(DecayError):
        decay_weight(1, elapsed_days=1, half_life_days=0)


def test_elapsed_days_handles_naive_datetimes():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    naive = datetime(2026, 10, 6)
    assert elapsed_days_since(naive, now=now) == pytest.approx(10.0)


@dataclass
class _Pref:
    id: int
    user_id: str
    attribute: str
    weight: float
    updated_at: datetime
    vault_entity_id: str | None = None


def _pref(pid, *, weight=1.0, days_ago=0.0, user_id="user-A", vault_entity_id=None):
    return _Pref(pid, user_id, f"偏好{pid}", weight,
                 datetime(2026, 10, 16, tzinfo=timezone.utc) - timedelta(days=days_ago),
                 vault_entity_id)


def test_decay_preferences_reports_each_record():
    records = [_pref(1, days_ago=0), _pref(2, days_ago=DEFAULT_HALF_LIFE_DAYS)]
    out = decay_preferences(records, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert out[0].new_weight == pytest.approx(1.0)
    assert out[1].new_weight == pytest.approx(0.5)
    assert out[1].elapsed_days == pytest.approx(DEFAULT_HALF_LIFE_DAYS)


def test_decay_flags_preferences_that_became_weak():
    old = _pref(1, weight=0.9, days_ago=DEFAULT_HALF_LIFE_DAYS * 3)
    out = decay_preferences([old], now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert out[0].new_weight < WEAK_WEIGHT_THRESHOLD
    assert out[0].became_weak is True


def test_apply_decay_writes_back_only_changed_records():
    records = [_pref(1, days_ago=0), _pref(2, days_ago=DEFAULT_HALF_LIFE_DAYS)]
    decayed = decay_preferences(records, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    changed = apply_decay(records, decayed)
    assert changed == 1
    assert records[1].weight == pytest.approx(0.5)


# --------------------- 同步更新私有 Vault 实体属性 --------------------- #
def test_sync_weights_to_vault_updates_the_entity_page(settings, workdir):
    """计划书三、P1「同步更新私有 Vault 实体属性」。"""
    store = WikiStore(workdir / "wiki", settings=settings)
    store.write_page(new_page(ENTITY, "ent-pref-1", title="偏好出行", user_id="user-A",
                              name="偏好出行", entity_type="用户偏好"),
                     user_id="user-A")

    records = [_pref(1, weight=1.0, days_ago=DEFAULT_HALF_LIFE_DAYS, vault_entity_id="ent-pref-1")]
    decayed = decay_preferences(records, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert sync_weights_to_vault(store, "user-A", decayed) == 1

    page = store.read_page("ent-pref-1", user_id="user-A")
    assert page.extra["weight"] == pytest.approx(0.5)


def test_sync_never_touches_another_accounts_vault(settings, workdir):
    """跨账号同步必须被拦住。"""
    store = WikiStore(workdir / "wiki", settings=settings)
    store.write_page(new_page(ENTITY, "ent-b", title="B 的偏好", user_id="user-B",
                              name="B 的偏好", entity_type="用户偏好"),
                     user_id="user-B")

    records = [_pref(1, user_id="user-B", days_ago=DEFAULT_HALF_LIFE_DAYS, vault_entity_id="ent-b")]
    decayed = decay_preferences(records, now=datetime(2026, 10, 16, tzinfo=timezone.utc))

    assert sync_weights_to_vault(store, "user-A", decayed) == 0, "不得跨账号同步"
    page = store.read_page("ent-b", user_id="user-B")
    assert "weight" not in page.extra

    with pytest.raises(DecayError):
        sync_weights_to_vault(store, "", decayed)
