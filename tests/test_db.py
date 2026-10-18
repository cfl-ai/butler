"""P0-1 数据库抽象层测试（对本机 PostgreSQL 5434 真实联调）。

计划书依据：三、P0-1「全部业务数据表强制增加 user_id 行级隔离字段；
所有增删改查 SQL 必须携带 user_id 过滤条件」；七、10 行级数据隔离验收。

说明：asyncpg 连接与事件循环绑定，本模块全程使用同一个事件循环。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from ai_butler.core.db import Base, Database, RowLevelIsolationError, UserScoped


class ScopedNote(Base, UserScoped):
    """【测试专用表】非计划书业务表；仅用于验证 P0-1 行级隔离抽象。"""

    __tablename__ = "_p0_selftest_user_scoped"
    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(String(64))


class PublicNote(Base):
    """【测试专用表】对应计划书「素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID」。"""

    __tablename__ = "_p0_selftest_public"
    id: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(String(64))


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
        async with database.write_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with database.session_scope(user_id="user-A") as s:
            s.add(ScopedNote(user_id="user-A", note="A-1"))
        async with database.session_scope(user_id="user-B") as s:
            s.add(ScopedNote(user_id="user-B", note="B-1"))
        async with database.session_scope(user_id=None) as s:
            s.add(PublicNote(note="public-1"))

    loop.run_until_complete(setup())
    yield database
    loop.run_until_complete(database.dispose())


def test_account_sees_only_its_own_rows(db, loop):
    """A 账号完全无法读取 B 账号数据（计划书七、10）。"""

    async def main():
        async with db.session_scope(user_id="user-A", readonly=True) as s:
            rows = (await s.execute(select(ScopedNote))).scalars().all()
        assert [r.note for r in rows] == ["A-1"]
        assert all(r.user_id == "user-A" for r in rows)

    loop.run_until_complete(main())


def test_scoped_query_without_user_id_is_refused(db, loop):
    """禁止不带用户身份的裸查询（计划书三、P0-1）。"""

    async def main():
        with pytest.raises(RowLevelIsolationError) as ei:
            async with db.session_scope(user_id=None) as s:
                await s.execute(select(ScopedNote))
        assert "行级隔离" in str(ei.value)

    loop.run_until_complete(main())


def test_public_entity_readable_without_user_id(db, loop):
    """公共 Workspace 实体不带 user_id 过滤（计划书三、P0-1）。"""

    async def main():
        async with db.session_scope(user_id=None) as s:
            rows = (await s.execute(select(PublicNote))).scalars().all()
        assert [r.note for r in rows] == ["public-1"]

    loop.run_until_complete(main())


def test_cross_account_insert_is_refused(db, loop):
    """拒绝把 A 的行写进 B 的账号下。"""

    async def main():
        with pytest.raises(RowLevelIsolationError):
            async with db.session_scope(user_id="user-A") as s:
                s.add(ScopedNote(user_id="user-B", note="越权写入"))
                await s.flush()

    loop.run_until_complete(main())


def test_blank_user_id_is_refused(db, loop):
    async def main():
        with pytest.raises(RowLevelIsolationError):
            async with db.session_scope(user_id="   "):
                pass

    loop.run_until_complete(main())
