"""U64/U66 端到端守护：访客凭据 → 文字查询 → 语音被拒。

用 ASGI 传输直连应用，**不连真模型**：注入假网关与假检索，
把「谁被允许做什么」这条规则钉死。真模型链路由 test_speech_service 覆盖。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_butler.app import create_app


class _FakeGateway:
    """假的 LLM 网关：只验证调用契约，不产生任何真实推理。"""

    def __init__(self, pieces: tuple[str, ...] = ("公司", "年假", "五天")) -> None:
        self.pieces = pieces
        self.calls: list[dict] = []

    async def stream(self, *, user_id, messages, model=None, max_tokens=None):
        self.calls.append({"user_id": user_id, "model": model})
        for piece in self.pieces:
            yield piece


class _FakeSearch:
    """假检索：返回固定命中，用来验证「证据会被带进提示词」。"""

    def __init__(self, hits: list[dict] | None = None) -> None:
        self.hits = hits or []
        self.queries: list[tuple[str, str | None]] = []

    def __call__(self, query: str, user_id: str | None):
        self.queries.append((query, user_id))
        return self.hits


def _client(settings, redis_client, *, gateway=None, search=None):
    app = create_app(settings, redis_client,
                     chat_gateway=gateway or _FakeGateway(),
                     chat_search=search or _FakeSearch())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def test_guest_entry_issues_restricted_credential(settings, redis_client):
    async def main():
        async with _client(settings, redis_client) as client:
            return await client.post("/api/session/guest", json={"device_id": "phone"})

    response = asyncio.run(main())
    assert response.status_code == 200
    body = response.json()
    assert body["tier"] == "guest"
    assert body["user_id"].startswith("guest:")
    # 能力面只有文字查询 —— 这是 U64 的全部含义
    assert body["capabilities"] == ["text_query"]
    assert body["token"]


def test_guest_profile_reports_tier_and_remaining_quota(settings, redis_client):
    async def main():
        async with _client(settings, redis_client) as client:
            guest = (await client.post("/api/session/guest", json={})).json()
            headers = {"authorization": "Bearer " + guest["token"]}
            return await client.get("/api/access/profile", headers=headers)

    response = asyncio.run(main())
    body = response.json()
    assert body["tier"] == "guest"
    assert body["capabilities"] == ["text_query"]
    assert body["text_query_remaining"] == settings.guest_text_query_budget


def test_guest_voice_endpoints_are_403_with_capability_code(settings, redis_client):
    """语音四个接口对访客一律 403，且**说得出缺哪项能力**。"""

    async def main():
        async with _client(settings, redis_client) as client:
            guest = (await client.post("/api/session/guest", json={})).json()
            headers = {"authorization": "Bearer " + guest["token"]}
            payload = {"audio_base64": "AAAA", "text": "你好"}
            return {
                path: await client.post(path, headers=headers, json=payload)
                for path in ("/api/voice/asr", "/api/voice/tts",
                             "/api/voice/speaker/enroll", "/api/voice/speaker/verify")
            }

    for path, response in asyncio.run(main()).items():
        assert response.status_code == 403, path
        body = response.json()
        assert body["code"] == "capability_required", path
        assert body["need_tier"] == "member", path


def test_guest_text_query_streams_plan_and_answer(settings, redis_client):
    """访客文字查询必须真的可用：accepted → plan → chunk* → done。"""

    async def main():
        gateway = _FakeGateway(("年假", "五天"))
        search = _FakeSearch()
        async with _client(settings, redis_client, gateway=gateway, search=search) as client:
            guest = (await client.post("/api/session/guest", json={})).json()
            headers = {"authorization": "Bearer " + guest["token"]}
            response = await client.post("/api/chat/stream", headers=headers,
                                         json={"utterance": "公司年假几天"})
            return response, gateway, search

    response, gateway, search = asyncio.run(main())
    assert response.status_code == 200
    events = _events(response.text)
    types = [e["type"] for e in events]
    assert types[0] == "accepted"
    assert "plan" in types
    assert types.count("chunk") == 2
    assert types[-1] == "done"

    # 分类与工具要能被端侧拿到（这是「为什么这么查」的可解释性）
    plan_event = next(e for e in events if e["type"] == "plan")
    assert plan_event["data"]["category"] == "fact_question"
    assert "retrieval.hybrid_rerank" in plan_event["data"]["tools"]
    # 访客不下发播报文本（无 voice_output 能力）
    assert all(not e.get("speech") for e in events if e["type"] == "chunk")
    assert gateway.calls, "网关没被调用：文字查询没真正走回答链路"
    # 访客检索**只查公共域**，不能带真实 user_id 进检索
    assert search.queries and all(uid != "guest:" for _, uid in search.queries)


def test_guest_budget_runs_out_with_actionable_message(settings, redis_client):
    """额度用完 → 明确 error 事件并提示登录，而不是静默返回空回答。"""

    async def main():
        async with _client(settings, redis_client) as client:
            guest = (await client.post("/api/session/guest", json={})).json()
            headers = {"authorization": "Bearer " + guest["token"]}
            last = None
            for _ in range(settings.guest_text_query_budget + 1):
                last = await client.post("/api/chat/stream", headers=headers,
                                         json={"utterance": "年假几天"})
            return last

    events = _events(asyncio.run(main()).text)
    error = next(e for e in events if e["type"] == "error")
    assert error["data"]["code"] == "guest_budget_exhausted"
    assert "登录" in error["text"]


def test_guest_cannot_request_clarification_bypass(settings, redis_client):
    """多意图必须反问，且反问话术由系统渲染（不由模型生成）。"""

    async def main():
        async with _client(settings, redis_client) as client:
            guest = (await client.post("/api/session/guest", json={})).json()
            headers = {"authorization": "Bearer " + guest["token"]}
            return await client.post("/api/chat/stream", headers=headers,
                                     json={"utterance": "年假几天？另外怎么报销？"})

    events = _events(asyncio.run(main()).text)
    clarify = next(e for e in events if e["type"] == "clarify")
    assert "哪一个" in clarify["text"]
    assert events[-1]["type"] == "done"


def test_member_gets_speech_text_for_tts(settings, redis_client):
    """成员的文字查询带播报文本（访客没有）—— 这是 U64 的直接差异。"""

    async def main():
        async with _client(settings, redis_client) as client:
            token = await _member_token(settings, redis_client, client)
            headers = {"authorization": "Bearer " + token}
            return await client.post("/api/chat/stream", headers=headers,
                                     json={"utterance": "公司年假几天"})

    events = _events(asyncio.run(main()).text)
    chunks = [e for e in events if e["type"] == "chunk"]
    assert chunks and all(e.get("speech") for e in chunks)


async def _member_token(settings, redis_client, client) -> str:
    from ai_butler.middleware.session_auth import login as issue_session
    from ai_butler.core.session_store import SessionStore

    store = SessionStore(redis_client,
                         ttl_seconds=settings.session_ttl_seconds,
                         idle_timeout_seconds=settings.session_idle_timeout_seconds)
    token = await issue_session(store=store, settings=settings,
                                user_id="user-A", device_id="phone")
    return token.token


def test_retrieval_plan_endpoint_exposes_taxonomy(settings, redis_client):
    async def main():
        async with _client(settings, redis_client) as client:
            guest = (await client.post("/api/session/guest", json={})).json()
            headers = {"authorization": "Bearer " + guest["token"]}
            return await client.post("/api/retrieval/plan", headers=headers,
                                     json={"query": "我上次说的那个方案"})

    body = asyncio.run(main()).json()
    assert body["plan"]["category"] == "personal_recall"
    # 访客拿不到私有库，但计划里必须记账
    assert [d["tool"] for d in body["plan"]["dropped_tools"]] == ["vault.search"]
    assert "categories" in body and "tools" in body


def test_guest_mode_can_be_disabled(settings, redis_client):
    """关掉访客模式时明确 403，不假装还能用。"""
    disabled = settings.model_copy(update={"guest_enable": False})

    async def main():
        async with _client(disabled, redis_client) as client:
            return await client.post("/api/session/guest", json={})

    response = asyncio.run(main())
    assert response.status_code == 403
    assert response.json()["code"] == "guest_disabled"
