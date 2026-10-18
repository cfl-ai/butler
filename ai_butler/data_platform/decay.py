"""P1 用户偏好权重衰减机制。

计划书依据
----------
- 三、P1「开发用户偏好权重衰减机制，**同步更新私有 Vault 实体属性**」
- 三、P1 表注「用户画像实体同步至私有 Vault」

模型口径
--------
计划书未给衰减公式，故属**实现参数**，此处显式登记：采用半衰期模型

    weight(t) = max(最小权重, weight₀ × 0.5 ^ (elapsed / half_life))

取最小权重而非 0：偏好不应因久未触发而彻底消失（可被重新强化）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

#: 半衰期（天）：偏好权重降到一半所需时间（实现参数）
DEFAULT_HALF_LIFE_DAYS = 90.0
#: 权重下限，避免偏好彻底归零（实现参数）
DEFAULT_MIN_WEIGHT = 0.05
#: 衰减后权重低于该值即视为「弱偏好」（实现参数）
WEAK_WEIGHT_THRESHOLD = 0.2


class DecayError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def decay_weight(weight: float, *, elapsed_days: float, half_life_days: float,
                 min_weight: float = DEFAULT_MIN_WEIGHT) -> float:
    """半衰期衰减。elapsed_days <= 0 时原样返回。"""
    if weight < 0:
        raise DecayError("权重不得为负")
    if half_life_days <= 0:
        raise DecayError("半衰期必须为正数")
    if elapsed_days <= 0:
        return weight
    decayed = weight * (0.5 ** (elapsed_days / half_life_days))
    return max(min_weight, min(weight, decayed))


def elapsed_days_since(moment: datetime, *, now: datetime | None = None) -> float:
    now = _as_aware(now or utcnow())
    return (now - _as_aware(moment)).total_seconds() / 86400.0


@dataclass(frozen=True)
class DecayedPreference:
    record_id: int
    user_id: str
    attribute: str
    old_weight: float
    new_weight: float
    elapsed_days: float
    vault_entity_id: str | None = None

    @property
    def became_weak(self) -> bool:
        return self.new_weight < WEAK_WEIGHT_THRESHOLD <= self.old_weight


def decay_preferences(records: Iterable, *, now: datetime | None = None,
                      half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
                      min_weight: float = DEFAULT_MIN_WEIGHT) -> list[DecayedPreference]:
    """对一批用户画像记录计算衰减后的权重（不落库，由调用方决定何时写回）。"""
    now = _as_aware(now or utcnow())
    out: list[DecayedPreference] = []
    for record in records:
        reference = getattr(record, "updated_at", None) or getattr(record, "created_at", None)
        if reference is None:
            raise DecayError(f"记录 {record!r} 缺少 updated_at/created_at，无法计算衰减")
        days = elapsed_days_since(reference, now=now)
        new_weight = decay_weight(record.weight, elapsed_days=days,
                                  half_life_days=half_life_days, min_weight=min_weight)
        out.append(DecayedPreference(
            record_id=record.id, user_id=record.user_id, attribute=record.attribute,
            old_weight=record.weight, new_weight=new_weight, elapsed_days=days,
            vault_entity_id=getattr(record, "vault_entity_id", None)))
    return out


def apply_decay(records: Sequence, decayed: Sequence[DecayedPreference]) -> int:
    """把衰减结果写回 ORM 对象（由 session 负责提交）。返回改动条数。"""
    by_id = {d.record_id: d for d in decayed}
    changed = 0
    for record in records:
        target = by_id.get(record.id)
        if target and abs(target.new_weight - record.weight) > 1e-9:
            record.weight = target.new_weight
            changed += 1
    return changed


def sync_weights_to_vault(store, user_id: str, decayed: Sequence[DecayedPreference]) -> int:
    """计划书三、P1「同步更新私有 Vault 实体属性」。

    只更新属于该 user_id 且带 vault_entity_id 的记录对应页面；
    页面缺失即跳过（不新建页面——新建属采集链路职责）。
    """
    from ai_butler.agent_wiki.ontology import ENTITY

    if not user_id or not str(user_id).strip():
        raise DecayError("同步 Vault 必须携带 user_id")

    updated = 0
    for item in decayed:
        if item.user_id != user_id or not item.vault_entity_id:
            continue
        page = store.read_page(item.vault_entity_id, user_id=user_id)
        if page is None or page.type != ENTITY:
            continue
        page.extra["weight"] = item.new_weight
        store.write_page(page, user_id=user_id)
        updated += 1
    return updated
