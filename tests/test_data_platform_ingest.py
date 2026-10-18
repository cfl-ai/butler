"""P1 多源增量采集测试。

计划书依据：三、P1「开发多源增量数据采集：对话、行为、设备、消费数据；写入强制携带上下文
user_id，增量事实同步生成 Wiki 声明证据」+ 触发时机「同步更新对应私有 Vault 页面」
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from ai_butler.agent_wiki.ontology import CLAIM, ENTITY, EVIDENCE
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.db import Base, Database
from ai_butler.core.storage import FileStore
from ai_butler.data_platform.audit import ChangeAudit
from ai_butler.data_platform.governance import ReviewQueue
from ai_butler.data_platform.ingest import (
    BEHAVIOR,
    CONSUMPTION,
    CONVERSATION,
    DEVICE,
    SOURCES,
    DataIngestor,
    FactEvidence,
    IncrementFact,
    IngestError,
)
from ai_butler.data_platform.models import (
    DeviceStateRecord,
    KnowledgeGraphRecord,
    MaterialSourceRecord,
    UserProfileRecord,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


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


@pytest.fixture(scope="module")
def database(settings, loop):
    db = Database(settings)

    async def setup():
        async with db.write_engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=[
                UserProfileRecord.__table__, KnowledgeGraphRecord.__table__,
                MaterialSourceRecord.__table__, DeviceStateRecord.__table__,
            ]))
            await conn.run_sync(Base.metadata.create_all)

    loop.run_until_complete(setup())
    yield db
    loop.run_until_complete(db.dispose())


@pytest.fixture()
def rig(settings, workdir, database, loop):
    store = FileStore(workdir / "storage", settings=settings)
    wiki = WikiStore(workdir / "wiki", settings=settings)
    audit = ChangeAudit(store)
    review = ReviewQueue(store)
    ingestor = DataIngestor(database=database, wiki_store=wiki, settings=settings,
                            audit=audit, review_queue=review)
    return loop, ingestor, wiki, audit, review


def _fact(attribute, value, confidence, *, source=CONVERSATION, evidences=(), conflict="none"):
    return IncrementFact(source=source, attribute=attribute, value=value,
                         base_confidence=confidence, evidences=tuple(evidences),
                         category="travel", conflict_status=conflict)


def test_four_data_sources_match_plan():
    assert SOURCES == ("conversation", "behavior", "device", "consumption")
    assert {CONVERSATION, BEHAVIOR, DEVICE, CONSUMPTION} == set(SOURCES)


def test_ingest_requires_user_context(rig):
    loop, ingestor, *_ = rig
    with pytest.raises(IngestError) as ei:
        loop.run_until_complete(ingestor.ingest(user_id="", facts=[_fact("a", "b", 0.9)]))
    assert "用户身份上下文" in str(ei.value)


def test_invalid_source_is_refused():
    with pytest.raises(IngestError):
        IncrementFact(source="telepathy", attribute="a", value="b", base_confidence=0.9)


def test_high_confidence_fact_is_persisted_and_synced_to_vault(rig):
    loop, ingestor, wiki, audit, _ = rig
    ev = FactEvidence("ev-1", weight=3.0, reliability=0.95, source_id="src-1",
                      source_text="用户明确表示偏好高铁", text_unit_id="tu-1")
    result = loop.run_until_complete(ingestor.ingest(
        user_id="user-A", facts=[_fact("偏好出行方式", "高铁", 0.5, evidences=[ev])], actor="p1"))

    assert result.persisted == ["偏好出行方式"]
    assert result.total == 1

    # 证据抬升了置信度 → 触发高分入库
    assert result.decisions[0].action == "persist"

    # Wiki：证据页 + 实体页 + 声明页都在该账号私有 Vault
    assert result.wiki_evidence == ["ev-1"]
    assert len(result.wiki_claims) == 1
    claim = wiki.read_page(result.wiki_claims[0], user_id="user-A")
    assert claim.type == CLAIM and claim.extra["evidence_ids"] == ["ev-1"]
    assert claim.extra["claim_type"] == "preference"

    entity = [p for p in wiki.list_pages(user_id="user-A", page_type=ENTITY)]
    assert any(p.extra.get("entity_type") == "用户画像" for p in entity)
    assert wiki.list_pages(user_id="user-B", page_type=CLAIM) == [], "不得写进他人 Vault"

    # 审计留痕
    assert audit.history(user_id="user-A", entity="data_user_profile")


def test_low_confidence_fact_is_logged_not_persisted(rig):
    loop, ingestor, wiki, _, _ = rig
    result = loop.run_until_complete(ingestor.ingest(
        user_id="user-B", facts=[_fact("偏好口味", "未知", 0.2)]))

    assert result.logged_only == ["偏好口味"] and result.persisted == []
    assert wiki.list_pages(user_id="user-B", page_type=CLAIM) == []

    async def read():
        async with ingestor._db.session_scope(user_id="user-B") as s:
            return (await s.execute(select(UserProfileRecord))).scalars().all()

    assert [r.attribute for r in loop.run_until_complete(read())] == []


def test_conflict_fact_goes_to_manual_review(rig):
    loop, ingestor, wiki, _, review = rig
    result = loop.run_until_complete(ingestor.ingest(
        user_id="user-A", facts=[_fact("偏好舱位", "商务", 0.99, conflict="pending")]))

    assert result.reviewing == ["偏好舱位"] and result.persisted == []
    assert result.wiki_claims == [], "进复核队列的事实不得同步进知识库"

    items = review.list_items(user_id="user-A")
    assert len(items) == 1 and items[0].kind == "conflict"
    assert items[0].ref_id == "user-A:偏好舱位"
    assert wiki.list_pages(user_id="user-A", page_type=CLAIM) == []


def test_re_ingest_updates_in_place_and_bumps_version(rig):
    loop, ingestor, _, audit, _ = rig
    loop.run_until_complete(ingestor.ingest(
        user_id="user-A", facts=[_fact("偏好出发时间", "早上八点", 0.95)]))
    loop.run_until_complete(ingestor.ingest(
        user_id="user-A", facts=[_fact("偏好出发时间", "早上七点", 0.96)]))

    async def read():
        async with ingestor._db.session_scope(user_id="user-A") as s:
            return (await s.execute(
                select(UserProfileRecord).where(
                    UserProfileRecord.attribute == "偏好出发时间"))).scalars().first()

    record = loop.run_until_complete(read())
    assert record.value == "早上七点"
    assert record.version == 2

    history = audit.history(user_id="user-A", entity="data_user_profile",
                            entity_id=str(record.id))
    assert [e.action for e in history] == ["create", "update"]


def test_device_and_consumption_sources_are_accepted(rig):
    loop, ingestor, _, _, _ = rig
    result = loop.run_until_complete(ingestor.ingest(
        user_id="user-A",
        facts=[_fact("设备电量习惯", "夜间充电", 0.9, source=DEVICE),
               _fact("消费档位", "中预算", 0.9, source=CONSUMPTION),
               _fact("点击行为", "常看攻略", 0.9, source=BEHAVIOR)]))
    assert set(result.persisted) == {"点击行为", "消费档位", "设备电量习惯"}
