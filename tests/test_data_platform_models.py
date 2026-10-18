"""P1 数据中台 —— 四大核心数据表与域隔离测试。

计划书依据：三、P1（四大核心数据表；素材溯源库无用户 ID；用户画像/设备状态绑定 user_id；
公共域全账号可读 / 私有域严格归属 user_id）+ decisions.U24（知识图谱库双域）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from ai_butler.core.db import Base, Database, DomainScoped, RowLevelIsolationError, UserScoped
from ai_butler.data_platform.models import (
    CONFLICT_NONE,
    CONFLICT_STATUSES,
    CORE_TABLES,
    DOMAIN_PUBLIC,
    PLAN_DOMAIN_ASSIGNMENT,
    SOURCE_ACTIVE,
    SOURCE_STATUSES,
    DeviceStateRecord,
    KnowledgeGraphRecord,
    MaterialSourceRecord,
    UserProfileRecord,
)


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
def db(settings, loop):
    database = Database(settings)

    async def setup():
        # 数据中台的表结构会随阶段演进，这里只重建这四张表，
        # 避免 create_all 不改已存在表导致的列漂移。
        async with database.write_engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=[
                UserProfileRecord.__table__, KnowledgeGraphRecord.__table__,
                MaterialSourceRecord.__table__, DeviceStateRecord.__table__,
            ]))
            await conn.run_sync(Base.metadata.create_all)

        async def seed(user_id, *, suffix):
            async with database.session_scope(user_id=user_id) as s:
                s.add(UserProfileRecord(user_id=user_id, attribute=f"偏好{suffix}",
                                        value=f"值{suffix}", confidence=0.9))
                s.add(DeviceStateRecord(user_id=user_id, device_id=f"dev-{suffix}",
                                        metric="battery", value="80", confidence=0.99))
                s.add(KnowledgeGraphRecord(user_id=user_id, subject=f"用户{suffix}",
                                          predicate="偏好", object=f"季节{suffix}",
                                          statement=f"{suffix} 偏好", confidence=0.8))
        await seed("user-A", suffix="A")
        await seed("user-B", suffix="B")

        async with database.session_scope(user_id=None) as s:
            # 公共域知识：计划书 P1「公共域：景点、天气、公开资讯，全账号可读」
            s.add(KnowledgeGraphRecord(user_id=None, subject="西湖", predicate="位于",
                                       object="杭州", statement="西湖位于杭州", confidence=0.95))
            s.add(MaterialSourceRecord(source_key="src-1", url="https://example.com/a",
                                       title="公开资讯"))

    loop.run_until_complete(setup())
    yield database
    loop.run_until_complete(database.dispose())


# ------------------------------ 域归属 ------------------------------ #
def test_four_core_tables_match_plan_section_p1():
    """计划书三、P1 逐字点名的四张表。"""
    assert set(CORE_TABLES) == {"用户画像库", "知识图谱库", "素材溯源库", "设备状态库"}


def test_domain_assignment_matches_plan_notes():
    assert PLAN_DOMAIN_ASSIGNMENT == {
        "用户画像库": "private", "知识图谱库": "dual",
        "素材溯源库": "public", "设备状态库": "private",
    }
    assert issubclass(UserProfileRecord, UserScoped)
    assert issubclass(DeviceStateRecord, UserScoped)
    assert issubclass(KnowledgeGraphRecord, DomainScoped)
    # 素材溯源库属于公共 Workspace 无用户 ID —— 不得继承任何一种隔离标记
    assert not issubclass(MaterialSourceRecord, (UserScoped, DomainScoped))
    assert not hasattr(MaterialSourceRecord, "user_id")


def test_field_spec_values():
    assert CONFLICT_STATUSES == ("none", "pending", "resolved")
    assert CONFLICT_NONE == "none"
    assert SOURCE_STATUSES == ("active", "archived")
    assert SOURCE_ACTIVE == "active"
    assert DOMAIN_PUBLIC == "public"


# ------------------------- 私有域：严格行级隔离 ------------------------- #
def test_profile_is_isolated_between_accounts(db, loop):
    async def main():
        async with db.session_scope(user_id="user-A") as s:
            return (await s.execute(select(UserProfileRecord))).scalars().all()

    rows = loop.run_until_complete(main())
    assert [r.attribute for r in rows] == ["偏好A"]
    assert all(r.user_id == "user-A" for r in rows)


def test_device_state_is_isolated_between_accounts(db, loop):
    async def main():
        async with db.session_scope(user_id="user-B") as s:
            return (await s.execute(select(DeviceStateRecord))).scalars().all()

    rows = loop.run_until_complete(main())
    assert [r.device_id for r in rows] == ["dev-B"]


def test_private_tables_refuse_unbound_query(db, loop):
    """计划书七、10：绕过业务层也无法越权读取。"""

    async def main():
        for model in (UserProfileRecord, DeviceStateRecord):
            with pytest.raises(RowLevelIsolationError):
                async with db.session_scope(user_id=None) as s:
                    await s.execute(select(model))

    loop.run_until_complete(main())


# --------------------------- 公共域：无 user_id --------------------------- #
def test_material_source_is_public(db, loop):
    """计划书三、P1：素材溯源库属于公共 Workspace，无用户 ID，全账号可读。"""

    async def main():
        async with db.session_scope(user_id=None) as s:
            return (await s.execute(select(MaterialSourceRecord))).scalars().all()

    rows = loop.run_until_complete(main())
    assert [r.source_key for r in rows] == ["src-1"]


# --------------------------- 双域：知识图谱库 --------------------------- #
def test_dual_domain_bound_account_sees_public_plus_own(db, loop):
    """计划书 P1：公共域全账号可读；私有域严格归属 user_id。"""

    async def main():
        async with db.session_scope(user_id="user-A") as s:
            rows = (await s.execute(select(KnowledgeGraphRecord))).scalars().all()
        return sorted((r.subject, r.user_id) for r in rows)

    got = loop.run_until_complete(main())
    # 绑定了 user-A：可见「公共域（西湖）+ 本人私有域（用户A）」，看不到 user-B 的
    assert got == [("用户A", "user-A"), ("西湖", None)], got


def test_dual_domain_unbound_account_sees_public_only(db, loop):
    """关键安全断言：未绑定账号时**绝不**泄露任何私有行。"""

    async def main():
        async with db.session_scope(user_id=None) as s:
            rows = (await s.execute(select(KnowledgeGraphRecord))).scalars().all()
        return [r.subject for r in rows]

    assert loop.run_until_complete(main()) == ["西湖"]


def test_dual_domain_rejects_writing_another_accounts_private_row(db, loop):
    async def main():
        with pytest.raises(RowLevelIsolationError):
            async with db.session_scope(user_id="user-A") as s:
                s.add(KnowledgeGraphRecord(user_id="user-B", subject="越权", predicate="写",
                                          object="他人私有", statement="x", confidence=0.5))
                await s.flush()

    loop.run_until_complete(main())


def test_dual_domain_rejects_unbound_private_write(db, loop):
    """未绑定账号只能写公共域。"""

    async def main():
        with pytest.raises(RowLevelIsolationError):
            async with db.session_scope(user_id=None) as s:
                s.add(KnowledgeGraphRecord(user_id="user-A", subject="x", predicate="y",
                                          object="z", statement="s", confidence=0.5))
                await s.flush()

        # 公共行（user_id=None）在未绑定账号时写入是合法的
        async with db.session_scope(user_id=None) as s:
            s.add(KnowledgeGraphRecord(user_id=None, subject="黄山", predicate="位于",
                                      object="安徽", statement="黄山位于安徽", confidence=0.9))

    loop.run_until_complete(main())
