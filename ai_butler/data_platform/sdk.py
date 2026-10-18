"""P1 全局统一数据 SDK。

计划书依据
----------
- 三、P1「封装全局统一数据 SDK（**所有 Agent 只读，禁止直写库**）」
- 三、P1 表注「SDK 约束：入参强制传入 user_id；查询自动拼接行级过滤；
  **公共知识库不受 user_id 限制**；SDK 同时封装 Agent-Wiki 读写 API」
- 三、P4「Wiki 约束：子 Agent 产出事实优先写入对应用户私有 Vault」

两条看似冲突的要求如何对齐
------------------------
「所有 Agent 只读，禁止直写库」针对的是**数据库**；
「SDK 同时封装 Agent-Wiki 读写 API」针对的是**知识库**——P4 明确要求子 Agent
把产出事实写进私有 Vault。因此本 SDK 的口径是：

| 通道 | Agent 权限 | 实现方式 |
| 四大核心数据表 | **只读** | 仅暴露 read_* 方法，全部走 readonly 会话 |
| Agent-Wiki | **可读可写** | wiki_* 方法，隔离由 WikiStore 强制 |

**所有**数据库访问都必须经由本 SDK；业务代码不得自行打开 session_scope。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select

from ai_butler.agent_wiki.ontology import WikiPage
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.db import Database, RowLevelIsolationError
from ai_butler.data_platform.models import (
    DeviceStateRecord,
    KnowledgeGraphRecord,
    MaterialSourceRecord,
    SOURCE_ACTIVE,
    UserProfileRecord,
)


class DataAccessError(RuntimeError):
    """违反 SDK 契约（缺少 user_id、越权访问、或试图直写库）。"""


def _require_user(user_id: str | None, *, why: str) -> str:
    if not user_id or not str(user_id).strip():
        raise DataAccessError(
            f"{why} 必须携带 user_id。计划书三、P1 表注：SDK 入参强制传入 user_id。"
        )
    return str(user_id)


def _row(obj: Any, fields: Iterable[str]) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in fields}


PROFILE_FIELDS = ("id", "user_id", "attribute", "value", "category", "confidence",
                  "weight", "conflict_status", "source_id", "version", "vault_entity_id")
KNOWLEDGE_FIELDS = ("id", "user_id", "subject", "predicate", "object", "statement",
                    "claim_page_id", "confidence", "weight", "conflict_status", "source_id",
                    "version")
DEVICE_FIELDS = ("id", "user_id", "device_id", "metric", "value", "reported_at",
                 "confidence", "weight", "conflict_status", "source_id", "version")
SOURCE_FIELDS = ("id", "source_key", "url", "title", "publisher", "retrieved_at", "status",
                 "archived_at")


class DataSDK:
    def __init__(self, *, database: Database, wiki_store: WikiStore) -> None:
        self._db = database
        self._wiki = wiki_store

    # ==================== 只读通道：四大核心数据表 ==================== #
    async def read_profile(self, *, user_id: str, category: str | None = None,
                           limit: int = 200) -> list[dict[str, Any]]:
        """用户画像库（私有域）。查询自动拼接 user_id 行级过滤。"""
        uid = _require_user(user_id, why="读取用户画像")
        async with self._db.session_scope(user_id=uid, readonly=True) as s:
            stmt = select(UserProfileRecord)
            if category:
                stmt = stmt.where(UserProfileRecord.category == category)
            rows = (await s.execute(stmt.limit(limit))).scalars().all()
        return [_row(r, PROFILE_FIELDS) for r in rows]

    async def read_device_state(self, *, user_id: str, metric: str | None = None,
                                limit: int = 200) -> list[dict[str, Any]]:
        """设备状态库（私有域）。"""
        uid = _require_user(user_id, why="读取设备状态")
        async with self._db.session_scope(user_id=uid, readonly=True) as s:
            stmt = select(DeviceStateRecord).order_by(DeviceStateRecord.reported_at.desc())
            if metric:
                stmt = stmt.where(DeviceStateRecord.metric == metric)
            rows = (await s.execute(stmt.limit(limit))).scalars().all()
        return [_row(r, DEVICE_FIELDS) for r in rows]

    async def read_knowledge(self, *, user_id: str | None = None, subject: str | None = None,
                             limit: int = 200) -> list[dict[str, Any]]:
        """知识图谱库（双域）。

        - 传 user_id：返回「公共域 + 本人私有域」；
        - 不传 user_id：**只返回公共域**（计划书 P1「公共域…全账号可读」）。
        """
        async with self._db.session_scope(user_id=user_id, readonly=True) as s:
            stmt = select(KnowledgeGraphRecord)
            if subject:
                stmt = stmt.where(KnowledgeGraphRecord.subject == subject)
            rows = (await s.execute(stmt.limit(limit))).scalars().all()
        return [_row(r, KNOWLEDGE_FIELDS) for r in rows]

    async def read_material_sources(self, *, status: str | None = SOURCE_ACTIVE,
                                    limit: int = 200) -> list[dict[str, Any]]:
        """素材溯源库。

        计划书三、P1 表注：「素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID」
        「公共知识库不受 user_id 限制」——**本方法刻意不接收 user_id**。
        """
        async with self._db.session_scope(user_id=None, readonly=True) as s:
            stmt = select(MaterialSourceRecord)
            if status:
                stmt = stmt.where(MaterialSourceRecord.status == status)
            rows = (await s.execute(stmt.limit(limit))).scalars().all()
        return [_row(r, SOURCE_FIELDS) for r in rows]

    # ==================== 读写通道：Agent-Wiki ==================== #
    def wiki_get(self, page_id: str, *, user_id: str | None = None) -> WikiPage | None:
        """读取 Wiki 页面。传 user_id 读该账号 Vault，不传读公共 Workspace。"""
        return self._wiki.read_page(page_id, user_id=user_id)

    def wiki_retrieve(self, *, user_id: str, page_type: str | None = None) -> list[WikiPage]:
        """检索语义：公共 Workspace + 本人 Vault，绝不加载他人页面（计划书三、P2）。"""
        uid = _require_user(user_id, why="Wiki 检索")
        return self._wiki.list_pages_for_retrieval(uid, page_type=page_type)

    def wiki_write(self, page: WikiPage, *, user_id: str | None = None):
        """写入 Wiki 页面。

        计划书三、P4「子 Agent 产出事实优先写入对应用户私有 Vault」——
        因此这里**允许写**；隔离规则由 WikiStore 强制（公共页不得带 user_id、
        私有页 user_id 必须与目标账号一致）。
        """
        return self._wiki.write_page(page, user_id=user_id)

    def wiki_list(self, *, user_id: str | None = None, page_type: str | None = None
                  ) -> list[WikiPage]:
        return self._wiki.list_pages(user_id=user_id, page_type=page_type)


#: SDK 的公开方法分类——供自检与测试核对，防止有人偷偷加一个写库入口
DB_READ_METHODS: tuple[str, ...] = (
    "read_profile", "read_device_state", "read_knowledge", "read_material_sources",
)
WIKI_METHODS: tuple[str, ...] = ("wiki_get", "wiki_retrieve", "wiki_write", "wiki_list")
ALLOWED_METHODS: tuple[str, ...] = DB_READ_METHODS + WIKI_METHODS
