"""P0-2 日志检索 · 问题标注 · 缺陷自动统计。

计划书依据
----------
- 三、P0-2「开发日志检索、问题标注、缺陷自动统计工具；**业务接口禁止读取其他用户日志**」
- 三、P0-1「日志脱敏规则：…保留 user_id 用于排障，剥离原始隐私内容」
- 三、P0-9「日志自动沉淀、优质 / 错误案例分类；隐私脱敏；Jev 错误、Wiki 冲突案例独立分类」

隔离设计（本项目最容易做错的一环）
--------------------------------
- **不存在**「按 user_id 之外的条件检索日志正文」的接口；
- 缺陷统计只返回**聚合计数**，不返回任何日志正文；
- 所有写入先过日志脱敏（core/log_safety.py），user_id 按计划书要求保留。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ai_butler.core.log_safety import redact
from ai_butler.core.storage import SCOPE_LOGS, FileStore

#: 计划书三、P0-9：优质 / 错误案例分类；Jev 错误、Wiki 冲突独立分类
CASE_CATEGORIES: tuple[str, ...] = ("优质案例", "错误案例", "Jev错误", "Wiki冲突")


class LogAccessError(RuntimeError):
    """违反「业务接口禁止读取其他用户日志」。"""


@dataclass
class LogRecord:
    record_id: str
    ts: str
    level: str
    event: str
    user_id: str
    session_id: str = ""
    task_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    category: str = ""
    note: str = ""
    annotated_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    def __init__(self, store: FileStore) -> None:
        self.store = store

    # ---------------- 写入 ----------------
    def append(self, *, event: str, level: str, user_id: str, session_id: str = "",
               task_id: str = "", detail: dict[str, Any] | None = None) -> LogRecord:
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"非法日志级别 {level!r}")
        if not user_id or not str(user_id).strip():
            raise LogAccessError("日志必须携带 user_id（计划书：保留 user_id 用于排障）")

        record = LogRecord(
            record_id=uuid.uuid4().hex[:16], ts=_now(), level=level, event=event,
            user_id=str(user_id), session_id=session_id, task_id=task_id,
            # 先脱敏再落盘：计划书三、P0-1 日志脱敏规则
            detail=redact(detail or {}),
        )
        day = record.ts[:10]
        path = self.store.path(SCOPE_LOGS, "audit", f"{day}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
        return record

    # ---------------- 检索 ----------------
    def query(self, *, user_id: str, level: str | None = None, event: str | None = None,
              day: str | None = None, limit: int = 200) -> list[LogRecord]:
        """按用户检索日志。

        user_id 是**必填**参数：计划书明确「业务接口禁止读取其他用户日志」，
        因此本模块不提供任何跨用户检索日志正文的入口。
        """
        if not user_id or not str(user_id).strip():
            raise LogAccessError(
                "日志检索必须携带 user_id：业务接口禁止读取其他用户日志（计划书三、P0-2）"
            )
        out: list[LogRecord] = []
        for record in self._iter(day):
            if record.user_id != user_id:
                continue
            if level and record.level != level:
                continue
            if event and event not in record.event:
                continue
            out.append(record)
        return out[-limit:]

    def _iter(self, day: str | None) -> Iterable[LogRecord]:
        base = self.store.scope_dir(SCOPE_LOGS) / "audit"
        if not base.is_dir():
            return []
        files = sorted(base.glob(f"{day}.jsonl" if day else "*.jsonl"))
        known = set(LogRecord.__dataclass_fields__)
        records: list[LogRecord] = []
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(LogRecord(**{k: v for k, v in json.loads(line).items()
                                                if k in known}))
        return records

    # ---------------- 问题标注 ----------------
    def annotate(self, record_id: str, *, category: str, note: str = "",
                 by: str = "") -> dict[str, Any]:
        """问题标注。计划书三、P0-9 的四类案例分类。"""
        if category not in CASE_CATEGORIES:
            raise ValueError(f"标注分类必须是 {CASE_CATEGORIES} 之一，实际 {category!r}")
        payload = {"record_id": record_id, "category": category,
                   "note": redact(note), "by": by, "ts": _now()}
        # 计划书三、P0-9 的四类分类相互独立，同一记录可同时命中多类，
        # 因此按「记录 + 分类」落盘；重复标注同一 (记录, 分类) 才视为覆盖。
        path = self.store.path(SCOPE_LOGS, "annotations", f"{record_id}__{category}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

    def annotations(self) -> list[dict[str, Any]]:
        base = self.store.scope_dir(SCOPE_LOGS) / "annotations"
        if not base.is_dir():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(base.glob("*.json"))]

    # ---------------- 缺陷自动统计 ----------------
    def defect_stats(self, *, day: str | None = None) -> dict[str, Any]:
        """缺陷自动统计。

        **只返回聚合计数，不返回任何日志正文**——统计口径不构成跨用户读取。
        """
        records = list(self._iter(day))
        by_level = Counter(r.level for r in records)
        by_event = Counter(r.event for r in records)
        failures = [r for r in records if r.level in ("ERROR", "CRITICAL")]
        categories = Counter(a["category"] for a in self.annotations())

        return {
            "total": len(records),
            "unique_users": len({r.user_id for r in records}),
            "by_level": dict(sorted(by_level.items())),
            "top_events": by_event.most_common(10),
            "defect_count": len(failures),
            "defect_events": Counter(r.event for r in failures).most_common(10),
            #: 便于按 user_id 关联排障，但不暴露任何正文
            "defect_user_refs": sorted(
                {hashlib.sha256(r.user_id.encode()).hexdigest()[:16] for r in failures}
            ),
            "annotation_counts": dict(sorted(categories.items())),
        }
