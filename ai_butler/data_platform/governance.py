"""P1 数据治理规则与冲突复核队列。

计划书依据
----------
- 三、P1「实现三条数据治理规则：**高分入库、低分留日志、冲突人工复核**；
  Wiki 检测到矛盾自动送入冲突复核队列」
- 三、P1「Wiki 检测到矛盾自动送入冲突复核队列」
- 三、P6「挂载统一人工审核节点；人工后台只能按 user_id 查看对应账号业务，
  **禁止批量遍历全部用户隐私**」
- 三、P1「冲突标记规则」（models.CONFLICT_*）

阈值口径
--------
「高分」的分界计划书未给出 → **不新造数值**，沿用计划书【八】已有的
jev_confidence_threshold = 0.82（与 Agent-Wiki 低置信报告同一处理方式）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_butler.core.storage import SCOPE_REVIEW, FileStore
from ai_butler.data_platform.models import CONFLICT_NONE

PERSIST = "persist"          # 高分入库
LOG_ONLY = "log_only"        # 低分留日志
REVIEW = "review"            # 冲突人工复核
ACTIONS: tuple[str, ...] = (PERSIST, LOG_ONLY, REVIEW)

PENDING = "pending"
RESOLVED = "resolved"


class GovernanceError(ValueError):
    pass


@dataclass(frozen=True)
class GovernanceDecision:
    action: str
    reason: str
    confidence: float
    conflict_status: str
    threshold: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DataGovernor:
    """三条数据治理规则。"""

    def __init__(self, *, settings) -> None:
        #: 计划书【八】已有的置信阈值，不新造第二个数字
        self.threshold = float(settings.jev_confidence_threshold)

    def decide(self, *, confidence: float, conflict_status: str = CONFLICT_NONE,
               ) -> GovernanceDecision:
        if not 0.0 <= confidence <= 1.0:
            raise GovernanceError(f"置信度必须在 0~1，实际 {confidence!r}")

        # 规则三优先：冲突一律人工复核，不看分数高低
        if conflict_status != CONFLICT_NONE:
            return GovernanceDecision(
                REVIEW, f"冲突标记为 {conflict_status!r}，送入冲突复核队列",
                confidence, conflict_status, self.threshold)

        if confidence >= self.threshold:
            return GovernanceDecision(
                PERSIST, f"置信 {confidence:.3f} >= 阈值 {self.threshold}，高分入库",
                confidence, conflict_status, self.threshold)

        return GovernanceDecision(
            LOG_ONLY, f"置信 {confidence:.3f} < 阈值 {self.threshold}，仅留日志不入库",
            confidence, conflict_status, self.threshold)


# --------------------------------------------------------------------------- #
# 冲突复核队列
# --------------------------------------------------------------------------- #
@dataclass
class ReviewItem:
    item_id: str
    kind: str                       # contradiction / low_confidence / evidence_missing ...
    ref_id: str
    reason: str
    user_id: str | None             # None 表示公共知识库的冲突，不含用户隐私
    status: str = PENDING
    created_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    resolution: str = ""
    resolved_by: str = ""
    resolved_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReviewQueue:
    """冲突复核队列。

    隔离口径：列出待复核项时，
    - 不传 user_id → **只返回公共知识库的项**（user_id 为 None），拿不到任何用户私有项；
    - 传 user_id   → 只返回该账号的项。
    与计划书三、P6「人工后台只能按 user_id 查看对应账号业务，禁止批量遍历全部用户隐私」一致。
    """

    def __init__(self, store: FileStore) -> None:
        self.store = store

    def _path(self, item_id: str):
        return self.store.path(SCOPE_REVIEW, f"{item_id}.json")

    def enqueue(self, *, kind: str, ref_id: str, reason: str, user_id: str | None = None,
                payload: dict[str, Any] | None = None) -> ReviewItem:
        if not kind or not ref_id:
            raise GovernanceError("复核项必须给出 kind 与 ref_id")
        item = ReviewItem(item_id=uuid.uuid4().hex[:16], kind=kind, ref_id=ref_id,
                          reason=reason, user_id=user_id, created_at=_now(),
                          payload=dict(payload or {}))
        path = self._path(item.item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item.as_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        return item

    def enqueue_wiki_contradictions(self, report, *, user_id: str | None = None) -> list[ReviewItem]:
        """计划书三、P1「Wiki 检测到矛盾自动送入冲突复核队列」。

        入参为 agent_wiki.compiler.CompileReport。
        """
        items = []
        for claim_id in getattr(report, "contradictions", ()) or ():
            items.append(self.enqueue(
                kind="contradiction", ref_id=claim_id,
                reason=f"Agent-Wiki 矛盾检测命中：{claim_id}", user_id=user_id,
                payload={"scope": getattr(report, "scope", ""),
                         "page_count": getattr(report, "page_count", 0)}))
        return items

    def _load(self, item_id: str) -> ReviewItem:
        path = self._path(item_id)
        if not path.is_file():
            raise GovernanceError(f"复核项不存在：{item_id}")
        known = set(ReviewItem.__dataclass_fields__)
        return ReviewItem(**{k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                             if k in known})

    def _all(self) -> list[ReviewItem]:
        base = self.store.scope_dir(SCOPE_REVIEW)
        if not base.is_dir():
            return []
        out = []
        for path in sorted(base.glob("*.json")):
            known = set(ReviewItem.__dataclass_fields__)
            out.append(ReviewItem(**{k: v for k, v in json.loads(
                path.read_text(encoding="utf-8")).items() if k in known}))
        return out

    def list_items(self, *, user_id: str | None = None,
                   status: str | None = PENDING) -> list[ReviewItem]:
        """按作用域列出复核项。不传 user_id 时**只返回公共项**。"""
        out = []
        for item in self._all():
            if item.user_id != user_id:
                continue
            if status and item.status != status:
                continue
            out.append(item)
        return out

    def resolve(self, item_id: str, *, resolution: str, by: str = "") -> ReviewItem:
        item = self._load(item_id)
        item.status = RESOLVED
        item.resolution = resolution
        item.resolved_by = by
        item.resolved_at = _now()
        self._path(item_id).write_text(
            json.dumps(item.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return item

    def stats(self) -> dict[str, Any]:
        """只返回聚合计数（人工后台不批量遍历用户隐私）。"""
        items = self._all()
        return {
            "total": len(items),
            "pending": sum(1 for i in items if i.status == PENDING),
            "resolved": sum(1 for i in items if i.status == RESOLVED),
            "by_kind": {k: sum(1 for i in items if i.kind == k)
                        for k in sorted({i.kind for i in items})},
        }
