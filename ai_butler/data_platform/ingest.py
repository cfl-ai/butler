"""P1 多源增量数据采集。

计划书依据
----------
- 三、P1「开发多源增量数据采集：**对话、行为、设备、消费数据**；
  写入强制携带上下文 user_id，**增量事实同步生成 Wiki 声明证据**」
- 三、P1 触发时机「用户交互、行为变化、支付完成、场景更新自动增量更新；
  必须携带当前登录用户身份上下文；**同步更新对应私有 Vault 页面**」
- 三、P1 表注「用户画像实体同步至私有 Vault；每一条增量事实生成 Claim 声明并绑定 Evidence 证据」

处理链路（每条增量事实）
----------------------
1. 置信度打分（融合 Agent-Wiki 证据权重）
2. 三条治理规则判定：高分入库 / 低分留日志 / 冲突人工复核
3. 入库时同步在私有 Vault 生成 Evidence 证据页 + Claim 声明页，并把
   用户画像实体（Entity）绑定到该 Claim
4. 全程写变更审计
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from ai_butler.agent_wiki.ontology import CLAIM, ENTITY, EVIDENCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.db import Database
from ai_butler.data_platform.audit import ChangeAudit, snapshot
from ai_butler.data_platform.confidence import EvidenceRef, score_confidence
from ai_butler.data_platform.governance import (
    LOG_ONLY,
    PERSIST,
    REVIEW,
    DataGovernor,
    GovernanceDecision,
    ReviewQueue,
)
from ai_butler.data_platform.models import CONFLICT_NONE, UserProfileRecord

#: 计划书三、P1 逐字点名的四类数据来源
CONVERSATION = "conversation"
BEHAVIOR = "behavior"
DEVICE = "device"
CONSUMPTION = "consumption"
SOURCES: tuple[str, ...] = (CONVERSATION, BEHAVIOR, DEVICE, CONSUMPTION)

PROFILE_SNAPSHOT_FIELDS = ("id", "user_id", "attribute", "value", "category", "confidence",
                           "weight", "conflict_status", "version")


class IngestError(ValueError):
    pass


@dataclass(frozen=True)
class FactEvidence:
    """一条证据：既用于置信度打分，也用于生成 Wiki Evidence 页面。"""

    evidence_id: str
    weight: float = 1.0
    reliability: float = 1.0
    source_id: str = ""
    source_text: str = ""
    text_unit_id: str = ""

    def as_ref(self) -> EvidenceRef:
        return EvidenceRef(self.evidence_id, weight=self.weight, reliability=self.reliability)


@dataclass(frozen=True)
class IncrementFact:
    source: str
    attribute: str
    value: str
    base_confidence: float
    evidences: tuple[FactEvidence, ...] = ()
    category: str = ""
    conflict_status: str = CONFLICT_NONE

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise IngestError(f"数据来源必须是 {SOURCES} 之一，实际 {self.source!r}")
        if not self.attribute or not str(self.attribute).strip():
            raise IngestError("增量事实必须给出 attribute")


@dataclass
class IngestResult:
    user_id: str
    persisted: list[str] = field(default_factory=list)
    logged_only: list[str] = field(default_factory=list)
    reviewing: list[str] = field(default_factory=list)
    wiki_claims: list[str] = field(default_factory=list)
    wiki_evidence: list[str] = field(default_factory=list)
    decisions: list[GovernanceDecision] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.decisions)


def _claim_id(user_id: str, attribute: str) -> str:
    """同一账号同一画像属性对应同一条 Claim，增量即更新（画像语义）。"""
    digest = hashlib.sha256(f"{user_id}|{attribute}".encode("utf-8")).hexdigest()[:16]
    return f"claim-{digest}"


def _entity_id(user_id: str, attribute: str) -> str:
    digest = hashlib.sha256(f"entity|{user_id}|{attribute}".encode("utf-8")).hexdigest()[:16]
    return f"ent-{digest}"


class DataIngestor:
    def __init__(self, *, database: Database, wiki_store: WikiStore, settings,
                 audit: ChangeAudit | None = None, review_queue: ReviewQueue | None = None,
                 governor: DataGovernor | None = None) -> None:
        self._db = database
        self._wiki = wiki_store
        self._audit = audit
        self._review = review_queue
        self._governor = governor or DataGovernor(settings=settings)

    async def ingest(self, *, user_id: str, facts: Sequence[IncrementFact], actor: str = "",
                     now: datetime | None = None) -> IngestResult:
        uid = self._require_user(user_id)
        result = IngestResult(user_id=uid)
        now = now or datetime.now(timezone.utc)

        async with self._db.session_scope(user_id=uid) as session:
            for fact in facts:
                breakdown = score_confidence(base=fact.base_confidence,
                                             evidences=[e.as_ref() for e in fact.evidences])
                decision = self._governor.decide(confidence=breakdown.confidence,
                                                 conflict_status=fact.conflict_status)
                result.decisions.append(decision)

                if decision.action == REVIEW:
                    result.reviewing.append(fact.attribute)
                    self._enqueue_review(uid, fact, decision)
                    continue

                if decision.action == LOG_ONLY:
                    result.logged_only.append(fact.attribute)
                    continue

                record = await self._persist(session, uid, fact, breakdown, now)
                result.persisted.append(fact.attribute)
                self._sync_wiki(uid, fact, record, breakdown.confidence, result, now, actor)

        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_user(user_id: str | None) -> str:
        if not user_id or not str(user_id).strip():
            raise IngestError(
                "增量采集必须携带当前登录用户身份上下文（计划书三、P1 触发时机）"
            )
        return str(user_id)

    def _enqueue_review(self, uid: str, fact: IncrementFact, decision: GovernanceDecision) -> None:
        if self._review is None:
            return
        self._review.enqueue(kind="conflict", ref_id=f"{uid}:{fact.attribute}",
                             reason=decision.reason, user_id=uid,
                             payload={"source": fact.source, "attribute": fact.attribute})

    async def _persist(self, session, uid: str, fact: IncrementFact, breakdown, now) -> UserProfileRecord:
        from sqlalchemy import select

        existing = (await session.execute(
            select(UserProfileRecord).where(UserProfileRecord.attribute == fact.attribute)
        )).scalars().first()

        entity_id = _entity_id(uid, fact.attribute)
        if existing is None:
            record = UserProfileRecord(
                user_id=uid, attribute=fact.attribute, value=fact.value,
                category=fact.category, confidence=breakdown.confidence,
                conflict_status=fact.conflict_status, vault_entity_id=entity_id,
                source_id=fact.evidences[0].source_id if fact.evidences else None)
            session.add(record)
            await session.flush()
            action, before = "create", None
        else:
            before = snapshot(existing, PROFILE_SNAPSHOT_FIELDS)
            existing.value = fact.value
            existing.confidence = breakdown.confidence
            existing.version += 1
            record = existing
            action = "update"

        if self._audit is not None:
            await session.flush()
            self._audit.record(entity="data_user_profile", entity_id=str(record.id),
                               action=action, user_id=uid, before=before,
                               after=snapshot(record, PROFILE_SNAPSHOT_FIELDS), actor="ingest")
        return record

    def _sync_wiki(self, uid: str, fact: IncrementFact, record, confidence: float,
                   result: IngestResult, now: datetime, actor: str) -> None:
        """计划书三、P1：增量事实同步生成 Wiki 声明证据，并更新私有 Vault 页面。"""
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        evidence_ids: list[str] = []

        for ev in fact.evidences:
            page = new_page(EVIDENCE, ev.evidence_id, title=f"证据 {ev.evidence_id}",
                            confidence=ev.reliability, user_id=uid, now=stamp,
                            source_id=ev.source_id or "unknown",
                            source_text=ev.source_text or fact.value,
                            text_unit_id=ev.text_unit_id or ev.evidence_id)
            self._wiki.write_page(page, user_id=uid)
            result.wiki_evidence.append(ev.evidence_id)
            evidence_ids.append(ev.evidence_id)

        entity_id = _entity_id(uid, fact.attribute)
        self._wiki.write_page(new_page(
            ENTITY, entity_id, title=fact.attribute, confidence=confidence, user_id=uid,
            now=stamp, name=fact.attribute, entity_type="用户画像", weight=record.weight),
            user_id=uid)

        claim_id = _claim_id(uid, fact.attribute)
        claim = new_page(CLAIM, claim_id, title=fact.attribute, confidence=confidence,
                         user_id=uid, now=stamp, claim_type="preference",
                         description=f"{fact.attribute}：{fact.value}",
                         subject_id=entity_id, object_id=f"value:{fact.value}",
                         evidence_ids=evidence_ids, source=fact.source)
        self._wiki.write_page(claim, user_id=uid)
        result.wiki_claims.append(claim_id)

        if self._audit is not None:
            self._audit.record_wiki_page(claim, action="create", actor=actor or "ingest")
