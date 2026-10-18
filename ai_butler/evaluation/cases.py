"""P9 日志沉淀与案例分类。

计划书依据
----------
三、P9「日志自动沉淀、优质 / 错误案例分类；隐私脱敏；
**Jev 错误、Wiki 冲突案例独立分类**」

要点：Jev 错误与 Wiki 冲突**不是普通错误**——它们是 P10 自进化的输入，
所以必须有**独立且稳定**的分类值，不能混进「其他错误」里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_butler.evaluation.redaction import Redactor

#: 优质 / 错误（计划书原话）
CASE_GOOD = "good"
CASE_BAD = "bad"
CASE_KINDS: tuple[str, ...] = (CASE_GOOD, CASE_BAD)

#: 分类。前两类是计划书点名要求独立分类的
CATEGORY_JEV_ERROR = "jev_error"
CATEGORY_WIKI_CONFLICT = "wiki_conflict"
CATEGORY_GENERAL = "general"
CASE_CATEGORIES: tuple[str, ...] = (CATEGORY_JEV_ERROR, CATEGORY_WIKI_CONFLICT,
                                    CATEGORY_GENERAL)


class CaseError(ValueError):
    pass


@dataclass(frozen=True)
class CaseRecord:
    case_id: str
    kind: str
    category: str
    summary: str
    occurred_at: str = ""
    source: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in CASE_KINDS:
            raise CaseError(f"未知案例类型 {self.kind!r}，可选 {list(CASE_KINDS)}")
        if self.category not in CASE_CATEGORIES:
            raise CaseError(f"未知案例分类 {self.category!r}，可选 {list(CASE_CATEGORIES)}")

    def as_dict(self, redactor: Redactor) -> dict[str, Any]:
        """**出口脱敏**：摘要与载荷都可能夹带明文身份信息。"""
        payload = {
            "case_id": self.case_id, "kind": self.kind, "category": self.category,
            "summary": redactor.redact(self.summary),
            "occurred_at": self.occurred_at, "source": self.source,
            "payload": redactor.redact_deep(dict(self.payload)),
        }
        redactor.assert_deep_clean(payload, where=f"案例 {self.case_id}")
        return payload


def classify(*, is_good: bool, jev_mismatch: bool = False,
             wiki_conflict: bool = False) -> tuple[str, str]:
    """判定 (kind, category)。

    分类优先级：**Jev 错误 / Wiki 冲突优先于普通错误**——
    它们是自进化的定向输入，被归到 general 就等于丢了。
    """
    kind = CASE_GOOD if is_good else CASE_BAD
    if is_good:
        return kind, CATEGORY_GENERAL
    if jev_mismatch:
        return kind, CATEGORY_JEV_ERROR
    if wiki_conflict:
        return kind, CATEGORY_WIKI_CONFLICT
    return kind, CATEGORY_GENERAL


@dataclass
class CaseDeposit:
    """日志沉淀池。"""

    records: list[CaseRecord] = field(default_factory=list)

    def add(self, record: CaseRecord) -> None:
        self.records.append(record)

    def by_category(self, category: str) -> tuple[CaseRecord, ...]:
        return tuple(r for r in self.records if r.category == category)

    def by_kind(self, kind: str) -> tuple[CaseRecord, ...]:
        return tuple(r for r in self.records if r.kind == kind)

    @property
    def good_count(self) -> int:
        return len(self.by_kind(CASE_GOOD))

    @property
    def bad_count(self) -> int:
        return len(self.by_kind(CASE_BAD))

    def support_ratio(self) -> float:
        """优质案例占比。数据够不够训，先看这个。"""
        total = len(self.records)
        return self.good_count / total if total else 0.0

    def as_dict(self, redactor: Redactor) -> dict[str, Any]:
        payload = {
            "total": len(self.records),
            "good": self.good_count,
            "bad": self.bad_count,
            "support_ratio": round(self.support_ratio(), 6),
            "by_category": {c: len(self.by_category(c)) for c in CASE_CATEGORIES},
            "records": [r.as_dict(redactor) for r in self.records],
        }
        redactor.assert_deep_clean(payload, where="日志沉淀池")
        return payload


def deposit_from_logs(rows: Sequence[Mapping[str, Any]]) -> CaseDeposit:
    """把原始日志行沉淀成案例。"""
    pool = CaseDeposit()
    for index, row in enumerate(rows, start=1):
        kind, category = classify(
            is_good=bool(row.get("is_good")),
            jev_mismatch=bool(row.get("jev_mismatch")),
            wiki_conflict=bool(row.get("wiki_conflict")))
        pool.add(CaseRecord(
            case_id=str(row.get("case_id") or f"case-{index:05d}"),
            kind=kind, category=category,
            summary=str(row.get("summary") or ""),
            occurred_at=str(row.get("occurred_at") or ""),
            source=str(row.get("source") or ""),
            payload=dict(row.get("payload") or {})))
    return pool
