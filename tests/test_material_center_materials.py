"""P2 素材入库管道与过期归档测试。

计划书依据：三、P2「文档清洗、去重、分类、时效打标，转换为 Wiki Source 源页面」
+「过期素材识别、新旧版本增量融合；过期来源标记为 status:archived 归档」
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from ai_butler.agent_wiki.ontology import SOURCE, SOURCE_ARCHIVED
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.db import Base, Database
from ai_butler.data_platform.models import (
    DeviceStateRecord,
    KnowledgeGraphRecord,
    MaterialSourceRecord,
    SOURCE_ACTIVE,
    UserProfileRecord,
)
from ai_butler.material_center.chunking import ChunkingRule
from ai_butler.material_center.cleaner import RawDocument
from ai_butler.material_center.embedding import HashingEmbedder
from ai_butler.material_center.materials import (
    ACTIONS,
    CREATE,
    REFRESH,
    SUPERSEDE,
    MaterialIngestor,
    page_id_for,
    source_key_for,
)
from ai_butler.material_center.retrieval import BM25Store, HybridRetriever
from ai_butler.material_center.service import SearchService
from ai_butler.material_center.vector_store import ChromaVectorStore

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
URL = "https://travel.example.com/xihu"

V1 = "西湖景区公告：开放时间调整为每日六点三十分，春季最佳游览季节。"
V2 = "西湖景区公告：冬季开放时间调整为每日七点，门票需提前预约。"


@pytest.fixture(scope="module")
def loop():
    """模块级事件循环。

    必须 set_event_loop：同进程内其他用例调用 asyncio.run() 后会把当前事件循环置空，
    若这里只 new_event_loop() 而不设置，asyncpg 在某些代码路径上会去取「当前」循环，
    产生只在整包连跑时才冒头的偶发失败（Event loop is closed / 连接挂在别的循环上）。
    """
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


@pytest.fixture()
def database(settings, loop):
    """函数级夹具：每条用例都从空表开始，避免用例之间互相污染。"""
    db = Database(settings)

    async def setup():
        async with db.write_engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=[
                MaterialSourceRecord.__table__, UserProfileRecord.__table__,
                KnowledgeGraphRecord.__table__, DeviceStateRecord.__table__,
            ]))
            await conn.run_sync(Base.metadata.create_all)

    loop.run_until_complete(setup())
    yield db
    loop.run_until_complete(db.dispose())


@pytest.fixture()
def rig(settings, workdir, database, loop):
    retriever = HybridRetriever(
        vector_store=ChromaVectorStore(workdir / "chroma", collection="materials"),
        bm25_store=BM25Store(workdir / "bm25"), embedder=HashingEmbedder(dim=64))
    service = SearchService(retriever=retriever,
                            chunking=ChunkingRule(max_chars=200, overlap_chars=0, min_chars=1))
    wiki = WikiStore(workdir / "wiki", settings=settings)
    ingestor = MaterialIngestor(database=database, wiki_store=wiki, search_service=service)
    return loop, ingestor, wiki, service


def _raw(text=V1, *, url=URL, doc_id="doc-1", title="西湖景区公告"):
    return RawDocument(doc_id=doc_id, url=url, title=title, text=text,
                       publisher="travel.example.com",
                       fetched_at=NOW.isoformat())


async def _rows(database, source_key=None):
    async with database.session_scope(user_id=None) as session:
        stmt = select(MaterialSourceRecord)
        if source_key:
            stmt = stmt.where(MaterialSourceRecord.source_key == source_key)
        return (await session.execute(stmt.order_by(MaterialSourceRecord.version))).scalars().all()


def test_actions_match_plan_incremental_fusion():
    assert ACTIONS == ("create", "refresh", "supersede")


def test_first_collection_creates_row_page_and_index(rig, database):
    loop, ingestor, wiki, service = rig
    report = loop.run_until_complete(ingestor.ingest([_raw()], now=NOW))

    assert report.created == ["doc-1"] and report.failures == []
    assert report.indexed_chunks > 0

    rows = loop.run_until_complete(_rows(database, source_key_for(URL)))
    assert len(rows) == 1 and rows[0].version == 1
    assert rows[0].status == SOURCE_ACTIVE
    assert rows[0].category == "景点", "应被分类为计划书点名的公共域类别"
    assert rows[0].content_hash and rows[0].stale_after is not None

    page = wiki.read_page(page_id_for(source_key_for(URL), 1))
    assert page is not None and page.type == SOURCE
    assert page.user_id is None, "素材溯源库属公共 Workspace，不得带 user_id"
    assert page.extra["url"] == URL and page.extra["category"] == "景点"

    hits = service.search("西湖景区开放时间", user_id="user-A").hits
    assert hits and hits[0].doc_id == page_id_for(source_key_for(URL), 1)


def test_same_content_again_refreshes_instead_of_new_version(rig, database):
    loop, ingestor, wiki, _ = rig
    loop.run_until_complete(ingestor.ingest([_raw()], now=NOW))
    later = NOW + timedelta(hours=6)
    report = loop.run_until_complete(ingestor.ingest([_raw()], now=later))

    assert report.refreshed == ["doc-1"] and report.created == []
    rows = loop.run_until_complete(_rows(database, source_key_for(URL)))
    assert len(rows) == 1, "同内容不应产生新版本"
    assert rows[0].retrieved_at.replace(tzinfo=timezone.utc) == later


def test_changed_content_supersedes_and_archives_the_old_version(rig, database):
    """计划书三、P2：新旧版本增量融合 + 过期来源标记 archived 归档。"""
    loop, ingestor, wiki, service = rig
    loop.run_until_complete(ingestor.ingest([_raw(V1)], now=NOW))
    report = loop.run_until_complete(ingestor.ingest([_raw(V2, doc_id="doc-2")],
                                                     now=NOW + timedelta(days=1)))
    assert report.superseded == ["doc-2"]

    rows = loop.run_until_complete(_rows(database, source_key_for(URL)))
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].status == SOURCE_ARCHIVED and rows[0].archived_at is not None
    assert rows[1].status == SOURCE_ACTIVE

    old_page = wiki.read_page(page_id_for(source_key_for(URL), 1))
    assert old_page.status == SOURCE_ARCHIVED

    # 旧版本必须从索引中移除，避免过期素材继续被检索到
    hits = service.search("春季最佳游览季节六点三十分", user_id="user-A").hits
    assert all(h.doc_id != page_id_for(source_key_for(URL), 1) for h in hits)
    new_hits = service.search("冬季开放时间七点门票预约", user_id="user-A").hits
    assert any(h.doc_id == page_id_for(source_key_for(URL), 2) for h in new_hits)


def test_duplicates_within_one_batch_are_counted_not_dropped_silently(rig, database):
    loop, ingestor, _, _ = rig
    report = loop.run_until_complete(ingestor.ingest(
        [_raw(V1, url="https://a.com/1", doc_id="d1"),
         _raw(V1, url="https://a.com/2", doc_id="d2")], now=NOW))
    assert report.duplicates == 1
    assert len(report.created) == 1


def test_private_material_goes_to_vault_only(rig, database):
    """素材溯源库无用户 ID ⇒ 私有素材不写该表，只进本人 Vault 与私有索引。"""
    loop, ingestor, wiki, service = rig
    report = loop.run_until_complete(ingestor.ingest(
        [_raw(V2, url="https://private.example.com/me", doc_id="mine")],
        user_id="user-A", now=NOW))

    assert report.created == ["mine"]
    rows = loop.run_until_complete(_rows(database, source_key_for("https://private.example.com/me")))
    assert rows == [], "私有素材不得写入公共素材溯源库"

    page_id = page_id_for(source_key_for("https://private.example.com/me"), 1)
    assert wiki.read_page(page_id, user_id="user-A") is not None
    assert wiki.read_page(page_id, user_id="user-B") is None

    assert service.search("冬季开放时间七点", user_id="user-A").hits
    assert service.search("冬季开放时间七点", user_id="user-B").hits == []


def test_uncleanable_material_is_reported_not_crashed(rig):
    loop, ingestor, _, _ = rig
    report = loop.run_until_complete(ingestor.ingest(
        [_raw("<html><script>x</script></html>", url="https://a.com/empty", doc_id="empty")],
        now=NOW))
    assert report.created == [] and report.failures
    assert report.failures[0][0] == "empty"


def test_archive_stale_marks_and_unindexes(rig, database, workdir):
    loop, ingestor, wiki, service = rig
    # 天气类素材时效仅 1 天；给足归档宽限期后应被归档
    loop.run_until_complete(ingestor.ingest(
        [_raw("明日天气晴朗，气温 22 度，适合出行。", url="https://w.com/1", doc_id="weather")],
        now=NOW))

    archived = loop.run_until_complete(
        ingestor.archive_stale(now=NOW + timedelta(days=30), grace_days=0))
    assert archived == [source_key_for("https://w.com/1")]

    rows = loop.run_until_complete(_rows(database, source_key_for("https://w.com/1")))
    assert rows[0].status == SOURCE_ARCHIVED
    assert service.search("天气晴朗气温", user_id="user-A").hits == []


def test_fresh_material_is_not_archived(rig):
    loop, ingestor, _, _ = rig
    loop.run_until_complete(ingestor.ingest([_raw()], now=NOW))
    archived = loop.run_until_complete(
        ingestor.archive_stale(now=NOW + timedelta(days=1), grace_days=0))
    assert archived == [], "景区类素材时效为一年，不应被过早归档"
