"""P0-1 会话鉴权中间件测试。"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from ai_butler.core.session_store import SessionStore
from ai_butler.middleware.session_auth import (
    REFRESHED_TOKEN_HEADER,
    SESSION_EXPIRED_CODE,
    SessionAuthMiddleware,
    force_logout,
    login,
    logout,
)

TTL = 900
IDLE = 300


async def _echo_app(scope, receive, send):
    uid = (scope.get("state") or {}).get("user_id", "<公共路径未鉴权>")
    body = json.dumps({"user_id": uid, "path": scope["path"]}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _build(settings, redis_client, *, ttl=TTL, idle=IDLE):
    s = settings.model_copy(update={"session_ttl_seconds": ttl, "session_idle_timeout_seconds": idle})
    store = SessionStore(redis_client, ttl_seconds=ttl, idle_timeout_seconds=idle)
    app = SessionAuthMiddleware(_echo_app, settings=s, store=store,
                                public_paths={"/health"}, public_prefixes=("/api/v1/auth/",))
    return s, store, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_missing_and_invalid_token_are_rejected(settings, redis_client):
    async def main():
        s, store, client = _build(settings, redis_client)
        async with client:
            r1 = await client.get("/api/v1/plan")
            r2 = await client.get("/api/v1/plan", headers={"Authorization": "Bearer not-a-real-jwt"})
        return r1, r2

    r1, r2 = asyncio.run(main())
    for r in (r1, r2):
        assert r.status_code == 401
        assert r.json()["code"] == SESSION_EXPIRED_CODE
        assert "重新登录" in r.json()["message"]


def test_valid_token_passes_user_id_downstream(settings, redis_client):
    async def main():
        s, store, client = _build(settings, redis_client)
        st = await login(store=store, settings=s, user_id="u-9", device_id="phone")
        async with client:
            r = await client.get("/api/v1/plan", headers={"Authorization": f"Bearer {st.token}"})
        return r

    r = asyncio.run(main())
    assert r.status_code == 200 and r.json()["user_id"] == "u-9"


def test_public_path_needs_no_token(settings, redis_client):
    async def main():
        s, store, client = _build(settings, redis_client)
        async with client:
            return await client.get("/health")

    assert asyncio.run(main()).status_code == 200


def test_force_logout_kills_all_devices(settings, redis_client):
    """计划书：支持单账号多端登录、强制踢下线。"""

    async def main():
        s, store, client = _build(settings, redis_client)
        a = await login(store=store, settings=s, user_id="u-9", device_id="phone")
        b = await login(store=store, settings=s, user_id="u-9", device_id="pad")
        killed = await force_logout(store=store, user_id="u-9")
        async with client:
            ra = await client.get("/x", headers={"Authorization": f"Bearer {a.token}"})
            rb = await client.get("/x", headers={"Authorization": f"Bearer {b.token}"})
        return killed, ra.status_code, rb.status_code

    killed, sa, sb = asyncio.run(main())
    assert killed == 2 and sa == 401 and sb == 401


def test_logout_revokes_only_its_own_session(settings, redis_client):
    """单端登出：只吊销自己那一个 jti，别的设备不受影响。

    为什么补这条：logout() 与 login()/force_logout() 同属会话生命周期 API，
    但**既没被调用过也没被测过**（死代码审计发现的）。
    登录与强制踢下线都有覆盖，唯独它没有 —— 而它处理的是「凭证主动作废」，
    正是最不该未经验证的那类路径。
    """

    async def main():
        s, store, client = _build(settings, redis_client)
        a = await login(store=store, settings=s, user_id="u-7", device_id="phone")
        b = await login(store=store, settings=s, user_id="u-7", device_id="pad")
        await logout(store=store, jti=a.jti)
        async with client:
            ra = await client.get("/x", headers={"Authorization": f"Bearer {a.token}"})
            rb = await client.get("/x", headers={"Authorization": f"Bearer {b.token}"})
        return ra.status_code, rb.status_code

    sa, sb = asyncio.run(main())
    assert sa == 401, "已登出的会话必须立刻失效，不能等到 TTL"
    assert sb == 200, "登出一端不应牵连同账号的其它设备"


def test_idle_session_is_rejected(settings, redis_client):
    """计划书：长时间无交互会话自动失效，失效后客户端提示重新登录。"""

    async def main():
        s, store, client = _build(settings, redis_client)
        st = await login(store=store, settings=s, user_id="u-9", device_id="phone",
                         now=int(time.time()) - IDLE - 60)
        async with client:
            return await client.get("/x", headers={"Authorization": f"Bearer {st.token}"})

    r = asyncio.run(main())
    assert r.status_code == 401 and r.json()["code"] == SESSION_EXPIRED_CODE


def test_token_is_refreshed_before_expiry(settings, redis_client):
    """计划书：会话 Token 自动刷新。"""
    ttl, idle, ahead = 900, 800, 300

    async def main():
        s, store, client = _build(settings, redis_client, ttl=ttl, idle=idle)
        st = await login(store=store, settings=s, user_id="u-9", device_id="phone",
                         now=int(time.time()) - 700)          # 剩余 200s < 刷新余量 300s
        async with client:
            r = await client.get("/x", headers={"Authorization": f"Bearer {st.token}"})
            fresh = r.headers.get(REFRESHED_TOKEN_HEADER)
            r2 = await client.get("/x", headers={"Authorization": f"Bearer {fresh}"})
            old = await client.get("/x", headers={"Authorization": f"Bearer {st.token}"})
        return r, fresh, r2, old

    r, fresh, r2, old = asyncio.run(main())
    assert r.status_code == 200 and fresh, "应换发新会话凭证"
    assert r2.status_code == 200, "新凭证可用"
    assert old.status_code == 401, "旧凭证应已吊销"
