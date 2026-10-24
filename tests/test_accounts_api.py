"""账号 / 模型 / 定位的接口级守护（U67~U71）。

这一层的重点不是"字段对不对"，而是**几条不能被绕过的话**：

* 访客（未登录）拿不到资料、模型、定位 —— 一律 403 且带能力码
* 「这个号是否已注册」在注册接口会透露（必要），在找回接口**不会**（防枚举）
* 任何响应里都不出现完整手机号、邮箱、API Key 明文
* 没有地图 key 就说没有（`map_available=False`），不画假地图
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_butler.app import create_app

PHONE = "13800000007"
PASSWORD = "Demo-Passw0rd"
DEVICE = "phone"


@pytest.fixture()
def settings(settings, tmp_path):
    """每个用例一个独立存储根。

    否则前一个用例注册的手机号会让后一个用例拿到 409 —— 这种"测试互相污染"
    排查起来极费时间，而且容易被误当成业务 bug。
    """
    return settings.model_copy(update={
        "storage_root": str(tmp_path),
        "account_store_root": str(tmp_path),
    })


class _FakeGateway:
    async def stream(self, *, user_id, messages, model=None, max_tokens=None,
                     provider=None, api_key=None, base_url=None):
        yield "好"


def _client(settings, redis_client):
    app = create_app(settings, redis_client,
                     chat_gateway=_FakeGateway(),
                     chat_search=lambda q, u: [])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _run(settings, redis_client, coro_factory):
    async def main():
        async with _client(settings, redis_client) as client:
            return await coro_factory(client)

    return asyncio.run(main())


def _register(client, *, kind="phone", identifier=PHONE, **extra):
    payload = {"kind": kind, "identifier": identifier,
               "password": PASSWORD, "device_id": DEVICE, **extra}
    return payload


# --------------------------------------------------------------------------- #
# 注册 / 登录
# --------------------------------------------------------------------------- #
def test_register_returns_session_and_masked_profile(settings, redis_client):
    async def flow(client):
        response = await client.post("/api/session/register", json=_register(client))
        return response

    response = _run(settings, redis_client, flow)
    assert response.status_code == 201
    body = response.json()
    assert body["token"]
    assert body["user_id"].startswith("u_")
    assert body["profile"]["ident_masked"] == "138****0007"
    # 完整手机号不得出现在响应里
    assert PHONE not in json.dumps(body, ensure_ascii=False)


def test_register_with_email_works_too(settings, redis_client):
    async def flow(client):
        return await client.post(
            "/api/session/register",
            json=_register(client, kind="email", identifier="bob@example.com"))

    response = _run(settings, redis_client, flow)
    assert response.status_code == 201
    assert response.json()["profile"]["ident_kind"] == "email"


def test_duplicate_registration_is_conflict(settings, redis_client):
    async def flow(client):
        await client.post("/api/session/register", json=_register(client))
        # 换个写法也必须撞上同一个账号
        return await client.post(
            "/api/session/register",
            json=_register(client, identifier="+86 138-0000-0007"))

    response = _run(settings, redis_client, flow)
    assert response.status_code == 409
    assert response.json()["code"] == "identifier_taken"


def test_login_with_kind_issues_member_session(settings, redis_client):
    async def flow(client):
        await client.post("/api/session/register", json=_register(client))
        return await client.post("/api/session/login", json={
            "kind": "phone", "identifier": PHONE,
            "password": PASSWORD, "device_id": DEVICE})

    response = _run(settings, redis_client, flow)
    assert response.status_code == 200
    assert response.json()["profile"]["ident_masked"] == "138****0007"


def test_wrong_password_is_401_without_revealing_account_existence(
        settings, redis_client):
    async def flow(client):
        await client.post("/api/session/register", json=_register(client))
        real = await client.post("/api/session/login", json={
            "kind": "phone", "identifier": PHONE, "password": "Nope-12345678",
            "device_id": DEVICE})
        ghost = await client.post("/api/session/login", json={
            "kind": "phone", "identifier": "13900000009",
            "password": "Nope-12345678", "device_id": DEVICE})
        return real, ghost

    real, ghost = _run(settings, redis_client, flow)
    assert real.status_code == ghost.status_code == 401
    assert real.json() == ghost.json()


# --------------------------------------------------------------------------- #
# 找回 / 重置
# --------------------------------------------------------------------------- #
def test_forgot_response_is_identical_for_existing_and_unknown_accounts(
        settings, redis_client):
    """唯一区别是**回显的令牌**（开发通道才有），文案与状态码必须一致。"""
    async def flow(client):
        await client.post("/api/session/register", json=_register(client))
        existed = await client.post("/api/session/forgot",
                                    json={"kind": "phone", "identifier": PHONE})
        ghost = await client.post("/api/session/forgot",
                                  json={"kind": "phone", "identifier": "13900000009"})
        return existed, ghost

    existed, ghost = _run(settings, redis_client, flow)
    assert existed.status_code == ghost.status_code == 200
    assert existed.json()["status"] == ghost.json()["status"] == "accepted"
    # 不存在时绝不能给出可用令牌
    assert "dev_token" not in ghost.json()
    assert existed.json().get("dev_token")


def test_reset_sets_new_password_and_revokes_sessions(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        old_token = reg["token"]
        forgot = (await client.post("/api/session/forgot",
                                    json={"kind": "phone", "identifier": PHONE})).json()
        reset = await client.post("/api/session/reset", json={
            "token": forgot["dev_token"], "new_password": "Brand-New-Passw0rd"})
        # 旧会话必须失效（改密后全端下线）
        old = await client.get("/api/accounts/me",
                               headers={"authorization": "Bearer " + old_token})
        relogin = await client.post("/api/session/login", json={
            "kind": "phone", "identifier": PHONE,
            "password": "Brand-New-Passw0rd", "device_id": DEVICE})
        return reset, old, relogin

    reset, old, relogin = _run(settings, redis_client, flow)
    assert reset.status_code == 200
    assert reset.json()["revoked_sessions"] >= 1
    assert old.status_code == 401, "改密后旧会话仍然可用 —— 这是安全缺陷"
    assert relogin.status_code == 200


def test_reset_token_cannot_be_reused(settings, redis_client):
    async def flow(client):
        await client.post("/api/session/register", json=_register(client))
        forgot = (await client.post("/api/session/forgot",
                                    json={"kind": "phone", "identifier": PHONE})).json()
        first = await client.post("/api/session/reset", json={
            "token": forgot["dev_token"], "new_password": "Brand-New-Passw0rd"})
        second = await client.post("/api/session/reset", json={
            "token": forgot["dev_token"], "new_password": "Another-Passw0rd"})
        return first, second

    first, second = _run(settings, redis_client, flow)
    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["code"] == "invalid_token"


# --------------------------------------------------------------------------- #
# 资料与昵称
# --------------------------------------------------------------------------- #
def test_profile_requires_member_tier(settings, redis_client):
    """访客没有 PROFILE_EDIT，不能读资料。"""
    async def flow(client):
        guest = (await client.post("/api/session/guest", json={})).json()
        return await client.get("/api/accounts/me",
                                headers={"authorization": "Bearer " + guest["token"]})

    response = _run(settings, redis_client, flow)
    assert response.status_code == 403
    assert response.json()["code"] == "capability_required"
    assert response.json()["need_tier"] == "member"


def test_nickname_customize_and_random_refresh(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        headers = {"authorization": "Bearer " + reg["token"]}
        custom = await client.post("/api/accounts/nickname",
                                   json={"nickname": "老张"}, headers=headers)
        randomized = await client.post("/api/accounts/nickname/random",
                                       headers=headers)
        me = await client.get("/api/accounts/me", headers=headers)
        return custom, randomized, me

    custom, randomized, me = _run(settings, redis_client, flow)
    assert custom.json()["nickname"] == "老张"
    assert randomized.json()["nickname"] and randomized.json()["nickname"] != "老张"
    assert me.json()["nickname"] == randomized.json()["nickname"]


def test_invalid_nickname_is_rejected_with_reason(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        headers = {"authorization": "Bearer " + reg["token"]}
        return await client.post("/api/accounts/nickname",
                                 json={"nickname": "我的号13800000007"}, headers=headers)

    response = _run(settings, redis_client, flow)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_nickname"


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
def test_models_require_member(settings, redis_client):
    async def flow(client):
        guest = (await client.post("/api/session/guest", json={})).json()
        return await client.get("/api/models",
                                headers={"authorization": "Bearer " + guest["token"]})

    response = _run(settings, redis_client, flow)
    assert response.status_code == 403


def test_model_list_shows_free_default_and_quota(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        return await client.get("/api/models",
                                headers={"authorization": "Bearer " + reg["token"]})

    body = _run(settings, redis_client, flow).json()
    assert body["binding"] is None              # 还没选过
    keys = {m["model_key"]: m for m in body["models"]}
    assert keys["local-vl"]["tier"] == "free"
    assert keys["local-vl"]["usable"] is True
    assert body["quota"]["free_remaining"] == settings.model_free_daily_calls


def test_select_catalog_model_and_unbind(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        headers = {"authorization": "Bearer " + reg["token"]}
        selected = await client.post("/api/models/select",
                                     json={"model_key": "local-vl"}, headers=headers)
        listed = await client.get("/api/models", headers=headers)
        removed = await client.request("DELETE", "/api/models", headers=headers)
        after = await client.get("/api/models", headers=headers)
        return selected, listed, removed, after

    selected, listed, removed, after = _run(settings, redis_client, flow)
    assert selected.status_code == 200
    assert selected.json()["binding"]["model_key"] == "local-vl"
    assert listed.json()["binding"]["model_key"] == "local-vl"
    assert removed.json()["removed"] is True
    assert after.json()["binding"] is None


def test_byok_key_is_never_returned_in_plaintext(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        headers = {"authorization": "Bearer " + reg["token"]}
        created = await client.post("/api/models/byok", json={
            "base_url": "https://api.example.com/v1",
            "model": "my-model",
            "api_key": "sk-demo-0123456789abcdef",
            "display_name": "我的模型",
        }, headers=headers)
        listed = await client.get("/api/models", headers=headers)
        return created, listed

    created, listed = _run(settings, redis_client, flow)
    assert created.status_code == 201
    for response in (created, listed):
        text = json.dumps(response.json(), ensure_ascii=False)
        assert "sk-demo-0123456789abcdef" not in text, "接口回传了明文密钥"
    assert created.json()["binding"]["api_key_hint"].endswith("cdef")


def test_byok_with_bad_base_url_is_rejected(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        return await client.post("/api/models/byok", json={
            "base_url": "ftp://nope", "model": "m", "api_key": "sk-demo-0123456789",
        }, headers={"authorization": "Bearer " + reg["token"]})

    response = _run(settings, redis_client, flow)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_base_url"


# --------------------------------------------------------------------------- #
# 定位 / 推荐 / 导航
# --------------------------------------------------------------------------- #
def test_location_requires_member(settings, redis_client):
    async def flow(client):
        guest = (await client.post("/api/session/guest", json={})).json()
        return await client.post("/api/location/recommend", json={},
                                 headers={"authorization": "Bearer " + guest["token"]})

    response = _run(settings, redis_client, flow)
    assert response.status_code == 403
    assert response.json()["code"] == "capability_required"


def test_recommend_is_coarsened_and_declares_missing_map(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        return await client.post("/api/location/recommend", json={
            "lat": 39.908722, "lon": 116.397499, "prefs": ["安静"], "limit": 3,
        }, headers={"authorization": "Bearer " + reg["token"]})

    body = _run(settings, redis_client, flow).json()
    assert body["located"] is True
    # 服务端只看得到粗化后的格点
    assert body["located_at_grid"] == [39.908, 116.397]
    assert body["grid_decimals"] == settings.geo_grid_decimals
    assert body["map_available"] is False
    assert "key" in body["map_note"]
    assert body["data_source"] == "seed-demo"     # 示例数据必须自报家门
    assert body["items"] and all(item["reason"] for item in body["items"][:3])


def test_recommend_without_location_still_works_and_says_why(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        return await client.post("/api/location/recommend", json={"prefs": ["咖啡"]},
                                 headers={"authorization": "Bearer " + reg["token"]})

    body = _run(settings, redis_client, flow).json()
    assert body["located"] is False
    assert body["located_at_grid"] is None
    assert any("未开启定位" in item["reason"] for item in body["items"])


def test_navigation_returns_three_apps_and_web_fallback(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        return await client.post("/api/location/navigation", json={
            "dest_lat": 39.908722, "dest_lon": 116.397499, "dest_name": "故宫",
        }, headers={"authorization": "Bearer " + reg["token"]})

    body = _run(settings, redis_client, flow).json()
    apps = [link["app"] for link in body["links"]]
    assert apps == ["amap", "baidu", "tencent", "web"]
    assert body["origin_shared"] is False, "默认不该把用户坐标交给第三方"


def test_navigation_rejects_invalid_coordinate(settings, redis_client):
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        return await client.post("/api/location/navigation", json={
            "dest_lat": 0, "dest_lon": 0,
        }, headers={"authorization": "Bearer " + reg["token"]})

    response = _run(settings, redis_client, flow)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_coordinate"


def test_chat_consumes_the_users_model_quota(settings, redis_client):
    """证明「按用户解析模型」真的接进了对话链路，而不是只做了个查询接口。

    发一次对话，免费额度必须 -1。这条如果失效，说明 `ChatOrchestrator`
    又退回用静态模型了 —— 那样"用户选的模型"就成了摆设。
    """
    async def flow(client):
        reg = (await client.post("/api/session/register", json=_register(client))).json()
        headers = {"authorization": "Bearer " + reg["token"]}
        before = (await client.get("/api/models", headers=headers)).json()
        await client.post("/api/chat/stream", json={"utterance": "公司年假几天"},
                          headers=headers)
        after = (await client.get("/api/models", headers=headers)).json()
        return before, after

    before, after = _run(settings, redis_client, flow)
    assert before["quota"]["free_remaining"] == settings.model_free_daily_calls
    assert after["quota"]["free_remaining"] == settings.model_free_daily_calls - 1
