"""P3 会话索引表：会话标识的**服务端签发**与**归属校验**（U73）。

计划书依据
----------
- 三、P3「会话上下文管理：**多轮对话状态保持**、上下文窗口控制、摘要压缩」
- 三、P4「**禁止读取其他 user_id 数据**」
- 三、P0-1「会话管理中间件：会话 Token 生成、校验、自动刷新」

为什么需要单独一张表
------------------
上下文按 `conversations/{user_id}/{conversation_id}.json` 落盘，路径里带 user_id，
所以**跨账号物理不可读** —— 这一层已经挡住了「读到别人的记忆」。

那索引表挡的是什么？挡三件路径之外的错：

1. **客户端自造标识**：不校验就等于允许任何人每发一句话就新建一个落盘文件，
   存储会被无限撑大；且「同一个会话」会碎成互不相连的片段，多轮记忆名存实亡。
2. **同账号内串会话**：A 会话的标识传成 B 会话的标识，严重性低于跨账号，
   但仍然是「读到了不属于本轮的上下文」，答非所问。
3. **误信客户端字段**：端侧此前传的 `session_id` 实为设备号 —— 一台设备上的
   所有对话会落进同一分片并永久累积。索引表要求标识**必须先被签发过**，
   未签发的一律不认，从机制上排除「拿个长得像 ID 的字段冒充」。

设计原则（与 U46/U66 同款）
--------------------------
- **fail-closed**：格式非法、查不到、归属不符、后端异常，一律判「不是你的」，
  绝不放行。放行一次就等于隔离失效一次。
- **不静默复用**：不属于当前账号的标识不「勉强用一下」，而是重新签发并写明原因。
- **播种类与实现分离**：`ConversationIndex` 是端口；Redis 实现用于生产，
  内存实现用于单测与无 Redis 场景，两者语义必须一致（有对照测试）。

编号规则
--------
`secrets.token_hex(16)` → 32 位小写十六进制。固定长度 + 固定字符集，
既是格式校验的判据，也天然满足 `FileStore._check_name` 的文件名白名单
（不含分隔符、冒号、点段）。
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

from ai_butler.core import access
from ai_butler.core.naming import redis_key

#: 会话标识的随机字节数（16 字节 → 32 位十六进制，碰撞概率可忽略）
CONVERSATION_ID_BYTES = 16
#: 会话上下文保留时长（计划书未给数值 → 实现参数）
DEFAULT_CONVERSATION_TTL_SECONDS = 7 * 24 * 3600

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ConversationError(ValueError):
    """会话标识不合法、无主或索引不可用。"""


def new_conversation_id() -> str:
    """签发一个新的会话标识。**只有服务端能签发**。"""
    return secrets.token_hex(CONVERSATION_ID_BYTES)


def is_wellformed(conversation_id: object) -> bool:
    """格式判据。**只判形状，不判归属** —— 归属必须查索引表。"""
    return isinstance(conversation_id, str) and bool(_ID_PATTERN.match(conversation_id))


def conversation_key(conversation_id: str) -> str:
    return redis_key("conversation", conversation_id)


def user_conversations_key(user_id: str) -> str:
    return redis_key("user_conversations", user_id)


class ConversationIndex(Protocol):
    """会话索引表端口。**四个方法都是 async**，实现可以是 Redis 或内存。"""

    async def issue(self, user_id: str) -> str:
        ...

    async def owns(self, user_id: str, conversation_id: str) -> bool:
        ...

    async def list_for(self, user_id: str) -> tuple[str, ...]:
        ...

    async def revoke(self, user_id: str, conversation_id: str) -> bool:
        ...


def _require_owner(user_id: str) -> str:
    """会话必须**有主**。访客不建会话：原话属 PII，且 `guest:` 前缀不是合法目录名。"""
    value = str(user_id or "").strip()
    if not value:
        raise ConversationError("会话标识必须有归属账号，不允许无主会话")
    if access.AccessTier.from_user_id(value) is access.AccessTier.GUEST:
        raise ConversationError("访客不建立会话索引：对话历史不落盘")
    return value


class InMemoryConversationIndex:
    """内存实现。语义与 Redis 实现一致，供单测与无 Redis 部署使用。"""

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}
        self._by_user: dict[str, set[str]] = {}

    async def issue(self, user_id: str) -> str:
        owner = _require_owner(user_id)
        conversation_id = new_conversation_id()
        self._owner[conversation_id] = owner
        self._by_user.setdefault(owner, set()).add(conversation_id)
        return conversation_id

    async def owns(self, user_id: str, conversation_id: str) -> bool:
        if not is_wellformed(conversation_id) or not str(user_id or "").strip():
            return False
        return self._owner.get(conversation_id) == str(user_id).strip()

    async def list_for(self, user_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._by_user.get(str(user_id or "").strip(), set())))

    async def revoke(self, user_id: str, conversation_id: str) -> bool:
        owner = str(user_id or "").strip()
        if not await self.owns(owner, conversation_id):
            return False
        self._owner.pop(conversation_id, None)
        self._by_user.get(owner, set()).discard(conversation_id)
        return True


class RedisConversationIndex:
    """Redis 实现。索引项带 TTL，与上下文文件的保留期对齐。"""

    def __init__(self, redis, *, ttl_seconds: int = DEFAULT_CONVERSATION_TTL_SECONDS) -> None:
        if int(ttl_seconds) <= 0:
            raise ConversationError("会话索引保留时长必须为正数")
        self._r = redis
        self.ttl = int(ttl_seconds)

    async def issue(self, user_id: str) -> str:
        owner = _require_owner(user_id)
        conversation_id = new_conversation_id()
        key = conversation_key(conversation_id)
        await self._r.hset(key, mapping={
            "user_id": owner,
            "created_at": int(time.time()),
        })
        await self._r.expire(key, self.ttl)
        ukey = user_conversations_key(owner)
        await self._r.sadd(ukey, conversation_id)
        await self._r.expire(ukey, self.ttl)
        return conversation_id

    async def owns(self, user_id: str, conversation_id: str) -> bool:
        """**fail-closed**：任何一步不确定都判「不是你的」。"""
        owner = str(user_id or "").strip()
        if not owner or not is_wellformed(conversation_id):
            return False
        try:
            recorded = await self._r.hget(conversation_key(conversation_id), "user_id")
        except Exception:                       # noqa: BLE001 —— 后端异常不放行
            return False
        if recorded is None:
            return False
        if isinstance(recorded, bytes):
            recorded = recorded.decode("utf-8", "replace")
        return str(recorded) == owner

    async def list_for(self, user_id: str) -> tuple[str, ...]:
        owner = str(user_id or "").strip()
        if not owner:
            return ()
        members = await self._r.smembers(user_conversations_key(owner))
        return tuple(sorted(str(m) for m in members))

    async def revoke(self, user_id: str, conversation_id: str) -> bool:
        owner = str(user_id or "").strip()
        if not await self.owns(owner, conversation_id):
            return False
        await self._r.delete(conversation_key(conversation_id))
        await self._r.srem(user_conversations_key(owner), conversation_id)
        return True


@dataclass(frozen=True)
class Resolution:
    """一次「这笔请求该用哪个会话」的裁定结果。"""

    conversation_id: str
    issued: bool
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"conversation_id": self.conversation_id,
                "issued": self.issued, "note": self.note}


async def resolve(index: ConversationIndex, *, user_id: str,
                  requested: str | None) -> Resolution:
    """把客户端传来的标识裁成「可以用哪个」。

    四种情况，三种都要重新签发，且**原因必须带出去** —— 静默换一个会话，
    用户只会觉得「它怎么又忘了」，而排障的人什么都看不到。
    """
    owner = _require_owner(user_id)
    candidate = str(requested or "").strip()

    if not candidate:
        return Resolution(await index.issue(owner), True, "首次对话，已签发会话标识")
    if not is_wellformed(candidate):
        return Resolution(await index.issue(owner), True, "会话标识格式非法，已重新签发")
    if not await index.owns(owner, candidate):
        # 归属不符：可能是别人的标识，也可能是伪造的。**两者同等对待**，
        # 不区分「不存在」与「不是你的」，避免成为探测他人会话是否存在的口子。
        return Resolution(await index.issue(owner), True,
                          "会话标识不属于当前账号，已重新签发，不沿用原会话")
    return Resolution(candidate, False, "")
