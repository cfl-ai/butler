"""U73 多轮上下文：接线、隔离、退化、装配。

这一层的重点不是「历史能不能存」，而是四条不能被绕过的话：

* **接线**：装配出来的 orchestrator 必须真的持有 `ContextManager`
  （E-77：类有测试、链路没人构造，绿灯亮着功能不存在）。
* **隔离**：另一账号拿着别人的会话标识来问，读不到任何历史，且被重新签发。
* **退化要说**：没装配 / 没标识 / 访客 / 索引坏了，一律退化为单轮，
  但原因必须进 `notes`。静默退化会把「没接上」伪装成「新用户第一次提问」。
* **不写半轮**：模型失败、回答被截断时不留「有问无答」的历史。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ai_butler.app import (
    build_chat_context_manager,
    build_chat_orchestrator,
    create_app,
)
from ai_butler.core import access
from ai_butler.core.conversation_index import (
    ConversationError,
    InMemoryConversationIndex,
    RedisConversationIndex,
    is_wellformed,
    new_conversation_id,
    resolve,
)
from ai_butler.core.storage import FileStore
from ai_butler.infra.llm_gateway import GatewayError
from ai_butler.material_center.verification import VerificationResult
from ai_butler.retrieval import chat as chat_mod
from ai_butler.scheduler.context import ContextError, ContextManager, ContextPolicy

PASSWORD = "Demo-Passw0rd"
PHONE_A = "13800000101"
PHONE_B = "13800000102"


@pytest.fixture()
def settings(settings, tmp_path):
    """每个用例独立存储根，并**默认关掉复检** —— 复检会改写答案，干扰历史断言。"""
    return settings.model_copy(update={
        "storage_root": str(tmp_path),
        "account_store_root": str(tmp_path),
        "chat_verify_enable": False,
        "chat_history_enable": True,
        "chat_history_max_turns": 20,
        "chat_history_max_chars": 4000,
        "chat_history_keep_turns": 6,
        "chat_summary_max_chars": 600,
    })


class _Gateway:
    """假网关：记录每次调用收到的 messages，不产生真实推理。"""

    def __init__(self, pieces: tuple[str, ...] = ("好的",), fail_with=None) -> None:
        self.pieces = pieces
        self.fail_with = fail_with
        self.calls: list[dict] = []

    async def stream(self, *, user_id, messages, model=None, max_tokens=None,
                     provider=None, api_key=None, base_url=None):
        self.calls.append({"user_id": user_id, "messages": list(messages)})
        if self.fail_with is not None:
            raise self.fail_with
        for piece in self.pieces:
            yield piece

    @property
    def last_prompt(self) -> str:
        return self.calls[-1]["messages"][-1].content


def _app(settings, redis, *, gateway=None, search=None):
    return create_app(
        settings, redis,
        chat_gateway=gateway if gateway is not None else _Gateway(),
        chat_search=search if search is not None else (lambda q, u: []),
    )


def _run(settings, redis, flow, *, gateway=None, search=None):
    gateway = gateway if gateway is not None else _Gateway()
    app = _app(settings, redis, gateway=gateway, search=search)
    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await flow(client)
    return asyncio.run(main()), app, gateway


def _events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


def _accepted(events: list[dict]) -> dict:
    return events[0]


def _done(events: list[dict]) -> dict:
    return [e for e in events if e["type"] == "done"][-1]


async def _register(client, phone: str) -> str:
    response = await client.post("/api/session/register", json={
        "kind": "phone", "identifier": phone,
        "password": PASSWORD, "device_id": "phone"})
    assert response.status_code == 201, response.text
    return response.json()["token"]


async def _ask(client, token: str, utterance: str,
               conversation_id: str | None = None) -> list[dict]:
    body: dict = {"utterance": utterance}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.post(
        "/api/chat/stream", headers={"authorization": "Bearer " + token}, json=body)
    return _events(response.text)


def _history_file(settings, user_id: str, conversation_id: str):
    from pathlib import Path
    return Path(settings.storage_root) / "conversations" / user_id / f"{conversation_id}.json"


# --------------------------------------------------------------------------- #
# 一、接线：历史真的进了提示词
# --------------------------------------------------------------------------- #
def test_first_turn_issues_conversation_id(settings, redis_client):
    gateway = _Gateway()

    async def flow(client):
        token = await _register(client, PHONE_A)
        return await _ask(client, token, "公司年假有几天")

    events, app, _ = _run(settings, redis_client, flow, gateway=gateway)
    accepted = _accepted(events)
    assert accepted["type"] == "accepted"
    conversation_id = accepted["data"]["conversation_id"]
    assert is_wellformed(conversation_id), "会话标识必须由服务端签发 32 位十六进制"
    assert accepted["data"]["memory"] is True
    assert "签发" in accepted["data"]["note"]
    # 首轮没有历史可带，但**不是**「记忆没开」
    assert _done(events)["data"]["history"] is False
    assert _done(events)["data"]["remembered"] is True


def test_second_turn_carries_previous_exchange(settings, redis_client):
    """跨请求：第二句发给模型的提示词里必须出现第一句的问答。"""
    gateway = _Gateway(pieces=("年假", "五天"))

    async def flow(client):
        token = await _register(client, PHONE_A)
        first = await _ask(client, token, "公司年假有几天")
        conversation_id = _accepted(first)["data"]["conversation_id"]
        second = await _ask(client, token, "那第二个呢", conversation_id=conversation_id)
        return conversation_id, second

    (conversation_id, second), _, _ = _run(settings, redis_client, flow, gateway=gateway)
    assert _accepted(second)["data"]["conversation_id"] == conversation_id
    assert _accepted(second)["data"]["note"] == "", "沿用自己的会话不该报异常"
    prompt = gateway.last_prompt
    assert "【对话历史】" in prompt
    assert "公司年假有几天" in prompt
    assert "年假五天" in prompt
    # 当前问题仍然单独出现，没被历史吞掉
    assert "【用户问题】那第二个呢" in prompt
    assert _done(second)["data"]["history"] is True


def test_history_survives_a_new_process(settings, redis_client):
    """落盘而不是进程内缓存：换一个 app 实例仍能读到（Redis 索引仍在）。"""
    gateway = _Gateway(pieces=("年假", "五天"))

    async def first_flow(client):
        token = await _register(client, PHONE_A)
        events = await _ask(client, token, "公司年假有几天")
        return _accepted(events)["data"]["conversation_id"], token

    (conversation_id, token), _, _ = _run(settings, redis_client, first_flow, gateway=gateway)

    async def second_flow(client):
        return await _ask(client, token, "那第二个呢", conversation_id=conversation_id)

    second, _, _ = _run(settings, redis_client, second_flow, gateway=gateway)
    assert "公司年假有几天" in gateway.last_prompt
    assert _done(second)["data"]["history"] is True


# --------------------------------------------------------------------------- #
# 二、隔离：别人的标识读不到任何东西
# --------------------------------------------------------------------------- #
def test_other_account_cannot_read_history_by_guessing_id(settings, redis_client):
    gateway = _Gateway(pieces=("订单", "已发货"))

    async def flow(client):
        alice = await _register(client, PHONE_A)
        bob = await _register(client, PHONE_B)
        first = await _ask(client, alice, "我的订单号是多少")
        alice_conversation = _accepted(first)["data"]["conversation_id"]
        # Bob 拿着 Alice 的标识来问
        second = await _ask(client, bob, "刚才那个呢",
                            conversation_id=alice_conversation)
        return alice_conversation, second

    (alice_conversation, second), _, _ = _run(settings, redis_client, flow, gateway=gateway)
    accepted = _accepted(second)
    assert accepted["data"]["conversation_id"] != alice_conversation
    assert "不属于当前账号" in accepted["data"]["note"]
    # 关键：Bob 的提示词里**一个字**的 Alice 历史都不能有
    assert "我的订单号是多少" not in gateway.last_prompt
    assert "【对话历史】" not in gateway.last_prompt
    assert _done(second)["data"]["history"] is False


def test_forged_conversation_id_is_reissued(settings, redis_client):
    """格式合法但从未签发过 —— 同样不认，且理由不区分「不存在」与「不是你的」。"""
    gateway = _Gateway()

    async def flow(client):
        token = await _register(client, PHONE_A)
        return await _ask(client, token, "年假几天", conversation_id=new_conversation_id())

    events, _, _ = _run(settings, redis_client, flow, gateway=gateway)
    accepted = _accepted(events)
    assert "不属于当前账号" in accepted["data"]["note"]
    assert is_wellformed(accepted["data"]["conversation_id"])


def test_malformed_conversation_id_is_reissued(settings, redis_client):
    """路径穿越形状的标识连格式都过不了，更不该被拿去拼路径。"""
    gateway = _Gateway()

    async def flow(client):
        token = await _register(client, PHONE_A)
        return await _ask(client, token, "年假几天",
                          conversation_id="../../etc/passwd")

    events, _, _ = _run(settings, redis_client, flow, gateway=gateway)
    accepted = _accepted(events)
    assert "格式非法" in accepted["data"]["note"]
    assert is_wellformed(accepted["data"]["conversation_id"])


def test_guest_never_gets_a_conversation(settings, redis_client):
    """访客原话属 PII：不建索引、不落盘、不下发标识。"""
    gateway = _Gateway()

    async def flow(client):
        guest = await client.post("/api/session/guest", json={"device_id": "phone"})
        token = guest.json()["token"]
        return await _ask(client, token, "公司年假有几天")

    events, _, _ = _run(settings, redis_client, flow, gateway=gateway)
    accepted = _accepted(events)
    assert accepted["data"]["conversation_id"] == ""
    assert accepted["data"]["memory"] is False
    assert "访客" in accepted["data"]["note"]
    # 落盘目录压根不该出现
    from pathlib import Path
    assert not (Path(settings.storage_root) / "conversations").exists()
    # 提示词里也没有历史段
    assert "【对话历史】" not in gateway.last_prompt


# --------------------------------------------------------------------------- #
# 三、退化必须说出口（E-77 的正向守护）
# --------------------------------------------------------------------------- #
def test_unwired_orchestrator_says_single_turn(settings, workdir):
    """没装配上下文时不能装作有记忆，原因必须出现在 notes 里。"""
    gateway = _Gateway()
    orchestrator = build_chat_orchestrator(
        gateway=gateway, search=lambda q, u: [], settings=settings,
        context_manager=None)

    async def collect():
        return [event.as_dict() async for event in orchestrator.stream(
            user_id="u_test_member", query="公司年假有几天",
            tier=access.AccessTier.MEMBER, conversation_id=new_conversation_id())]

    events = asyncio.run(collect())
    done = [e for e in events if e["type"] == "done"][-1]
    assert any("未装配" in note for note in done["data"]["notes"])
    assert done["data"]["history"] is False
    assert done["data"]["remembered"] is False


def test_missing_conversation_id_says_single_turn(settings, workdir):
    gateway = _Gateway()
    manager = build_chat_context_manager(
        FileStore(workdir / "storage", settings=settings), settings)
    orchestrator = build_chat_orchestrator(
        gateway=gateway, search=lambda q, u: [], settings=settings,
        context_manager=manager)

    async def collect():
        return [event.as_dict() async for event in orchestrator.stream(
            user_id="u_test_member", query="公司年假有几天",
            tier=access.AccessTier.MEMBER)]

    events = asyncio.run(collect())
    done = [e for e in events if e["type"] == "done"][-1]
    assert any("未提供会话标识" in note for note in done["data"]["notes"])


def test_broken_index_degrades_to_single_turn(settings, redis_client):
    """索引后端炸了：不影响回答，但要退化成单轮并说明。"""
    class _ExplodingIndex:
        async def issue(self, user_id):
            raise ConversationError("索引不可用")

    gateway = _Gateway()
    app = _app(settings, redis_client, gateway=gateway)
    app.state.conversation_index = _ExplodingIndex()

    async def flow(client):
        token = await _register(client, PHONE_A)
        return await _ask(client, token, "公司年假有几天")

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await flow(client)

    events = asyncio.run(main())
    accepted = _accepted(events)
    assert accepted["data"]["conversation_id"] == ""
    assert "会话索引不可用" in accepted["data"]["note"]


# --------------------------------------------------------------------------- #
# 四、不写半轮
# --------------------------------------------------------------------------- #
def test_gateway_failure_writes_nothing(settings, redis_client):
    gateway = _Gateway(fail_with=GatewayError("上游 502"))

    async def flow(client):
        token = await _register(client, PHONE_A)
        events = await _ask(client, token, "公司年假有几天")
        return token, events

    (token, events), _, _ = _run(settings, redis_client, flow, gateway=gateway)
    assert [e for e in events if e["type"] == "error"]
    from pathlib import Path
    conversations = Path(settings.storage_root) / "conversations"
    assert not conversations.exists() or not any(conversations.rglob("*.json"))


def test_revised_answer_is_what_gets_remembered(settings, redis_client, monkeypatch):
    """复检推翻初稿时，历史里存的是**用户看到的那一版**，不是被推翻的初稿。"""
    monkeypatch.setattr(chat_mod, "verify", lambda *a, **k: VerificationResult(
        grounded=False, attribution_rate=0.0, abstained=True))
    monkeypatch.setattr(chat_mod, "render_abstention", lambda v: "资料不足，暂不回答。")
    settings = settings.model_copy(update={"chat_verify_enable": True})
    gateway = _Gateway(pieces=("年假", "五天"))

    async def flow(client):
        token = await _register(client, PHONE_A)
        first = await _ask(client, token, "公司年假有几天")
        conversation_id = _accepted(first)["data"]["conversation_id"]
        second = await _ask(client, token, "再说一次", conversation_id=conversation_id)
        return second

    second, _, _ = _run(settings, redis_client, flow, gateway=gateway)
    assert [e for e in second if e["type"] == "revise"]
    prompt = gateway.last_prompt
    assert "资料不足，暂不回答。" in prompt
    assert "年假五天" not in prompt, "被推翻的初稿不得进历史"


def test_append_exchange_rejects_half_turn(settings, workdir):
    manager = ContextManager(FileStore(workdir / "storage", settings=settings),
                             policy=ContextPolicy())
    with pytest.raises(ContextError):
        manager.append_exchange("u_x", "c" * 32, question="问", answer="   ")


# --------------------------------------------------------------------------- #
# 五、装配断言（E-77 的直接守护）
# --------------------------------------------------------------------------- #
def test_app_orchestrator_is_wired_with_context(settings, redis_client):
    """**构造这件事本身要被断言**：生产装配不允许漏掉上下文。"""
    app = _app(settings, redis_client)
    assert app.state.chat_orchestrator.context_manager is not None
    assert isinstance(app.state.chat_orchestrator.context_manager, ContextManager)
    assert app.state.conversation_index is not None


def test_context_manager_policy_comes_from_settings(settings, redis_client):
    settings = settings.model_copy(update={"chat_history_max_turns": 9,
                                           "chat_history_max_chars": 1234})
    app = _app(settings, redis_client)
    policy = app.state.chat_orchestrator.context_manager.policy
    assert policy.max_turns == 9
    assert policy.max_chars == 1234


def test_history_can_be_switched_off(settings, redis_client):
    settings = settings.model_copy(update={"chat_history_enable": False})
    gateway = _Gateway()

    async def flow(client):
        token = await _register(client, PHONE_A)
        return await _ask(client, token, "公司年假有几天")

    events, _, _ = _run(settings, redis_client, flow, gateway=gateway)
    accepted = _accepted(events)
    assert accepted["data"]["memory"] is False
    assert "关闭" in accepted["data"]["note"]


# --------------------------------------------------------------------------- #
# 六、索引表本体
# --------------------------------------------------------------------------- #
def test_index_issue_and_owns():
    async def main():
        index = InMemoryConversationIndex()
        conversation_id = await index.issue("u_a")
        return (conversation_id,
                await index.owns("u_a", conversation_id),
                await index.owns("u_b", conversation_id),
                await index.list_for("u_a"))

    conversation_id, owns_a, owns_b, listed = asyncio.run(main())
    assert is_wellformed(conversation_id)
    assert owns_a is True
    assert owns_b is False
    assert listed == (conversation_id,)


def test_index_refuses_ownerless_conversation():
    async def main():
        index = InMemoryConversationIndex()
        with pytest.raises(ConversationError):
            await index.issue("")
        with pytest.raises(ConversationError):
            await index.issue("guest:abc")

    asyncio.run(main())


def test_redis_index_fails_closed_when_backend_raises():
    class _Broken:
        async def hget(self, *a, **k):
            raise RuntimeError("redis down")

    async def main():
        index = RedisConversationIndex(_Broken(), ttl_seconds=100)
        return await index.owns("u_a", new_conversation_id())

    # 后端异常时**不能**放行 —— 放行一次就是隔离失效一次
    assert asyncio.run(main()) is False


def test_resolve_never_returns_foreign_conversation():
    async def main():
        index = InMemoryConversationIndex()
        alice = await index.issue("u_alice")
        return await resolve(index, user_id="u_bob", requested=alice)

    resolution = asyncio.run(main())
    assert resolution.issued is True
    assert resolution.conversation_id not in ("", None)
    assert "不属于当前账号" in resolution.note


# --------------------------------------------------------------------------- #
# 七、指代消解只作用于检索侧
# --------------------------------------------------------------------------- #
def test_anaphora_expands_search_text_only():
    from ai_butler.retrieval.chat import ChatOrchestrator

    text, note = ChatOrchestrator._search_text("那第二个呢", "公司年假有几天")
    assert text == "公司年假有几天 那第二个呢"
    assert "指代消解" in note

    # 长句不扩展：自带足够语义，拼上一轮只会稀释
    long_query = "我想了解一下公司在不同城市的分支机构的年假政策有什么差异"
    text, note = ChatOrchestrator._search_text(long_query, "公司年假有几天")
    assert text == long_query
    assert note == ""

    # 没有上一轮就不扩展
    text, note = ChatOrchestrator._search_text("那第二个呢", "")
    assert text == "那第二个呢"
    assert note == ""
