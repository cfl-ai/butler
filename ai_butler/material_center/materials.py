"""P2 素材入库总管道：清洗 → 去重 → 落库 → Wiki 源页面 → 建索引。

计划书依据
----------
- 三、P2「开发多源自动化爬虫采集链路；原始素材自动生成 Agent-Wiki source.* 源页面，保存来源元数据」
- 三、P2「实现文档清洗、去重、分类、时效打标，转换为 Wiki Source 源页面」
- 三、P2「开发**过期素材识别、新旧版本增量融合**；过期来源标记为 status:archived 归档」
- 三、P2「强制所有素材绑定溯源来源，对应 Wiki Evidence 证据条目」

三种入库结果（对应计划书「增量融合」）
------------------------------------
| 情形 | 处理 |
| 内容哈希已存在 | **refresh**：刷新抓取时间，不新建版本 |
| 同一来源 URL，内容已变 | **supersede**：旧版本归档（保留可溯源），新版本入库并重建索引 |
| 全新来源 | **create**：新建 |

公共素材写入素材溯源库（无 user_id）；用户私有素材只写本人 Vault 与私有索引
（计划书三、P1 表注：素材溯源库属于公共 Workspace 无用户 ID）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from sqlalchemy import select

from ai_butler.agent_wiki.ontology import SOURCE_ARCHIVED, WikiPage
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.db import Database
from ai_butler.data_platform.models import SOURCE_ACTIVE, MaterialSourceRecord
from ai_butler.material_center.cleaner import (
    CleanError,
    CleanedDocument,
    RawDocument,
    clean_document,
    deduplicate,
    to_wiki_source_page,
)
from ai_butler.material_center.service import SearchService

CREATE = "create"
REFRESH = "refresh"
SUPERSEDE = "supersede"
ACTIONS: tuple[str, ...] = (CREATE, REFRESH, SUPERSEDE)

#: 过期后再过多少天执行归档（实现参数）
DEFAULT_ARCHIVE_GRACE_DAYS = 7


class MaterialError(RuntimeError):
    pass


def source_key_for(url: str) -> str:
    """素材稳定标识：由 URL 派生，同一来源的多个版本共用。"""
    return "src-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def page_id_for(source_key: str, version: int) -> str:
    return f"{source_key}@v{version}"


@dataclass
class MaterialIngestReport:
    collected: int = 0
    cleaned: int = 0
    duplicates: int = 0
    created: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    wiki_pages: list[str] = field(default_factory=list)
    indexed_chunks: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def stored(self) -> list[str]:
        return self.created + self.refreshed + self.superseded


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class MaterialIngestor:
    def __init__(self, *, database: Database, wiki_store: WikiStore,
                 search_service: SearchService) -> None:
        self._db = database
        self._wiki = wiki_store
        self._search = search_service

    # ------------------------------------------------------------------ #
    async def ingest(self, raw_docs: Sequence[RawDocument], *, user_id: str | None = None,
                     now: datetime | None = None) -> MaterialIngestReport:
        moment = now or datetime.now(timezone.utc)
        report = MaterialIngestReport(collected=len(raw_docs))

        # 逐条清洗以隔离单条失败，但**去重必须在整批上做**——
        # 若逐条去重，批次里永远只有一条，跨文档重复内容无从发现。
        survivors: list[CleanedDocument] = []
        for raw in raw_docs:
            try:
                survivors.append(clean_document(raw, now=moment))
            except CleanError as exc:
                report.failures.append((raw.doc_id, str(exc)))
        cleaned = deduplicate(survivors)
        report.duplicates = sum(1 for d in cleaned if d.is_duplicate)
        unique = [d for d in cleaned if not d.is_duplicate]
        report.cleaned = len(unique)

        async with self._db.session_scope(user_id=user_id) as session:
            for doc in unique:
                try:
                    action = await self._store(session, doc, user_id=user_id, now=moment,
                                               report=report)
                except MaterialError as exc:
                    report.failures.append((doc.doc_id, str(exc)))
                    continue
                getattr(report, {"create": "created", "refresh": "refreshed",
                                 "supersede": "superseded"}[action]).append(doc.doc_id)
        return report

    # ------------------------------------------------------------------ #
    async def _store(self, session, doc: CleanedDocument, *, user_id: str | None,
                     now: datetime, report: MaterialIngestReport) -> str:
        source_key = source_key_for(doc.url)
        stale_after = _parse(doc.stale_after)

        if user_id is None:
            action, version = await self._upsert_row(session, doc, source_key, stale_after,
                                                     now=now)
        else:
            # 素材溯源库属于公共 Workspace 无 user_id ⇒ 私有素材只进 Vault 与私有索引
            action, version = CREATE, 1

        page_id = page_id_for(source_key, version)
        page = to_wiki_source_page(doc, user_id=user_id, now=now.isoformat())
        page.id = page_id
        self._wiki.write_page(page, user_id=user_id)
        report.wiki_pages.append(page_id)

        self._search.retriever.remove_document(page_id, user_id=user_id)
        report.indexed_chunks += self._search.index_text(
            doc_id=page_id, text=doc.text, url=doc.url, user_id=user_id,
            page_id=page_id, confidence=None)
        return action

    async def _upsert_row(self, session, doc: CleanedDocument, source_key: str,
                          stale_after: datetime | None, *, now: datetime) -> tuple[str, int]:
        same_content = (await session.execute(
            select(MaterialSourceRecord).where(
                MaterialSourceRecord.content_hash == doc.content_hash)
        )).scalars().first()
        if same_content is not None:
            same_content.retrieved_at = now
            same_content.status = SOURCE_ACTIVE
            same_content.archived_at = None
            await session.flush()
            return REFRESH, same_content.version

        active_same_source = (await session.execute(
            select(MaterialSourceRecord).where(
                MaterialSourceRecord.source_key == source_key,
                MaterialSourceRecord.status == SOURCE_ACTIVE)
        )).scalars().all()

        version = 1
        for old in active_same_source:
            await self._archive_row(session, old, now=now)
            version = max(version, old.version + 1)
            old_page_id = page_id_for(source_key, old.version)
            self._archive_wiki_page(old_page_id)
            self._search.retriever.remove_document(old_page_id, user_id=None)

        session.add(MaterialSourceRecord(
            source_key=source_key, version=version, url=doc.url, title=doc.title,
            publisher=doc.publisher, retrieved_at=now, status=SOURCE_ACTIVE,
            content_hash=doc.content_hash, category=doc.category, stale_after=stale_after,
            published_at=_parse(doc.published_at)))
        await session.flush()
        return (SUPERSEDE if active_same_source else CREATE), version

    @staticmethod
    async def _archive_row(session, row: MaterialSourceRecord, *, now: datetime) -> None:
        row.status = SOURCE_ARCHIVED
        row.archived_at = now
        await session.flush()

    def _archive_wiki_page(self, page_id: str) -> None:
        page = self._wiki.read_page(page_id)
        if page is None:
            return
        page.status = SOURCE_ARCHIVED
        page.extra["archived_at"] = datetime.now(timezone.utc).isoformat()
        self._wiki.write_page(page)

    # ------------------------------------------------------------------ #
    async def archive_stale(self, *, now: datetime | None = None,
                            grace_days: int = DEFAULT_ARCHIVE_GRACE_DAYS) -> list[str]:
        """过期素材识别与归档。计划书三、P2「过期来源标记为 status:archived 归档」。"""
        moment = now or datetime.now(timezone.utc)
        archived: list[str] = []
        async with self._db.session_scope(user_id=None) as session:
            rows = (await session.execute(
                select(MaterialSourceRecord).where(
                    MaterialSourceRecord.status == SOURCE_ACTIVE)
            )).scalars().all()
            for row in rows:
                if row.stale_after is None:
                    continue
                deadline = row.stale_after
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if moment < deadline + timedelta(days=grace_days):
                    continue
                await self._archive_row(session, row, now=moment)
                page_id = page_id_for(row.source_key, row.version)
                self._archive_wiki_page(page_id)
                self._search.retriever.remove_document(page_id, user_id=None)
                archived.append(row.source_key)
        return archived
