"""P0-1 会话登记表（Redis）。

计划书依据：三、P0-1「会话管理中间件：会话 Token 生成、校验、自动刷新；
设置会话有效期，长时间无交互会话自动失效；失效后客户端提示重新登录；
支持单账号多端登录、强制踢下线」

设计要点
--------
- 绝对有效期：Redis 键 TTL = SESSION_TTL_SECONDS，到点自动消失。
- 空闲失效：每次校验比对 last_active_at，超过 SESSION_IDLE_TIMEOUT_SECONDS 即吊销。
  两个阈值均属【文档未定义项 #12】，由配置强制提供，本模块不设默认值。
- 单账号多端：以 (user_id, device_id) 维度登记，同一账号可并存多条会话。
- 强制踢下线：revoke_all_for_user 一次性吊销该账号全部设备的会话。
"""

from __future__ import annotations

import time
from typing import Any

from ai_butler.core.naming import redis_key


def session_key(jti: str) -> str:
    return redis_key("session", jti)


def user_sessions_key(user_id: str) -> str:
    return redis_key("user_sessions", user_id)


class SessionStore:
    def __init__(self, redis, *, ttl_seconds: int, idle_timeout_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("会话有效期必须为正数【文档未定义项 #12，需人工补充】")
        if idle_timeout_seconds <= 0:
            raise ValueError("会话空闲阈值必须为正数【文档未定义项 #12，需人工补充】")
        self._r = redis
        self.ttl = int(ttl_seconds)
        self.idle_timeout = int(idle_timeout_seconds)

    # ---------------- 登记 / 吊销 ----------------
    async def register(self, *, jti: str, user_id: str, device_id: str, now: int | None = None) -> None:
        ts = int(now if now is not None else time.time())
        key = session_key(jti)
        await self._r.hset(key, mapping={
            "user_id": user_id, "device_id": device_id,
            "created_at": ts, "last_active_at": ts,
        })
        await self._r.expire(key, self.ttl)
        ukey = user_sessions_key(user_id)
        await self._r.sadd(ukey, jti)
        await self._r.expire(ukey, self.ttl)

    async def revoke(self, jti: str) -> None:
        key = session_key(jti)
        rec = await self._r.hgetall(key)
        if rec:
            await self._r.srem(user_sessions_key(rec["user_id"]), jti)
        await self._r.delete(key)

    async def revoke_all_for_user(self, user_id: str) -> int:
        """强制踢下线：吊销该账号全部设备的会话，返回吊销条数。"""
        ukey = user_sessions_key(user_id)
        jtis = list(await self._r.smembers(ukey))
        if jtis:
            await self._r.delete(*[session_key(j) for j in jtis])
        await self._r.delete(ukey)
        return len(jtis)

    # ---------------- 校验 ----------------
    async def validate(self, jti: str, *, now: int | None = None) -> dict[str, Any] | None:
        """校验并续期。返回 None 表示会话已失效，调用方必须直接拒绝。"""
        ts = int(now if now is not None else time.time())
        key = session_key(jti)
        rec = await self._r.hgetall(key)
        if not rec:
            return None
        last_active = int(rec.get("last_active_at", 0))
        if ts - last_active > self.idle_timeout:
            await self.revoke(jti)          # 长时间无交互 → 自动失效
            return None
        await self._r.hset(key, "last_active_at", ts)
        await self._r.expire(key, self.ttl)
        return {"jti": jti, "user_id": rec["user_id"], "device_id": rec["device_id"],
                "created_at": int(rec["created_at"]), "last_active_at": ts}

    async def list_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """单账号多端登录：列出该账号当前全部在线设备。"""
        out = []
        for jti in await self._r.smembers(user_sessions_key(user_id)):
            rec = await self._r.hgetall(session_key(jti))
            if rec:
                out.append({"jti": jti, **{k: v for k, v in rec.items()}})
        return out
