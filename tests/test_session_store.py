"""P0-1 会话登记表测试（fakeredis）。"""

from __future__ import annotations

import asyncio
import time

import pytest

from ai_butler.core.session_store import SessionStore

TTL = 900
IDLE = 300


def _store(r):
    return SessionStore(r, ttl_seconds=TTL, idle_timeout_seconds=IDLE)


def test_register_and_validate(redis_client):
    async def main():
        s = _store(redis_client)
        await s.register(jti="j1", user_id="u-1", device_id="dev-a", now=1000)
        rec = await s.validate("j1", now=1100)
        assert rec and rec["user_id"] == "u-1" and rec["device_id"] == "dev-a"
        assert rec["last_active_at"] == 1100
        return await s.validate("不存在", now=1100)

    assert asyncio.run(main()) is None


def test_idle_timeout_invalidates_session(redis_client):
    """计划书：长时间无交互会话自动失效。"""

    async def main():
        s = _store(redis_client)
        await s.register(jti="j1", user_id="u-1", device_id="dev-a", now=1000)
        assert await s.validate("j1", now=1000 + IDLE + 1) is None
        return await s.validate("j1", now=1000 + IDLE + 2)

    assert asyncio.run(main()) is None


def test_multi_device_and_force_logout(redis_client):
    """计划书：支持单账号多端登录、强制踢下线。"""

    async def main():
        s = _store(redis_client)
        await s.register(jti="j1", user_id="u-1", device_id="phone", now=1000)
        await s.register(jti="j2", user_id="u-1", device_id="pad", now=1000)
        await s.register(jti="j3", user_id="u-2", device_id="phone", now=1000)
        assert len(await s.list_sessions("u-1")) == 2
        killed = await s.revoke_all_for_user("u-1")
        assert killed == 2
        assert await s.validate("j1", now=1001) is None
        assert await s.validate("j2", now=1001) is None
        other = await s.validate("j3", now=1001)
        assert other and other["user_id"] == "u-2", "不得影响其他账号"

    asyncio.run(main())


def test_single_revoke(redis_client):
    async def main():
        s = _store(redis_client)
        await s.register(jti="j1", user_id="u-1", device_id="dev-a", now=1000)
        await s.revoke("j1")
        assert await s.validate("j1", now=1001) is None
        assert await s.list_sessions("u-1") == []

    asyncio.run(main())


def test_configured_idle_timeout_actually_applies(settings, redis_client):
    """用 .env 中实际配置的空闲阈值验证：超过该阈值无交互即失效。

    断言的是「配置真的驱动了行为」，不断言具体数值——数值可按实际情况调整。
    """

    async def main():
        store = SessionStore(redis_client,
                             ttl_seconds=settings.session_ttl_seconds,
                             idle_timeout_seconds=settings.session_idle_timeout_seconds)
        now = int(time.time())
        await store.register(jti="j", user_id="u", device_id="d",
                             now=now - settings.session_idle_timeout_seconds - 1)
        expired = await store.validate("j", now=now)
        await store.register(jti="k", user_id="u", device_id="d",
                             now=now - settings.session_idle_timeout_seconds + 5)
        alive = await store.validate("k", now=now)
        return expired, alive

    expired, alive = asyncio.run(main())
    assert expired is None, "超过配置的空闲阈值必须失效"
    assert alive is not None, "未超过配置的空闲阈值必须有效"


def test_undefined_thresholds_rejected(redis_client):
    """【文档未定义项 #12】阈值必须为正数，模块自身不提供默认值。"""
    with pytest.raises(ValueError):
        SessionStore(redis_client, ttl_seconds=0, idle_timeout_seconds=IDLE)
    with pytest.raises(ValueError):
        SessionStore(redis_client, ttl_seconds=TTL, idle_timeout_seconds=0)
