"""在线会话痕迹埋点的守护测试（U91）。

挡的是「声明 ≠ 能力」的又一形态：`evaluation/journal_source.py` 从 U84 起
就在文档里写着「线上链路每次运行结束时（埋点）」，而全项目没有任何一处
调用 `write_trace`——离线闭环的首轮痕迹来源因此永远是空的，
「评测中台发现错误」只能靠人工喂 traces。

本文件钉住三件事：

1. **聊一次，账本里就有一段会话**（start → retrieval → end，question 原样）；
2. **埋点失败不连坐回答**：留痕是手段，回答是目的；
3. **回放侧读到的正是埋点侧写的**：同一个文件、同一个 journal_id，
   折出来的痕迹与复评现跑的同形（`produce(case)` 同端口）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from ai_butler.app import create_app
from ai_butler.core.config import load_settings
from ai_butler.core.event_journal import (
    EV_SESSION_END,
    EV_SESSION_START,
    EventJournal,
    FileJournalStore,
    chat_journal_path,
)
from ai_butler.evaluation.journal_source import JournalTraceSource
from ai_butler.evaluation.platform import EvaluationCase
from ai_butler.evaluation.producer import trace_from_events

PASSWORD = "Demo-Passw0rd"


class _Gateway:
    """假网关：不产生真实推理，只回固定片段。"""

    def __init__(self, pieces: tuple[str, ...] = ("好的",)) -> None:
        self.pieces = pieces

    async def stream(self, *, user_id, messages, model=None, max_tokens=None,
                     provider=None, api_key=None, base_url=None):
        for piece in self.pieces:
            yield piece


class _Ev:
    def __init__(self, type: str, text: str = "", data: dict | None = None) -> None:
        self.type = type
        self.text = text
        self.data = data or {}


@pytest.fixture()
def settings(settings, tmp_path):
    return settings.model_copy(update={
        "storage_root": str(tmp_path),
        "account_store_root": str(tmp_path),
        "chat_verify_enable": False,
        "chat_history_enable": False,      # 关掉历史：埋点断言不受多轮干扰
    })


def _app(settings, redis, *, gateway=None):
    return create_app(
        settings, redis,
        chat_gateway=gateway or _Gateway(),
        chat_search=lambda q, u: [],
    )


async def _register(client, phone: str) -> str:
    response = await client.post("/api/session/register", json={
        "kind": "phone", "identifier": phone, "password": PASSWORD,
        "device_id": "phone"})
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _events(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines()
            if line.startswith("data: ")]


def _ask_sync(app, token: str, utterance: str) -> list[dict]:
    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            response = await client.post(
                "/api/chat/stream",
                headers={"authorization": "Bearer " + token},
                json={"utterance": utterance})
            return _events(response.text)
    return asyncio.run(main())


# --------------------------------------------------------------------------- #
# 一、折叠函数被两侧共用
# --------------------------------------------------------------------------- #
def test_trace_from_events_revise_replaces_and_done_completes():
    """共享折叠函数的语义：revise 是替换、done 缺失不算跑完、error 留痕。"""
    trace = trace_from_events(
        trace_id="t1", question="年假几天",
        events=[_Ev("chunk", text="初稿：三天"),
                _Ev("revise", text="资料里没有依据。"),
                _Ev("error", text="下游超时", data={"code": "timeout"}),
                _Ev("done", data={"retrieved_ids": ["doc-1"]})])
    assert trace.answer == "资料里没有依据。"
    assert "初稿" not in trace.answer
    assert trace.completed is True
    assert trace.retrieval.retrieved_ids == ("doc-1",)
    assert [e.error_code for e in trace.exceptions] == ["timeout"]
    # 不伪造工具调用与 token：这两个维度只能「无从判定」
    assert trace.tool_calls == () and trace.llm_calls == ()


# --------------------------------------------------------------------------- #
# 二、聊一次，账本里就有一段会话
# --------------------------------------------------------------------------- #
def test_chat_stream_journals_one_session_per_run(settings, redis_client):
    app = _app(settings, redis_client)
    token = asyncio.run(_register_via(app, "13800000901"))
    events = _ask_sync(app, token, "公司年假有几天")
    assert any(e["type"] == "done" for e in events)      # 回答照常给

    journal_file = chat_journal_path(settings)
    assert journal_file.exists(), "聊过之后账本必须有内容"
    journal = EventJournal(journal_id="chat",
                           store=FileJournalStore(journal_file))
    types = [e.type for e in journal.events]
    assert types[0] == EV_SESSION_START and types[-1] == EV_SESSION_END
    start = journal.events[0].payload
    assert start["question"] == "公司年假有几天"
    assert start["channel"] == "chat"
    assert start["user_id"]                           # 埋点要能追溯到人
    end = journal.events[-1].payload
    assert end["completed"] is True


def test_guest_chat_is_journals_with_its_tier(settings, redis_client):
    """访客会话也留痕：访客路径是最公开的那一面，它的缺陷同样要能被看见。

    `tier` 一并落账——回放与评测据此区分身份，不需要另猜。
    """
    app = _app(settings, redis_client)
    token = asyncio.run(_guest_via(app))
    _ask_sync(app, token, "附近有什么好玩的")
    journal = EventJournal(journal_id="chat",
                           store=FileJournalStore(chat_journal_path(settings)))
    assert journal.events[0].payload["tier"] == "guest"


def test_journal_failure_does_not_break_the_answer(settings, redis_client,
                                                   monkeypatch):
    """留痕是手段，回答是目的：落账炸掉也不能影响本轮回答。"""
    import ai_butler.app as app_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("账本不可用")

    monkeypatch.setattr(app_mod, "write_trace", _boom)
    app = _app(settings, redis_client)
    token = asyncio.run(_register_via(app, "13800000902"))
    events = _ask_sync(app, token, "公司年假有几天")
    done = [e for e in events if e["type"] == "done"]
    assert done, "埋点失败时回答必须照常完成"


# --------------------------------------------------------------------------- #
# 三、回放侧读到的正是埋点侧写的
# --------------------------------------------------------------------------- #
def test_replayed_trace_matches_what_was_journaled(settings, redis_client):
    """闭环首轮读到的痕迹 = 埋点写进去的那次运行（同文件、同 journal_id）。"""
    app = _app(settings, redis_client)
    token = asyncio.run(_register_via(app, "13800000903"))
    _ask_sync(app, token, "公司年假有几天")
    source = JournalTraceSource(EventJournal(
        journal_id="chat", store=FileJournalStore(chat_journal_path(settings))))
    traces = source.traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace.question == "公司年假有几天"
    assert trace.completed is True
    # 与复评现跑同端口：按用例取，取到的就是那一次运行
    case = EvaluationCase(case_id="c1", question="公司年假有几天")
    produced = asyncio.run(source.produce(case))
    assert [t.trace_id for t in produced] == [trace.trace_id]


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
async def _register_via(app, phone: str) -> str:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        return await _register(client, phone)


async def _guest_via(app) -> str:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        response = await client.post("/api/session/guest",
                                     json={"device_id": "guest-phone"})
        assert response.status_code == 200, response.text
        return response.json()["token"]
