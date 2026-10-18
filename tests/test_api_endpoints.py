"""P7 云端接口：登录与设备上报。

计划书依据
----------
- 三、P7 步骤3「账号密码登录、会话凭证本地加密存储；明文密码不在本地持久化」
- 三、P7 步骤7「上报携带会话身份」
- 七、11「未登录状态下所有业务接口拒绝执行」

为什么是这两个接口
------------------
移动端 ApiClient 写死了三个路径：/api/session/login、/api/chat/stream、
/api/device/report。而后端此前**只有 /health 一个路由** —— 应用在真机上
能启动、能渲染登录页，但永远登不进去。本轮补上登录与上报，
/chat/stream 仍缺（已在台账 U42 登记）。
"""

from __future__ import annotations

import asyncio

import httpx

from ai_butler.app import create_app
from ai_butler.core.users import UserRegistry
from ai_butler.middleware.session_auth import login as issue_session


def _client(settings, redis_client):
    app = create_app(settings, redis_client)
    return app, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t")


def _seed(app, *, user_id="u-1", password="correct-horse"):
    """向应用用同一份账号表登记一个账号。"""
    registry = app.state.users
    try:
        registry.register(user_id=user_id, password=password)
    except Exception:
        pass
    return registry


# ============================== 登录 ==============================
def test_login_is_public_and_returns_a_session(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        async with client:
            return await client.post("/api/session/login", json={
                "account": "u-1", "password": "correct-horse", "device_id": "d-1"})

    r = asyncio.run(main())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "u-1"
    assert body["token"], "必须返回会话 token"
    assert body["expires_in_seconds"] > 0
    assert body["idle_timeout_seconds"] > 0


def test_login_with_wrong_password_is_rejected(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        async with client:
            return await client.post("/api/session/login", json={
                "account": "u-1", "password": "wrong", "device_id": "d-1"})

    r = asyncio.run(main())
    assert r.status_code == 401
    assert "token" not in r.json()


def test_unknown_account_and_wrong_password_look_the_same(settings, redis_client):
    """不泄露账号是否存在 —— 两者返回同一状态码与同一个 message。"""
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        async with client:
            wrong = await client.post("/api/session/login", json={
                "account": "u-1", "password": "wrong", "device_id": "d"})
            ghost = await client.post("/api/session/login", json={
                "account": "ghost", "password": "wrong", "device_id": "d"})
        return wrong, ghost

    wrong, ghost = asyncio.run(main())
    assert wrong.status_code == ghost.status_code == 401
    assert wrong.json()["message"] == ghost.json()["message"]


def test_login_requires_every_field(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        async with client:
            return await client.post("/api/session/login", json={"account": "u-1"})

    r = asyncio.run(main())
    assert r.status_code == 400


def test_login_response_never_echoes_the_password(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        async with client:
            return await client.post("/api/session/login", json={
                "account": "u-1", "password": "correct-horse", "device_id": "d-1"})

    r = asyncio.run(main())
    assert "correct-horse" not in r.text


# ============================== 设备上报 ==============================
def test_device_report_requires_a_session(settings, redis_client):
    """计划书七、11：未登录拒绝一切业务接口。"""
    async def main():
        _app, client = _client(settings, redis_client)
        async with client:
            return await client.post("/api/device/report", json={"user_id": "u-1"})

    r = asyncio.run(main())
    assert r.status_code == 401


def test_device_report_accepts_the_authenticated_user(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        store = app.state.session_store
        st = await issue_session(store=store, settings=settings,
                                 user_id="u-1", device_id="d-1")
        async with client:
            return await client.post(
                "/api/device/report",
                json={"user_id": "u-1", "session_id": "d-1", "battery_level": 88},
                headers={"Authorization": f"Bearer {st.token}"})

    r = asyncio.run(main())
    assert r.status_code == 200
    assert r.json()["user_id"] == "u-1"


def test_device_report_refuses_a_spoofed_user_id(settings, redis_client):
    """用 A 的会话替 B 上报 —— **以会话身份为准**，body 里冒充别人直接 403。"""
    async def main():
        app, client = _client(settings, redis_client)
        store = app.state.session_store
        st = await issue_session(store=store, settings=settings,
                                 user_id="u-1", device_id="d-1")
        async with client:
            return await client.post(
                "/api/device/report",
                json={"user_id": "u-2", "latitude": 30.2, "longitude": 120.1},
                headers={"Authorization": f"Bearer {st.token}"})

    r = asyncio.run(main())
    assert r.status_code == 403
    assert r.json()["code"] == "user_mismatch"


def test_business_paths_still_reject_anonymous(settings, redis_client):
    """补了登录接口之后，**别的业务接口仍然拒绝匿名**。"""
    async def main():
        _app, client = _client(settings, redis_client)
        async with client:
            return await client.post("/api/chat/stream", json={"utterance": "hi"})

    r = asyncio.run(main())
    assert r.status_code == 401
