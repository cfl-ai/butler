"""P0-1 数据库抽象层。

计划书依据
----------
- 三、P0-1「搭建数据库抽象层：统一连接池、事务、读写分离、加密存储」
- 三、P0-1「全部业务数据表强制增加 user_id 行级隔离字段；
  所有增删改查 SQL 必须携带 user_id 过滤条件」
- 三、P1「全部业务数据表增加 user_id；素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID」
- 七、验收标准 10「行级数据隔离：A 账号完全无法读取 B 账号的画像、对话、账单、私有 Wiki Vault；
  接口、数据库层双重防护，绕过业务层也无法越权读取」

本模块提供的能力
----------------
1. 统一连接池：主库（写）与只读库（读）各自 engine，参数集中来自 Settings。
2. 事务：session_scope 统一提交/回滚边界。
3. 读写分离：session_scope(readonly=True) 走只读库；未配置只读库时复用主库。
4. 行级隔离强制：user_id 未绑定即拒绝访问归属账号的业务实体；
   绑定后自动注入 user_id 过滤条件，业务代码无法忘记写 WHERE。
5. 加密存储：配合 FieldCipher 对隐私字段独立加密（见 app/core/security.py）。

公共 Workspace 例外（计划书明确）
--------------------------------
「素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID」——此类表**不得**继承 UserScoped，
否则会被行级过滤误伤。公共实体请直接使用 Base。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Optional

from sqlalchemy import String, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, ORMExecuteState, Session, mapped_column, with_loader_criteria

from ai_butler.core.config import Settings

_current_user_id: ContextVar[Optional[str]] = ContextVar("current_user_id", default=None)


class RowLevelIsolationError(RuntimeError):
    """缺少 user_id 上下文却试图访问归属账号的数据。计划书：禁止不带用户身份的裸数据访问。"""


def current_user_id() -> Optional[str]:
    return _current_user_id.get()


class Base(DeclarativeBase):
    """公共实体基类（如 Agent-Wiki 公共 Workspace 的素材溯源表）。"""


class UserScoped:
    """行级隔离标记（严格私有）。

    计划书：全部业务数据表强制增加 user_id 行级隔离字段。
    用户画像库、设备状态库、私有 Wiki Vault、账单等**必须**继承本类。
    """

    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class DomainScoped:
    """公共 / 私有双域标记。

    计划书三、P1：「公共域：景点、天气、公开资讯，全账号可读；
    私有域：用户偏好、对话、消费、设备信息、私有 Vault，严格归属 user_id，禁止跨用户访问」

    用于**同时承载两域**的表（数据中台的知识图谱库，见 decisions.U24）：
    - user_id 为 NULL → 公共域，全账号可读；
    - user_id 非空     → 私有域，仅该账号可读。

    过滤语义（两种绑定情况都安全）：
    - 绑定了 user_id：可见「公共域 OR 本人私有域」；
    - 未绑定 user_id：只可见公共域，**绝不泄露任何私有行**。
    """

    user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)


class Database:
    """统一连接池 + 读写分离 + 事务边界。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.write_engine = create_async_engine(
            settings.write_dsn,
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
            pool_timeout=settings.postgres_pool_timeout,
            pool_pre_ping=True,
            echo=settings.postgres_echo,
        )
        self.read_engine = create_async_engine(
            settings.read_dsn,
            pool_size=settings.postgres_pool_size,
            max_overflow=settings.postgres_max_overflow,
            pool_timeout=settings.postgres_pool_timeout,
            pool_pre_ping=True,
            echo=settings.postgres_echo,
        )
        self.write_session_factory = async_sessionmaker(self.write_engine, expire_on_commit=False)
        self.read_session_factory = async_sessionmaker(self.read_engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_scope(self, *, user_id: Optional[str], readonly: bool = False) -> AsyncIterator[AsyncSession]:
        """事务边界 + user_id 上下文绑定。

        user_id 为 None 时只能访问公共实体；一旦触及 UserScoped 实体即抛
        RowLevelIsolationError（计划书：禁止不带 user_id 的裸查询）。
        """
        if user_id is not None and not str(user_id).strip():
            raise RowLevelIsolationError("user_id 不得为空字符串；不允许硬编码或占位")
        token = _current_user_id.set(user_id)
        factory = self.read_session_factory if readonly else self.write_session_factory
        session = factory()
        try:
            yield session
            if not readonly:
                await session.commit()
        except Exception:
            if not readonly:
                await session.rollback()
            raise
        finally:
            await session.close()
            _current_user_id.reset(token)

    async def dispose(self) -> None:
        await self.write_engine.dispose()
        await self.read_engine.dispose()


# --------------------------------------------------------------------------- #
# 行级隔离强制：无论业务代码怎么写，归属账号的实体查询都会被注入 user_id 过滤
# --------------------------------------------------------------------------- #
@event.listens_for(Session, "do_orm_execute")
def _enforce_row_level_isolation(state: ORMExecuteState) -> None:
    if state.is_column_load or state.is_relationship_load:
        return
    mappers = state.all_mappers
    if not mappers:
        return
    strict = any(issubclass(m.class_, UserScoped) for m in mappers)
    dual = any(issubclass(m.class_, DomainScoped) for m in mappers)
    if not strict and not dual:
        return          # 纯公共实体（无 user_id），不做行级过滤

    uid = _current_user_id.get()
    if strict and not uid:
        raise RowLevelIsolationError(
            "【行级隔离】本次语句涉及归属账号的业务实体，但当前会话未绑定 user_id。"
            "计划书三、P0-1：所有增删改查 SQL 必须携带 user_id 过滤条件；"
            "计划书七、10：绕过业务层也无法越权读取。"
        )

    if state.is_select:
        if strict:
            state.statement = state.statement.options(
                with_loader_criteria(
                    UserScoped,
                    lambda cls: cls.user_id == uid,
                    include_aliases=True,
                    track_closure_variables=False,
                )
            )
        if dual:
            # uid 为空时 cls.user_id == uid 生成 IS NULL，语义自动退化为「仅公共域」
            state.statement = state.statement.options(
                with_loader_criteria(
                    DomainScoped,
                    lambda cls: cls.user_id.is_(None) | (cls.user_id == uid),
                    include_aliases=True,
                    track_closure_variables=False,
                )
            )
        return


@event.listens_for(Session, "before_flush")
def _enforce_user_scope_on_write(session: Session, flush_context, instances) -> None:
    """写入前校验：归属账号的实体必须有 user_id，且必须与当前绑定账号一致。"""
    touched = list(session.new) + list(session.dirty) + list(session.deleted)
    strict = [o for o in touched if isinstance(o, UserScoped)]
    dual = [o for o in touched if isinstance(o, DomainScoped)]
    if not strict and not dual:
        return
    uid = _current_user_id.get()
    if strict and not uid:
        raise RowLevelIsolationError(
            "【行级隔离】本次写入涉及归属账号的业务实体，但当前会话未绑定 user_id。"
        )
    for obj in strict:
        owner = getattr(obj, "user_id", None)
        if owner != uid:
            raise RowLevelIsolationError(
                f"【行级隔离】拒绝写入：待写入行的 user_id={owner!r} "
                f"与当前会话绑定账号 {uid!r} 不一致。"
            )
    for obj in dual:
        owner = getattr(obj, "user_id", None)
        # 公共域（None）任何人可写；私有域只能写自己的
        if owner is not None and owner != uid:
            raise RowLevelIsolationError(
                f"【行级隔离】拒绝写入：双域表私有行 user_id={owner!r} "
                f"与当前会话绑定账号 {uid!r} 不一致。"
            )
