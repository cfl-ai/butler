"""P1 数据版本溯源 · 变更审计 · 回溯系统。

计划书依据
----------
- 三、P1「搭建数据版本溯源、变更审计、回溯系统；**审计记录账号变更行为**；
  **同步保留 Wiki 页面变更历史**」
- 三、P1「版本溯源」（核心能力之一）

隔离口径
--------
审计记录按 user_id 分域存放：不传 user_id 只能读到公共域（user_id 为 None）的变更；
传 user_id 只能读到该账号的变更。与计划书三、P6「人工后台只能按 user_id 查看对应账号业务」一致。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_butler.agent_wiki.ontology import WikiPage
from ai_butler.core.storage import SCOPE_AUDIT, FileStore

CREATE = "create"
UPDATE = "update"
DELETE = "delete"
ACTIONS: tuple[str, ...] = (CREATE, UPDATE, DELETE)


class AuditError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ChangeEntry:
    change_id: str
    ts: str
    user_id: str | None        # None 表示公共域变更
    entity: str                # 表名 / "wiki_page"
    entity_id: str
    action: str
    version: int
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    actor: str = ""            # 触发方：账号行为 / agent 名 / 系统任务

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChangeAudit:
    def __init__(self, store: FileStore) -> None:
        self.store = store

    # ---------------- 存放 ----------------
    @staticmethod
    def _shard(user_id: str | None) -> str:
        return user_id or "_public"

    def _dir(self, user_id: str | None, entity: str, entity_id: str):
        digest = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:12]
        return self.store.path(SCOPE_AUDIT, self._shard(user_id), entity, digest)

    @staticmethod
    def _safe(part: str) -> str:
        if not part or any(c in part for c in ("/", "\\", "..")):
            raise AuditError(f"非法的实体标识 {part!r}")
        return part

    # ---------------- 记录 ----------------
    def record(self, *, entity: str, entity_id: str, action: str,
               user_id: str | None = None, before: dict[str, Any] | None = None,
               after: dict[str, Any] | None = None, actor: str = "") -> ChangeEntry:
        self._safe(entity)
        self._safe(entity_id)
        if action not in ACTIONS:
            raise AuditError(f"变更动作必须是 {ACTIONS} 之一，实际 {action!r}")

        directory = self._dir(user_id, entity, entity_id)
        history = self.history(user_id=user_id, entity=entity, entity_id=entity_id)
        version = (history[-1].version + 1) if history else 1

        entry = ChangeEntry(change_id=uuid.uuid4().hex[:16], ts=_now(), user_id=user_id,
                            entity=entity, entity_id=entity_id, action=action,
                            version=version, before=before, after=after, actor=actor)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"v{version:04d}.json").write_text(
            json.dumps(entry.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return entry

    # ---------------- 查询 ----------------
    def _iter_shard(self, user_id: str | None):
        base = self.store.scope_dir(SCOPE_AUDIT) / self._shard(user_id)
        if not base.is_dir():
            return []
        return sorted(base.rglob("v*.json"))

    def history(self, *, user_id: str | None = None, entity: str | None = None,
                entity_id: str | None = None) -> list[ChangeEntry]:
        """按作用域读取变更历史。不传 user_id → 只读公共域。"""
        known = set(ChangeEntry.__dataclass_fields__)
        out: list[ChangeEntry] = []
        for path in self._iter_shard(user_id):
            entry = ChangeEntry(**{k: v for k, v in json.loads(
                path.read_text(encoding="utf-8")).items() if k in known})
            if entry.user_id != user_id:
                continue
            if entity and entry.entity != entity:
                continue
            if entity_id and entry.entity_id != entity_id:
                continue
            out.append(entry)
        return sorted(out, key=lambda e: e.version)

    def latest(self, *, entity: str, entity_id: str,
               user_id: str | None = None) -> ChangeEntry | None:
        history = self.history(user_id=user_id, entity=entity, entity_id=entity_id)
        return history[-1] if history else None

    def stats(self) -> dict[str, Any]:
        """只返回聚合计数，不暴露任何账号明细。"""
        known = set(ChangeEntry.__dataclass_fields__)
        total = 0
        by_action: dict[str, int] = {}
        base = self.store.scope_dir(SCOPE_AUDIT)
        if base.is_dir():
            for path in base.rglob("v*.json"):
                entry = ChangeEntry(**{k: v for k, v in json.loads(
                    path.read_text(encoding="utf-8")).items() if k in known})
                total += 1
                by_action[entry.action] = by_action.get(entry.action, 0) + 1
        return {"total_changes": total, "by_action": dict(sorted(by_action.items()))}

    # ---------------- Wiki 页面变更历史（含回溯） ----------------
    def record_wiki_page(self, page: WikiPage, *, action: str = UPDATE, actor: str = "",
                         before: str | None = None) -> ChangeEntry:
        """记录 Wiki 页面变更。计划书三、P1「同步保留 Wiki 页面变更历史」。"""
        return self.record(entity="wiki_page", entity_id=page.id, action=action,
                           user_id=page.user_id, before={"markdown": before} if before else None,
                           after={"markdown": page.to_markdown()}, actor=actor)

    def wiki_page_history(self, page_id: str, *, user_id: str | None = None) -> list[ChangeEntry]:
        return self.history(user_id=user_id, entity="wiki_page", entity_id=page_id)

    def rollback_wiki_page(self, change_id: str, *, user_id: str | None = None,
                           actor: str = "") -> WikiPage:
        """把某次变更的「变更前」内容写回，并把这次回滚也记为新版本（不删历史）。"""
        history = self.history(user_id=user_id, entity="wiki_page")
        target = next((e for e in history if e.change_id == change_id), None)
        if target is None:
            raise AuditError(f"变更记录不存在或不属于该账号：{change_id}")
        if not target.before or not target.before.get("markdown"):
            raise AuditError(f"变更 {change_id} 没有可回溯的前态（可能是首次创建）")

        restored = WikiPage.parse(target.before["markdown"])
        return restored


def snapshot(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """把 ORM 对象抽成可存快照（避免审计里混入会话状态）。"""
    out: dict[str, Any] = {}
    for name in fields:
        value = getattr(obj, name, None)
        out[name] = value.isoformat() if isinstance(value, datetime) else value
    return out
