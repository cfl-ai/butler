"""入口接线（U115）：wiki 编译产物注入 + function calling 承接层。

守的核心是「跑通」这三个字的三层含义：
1. **产出**：`WikiDigestProvider` 真能读页面、合成 digest（不是永远空）；
2. **注入**：digest 真的进提示词与 `plan` 事件（生成却没人读 = 没生成）；
3. **退回**：工具回路不可用时**安静退回**常规问答并说明原因，
   而不是让一次提问变成 500，也不是假装「工具答过了」。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, new_page
from ai_butler.agent_wiki.wiring import WikiDigestProvider, build_wiki_store, compile_on_startup
from ai_butler.app import build_entry_tool_answer, build_entry_wiki_digest
from ai_butler.retrieval.chat import ChatOrchestrator
from ai_butler.scheduler.chat_intake import ToolAnswer, answer_with_tools
from ai_butler.scheduler.intake import IntakeReport


# ------------------------------ 公用桩 ------------------------------ #
class _Gateway:
    def __init__(self, pieces: tuple[str, ...] = ("答案",)) -> None:
        self.pieces = pieces
        self.streamed: list[dict] = []

    async def stream(self, *, user_id, messages, **kwargs):
        self.streamed.append({"user_id": user_id, "messages": list(messages), **kwargs})
        for piece in self.pieces:
            yield piece


class _Report:
    """最小 `IntakeReport` 替身：只带 ran_count / outcomes / as_dict。

    `result` 必须给一个带 `.text` 的对象 —— 真实 `IntakeReport.ran` 的判据就是
    「result 不是 None」，替身若省掉它，`ToolAnswer.text` 会静默返回空串，
    测试就会以为「回路答了」，实际走了兜底（E-92 家族：判不了被记成判不过）。
    """

    def __init__(self, *, ran: bool, answer: str = "") -> None:
        self.ran_count = 1 if ran else 0
        self.disabled = False
        result = type("_R", (), {"text": answer, "rounds": 1, "tool_results": (),
                                 "degradation": (), "truncated": False})()
        probe = type("_O", (), {"ran": ran, "result": result if ran else None,
                                "unit": type("_U", (), {"text": "杭州天气"}),
                                "assignment": None, "tools": (), "skipped": ""})()
        self.outcomes = (probe,)

    def as_dict(self):
        return {"disabled": False, "ran_count": self.ran_count,
                "units": [{"text": "杭州天气", "answer": "",
                           "tools": ["search_materials"], "ran": bool(self.ran_count)}],
                "unassigned": []}


def _events(orchestrator: ChatOrchestrator, *, user_id="13800000201", query="杭州天气怎么样"):
    return asyncio.run(_collect(orchestrator, user_id=user_id, query=query))


async def _collect(orchestrator, *, user_id, query):
    return [e async for e in orchestrator.stream(user_id=user_id, query=query)]


def _make_wiki(tmp_path, settings, *, claims: int = 2):
    store = build_wiki_store(settings.model_copy(update={"wiki_root": str(tmp_path)}))
    for index in range(claims):
        store.write_page(_claim(f"cl-{index}", user_id=None))
    return store


def _claim(page_id: str, *, user_id: str | None = None):
    return new_page(CLAIM, page_id, confidence=0.9, claim_type="fact",
                    description=f"声明 {page_id}",
                    subject_id=f"entity:s-{page_id}", object_id=f"entity:o-{page_id}",
                    user_id=user_id)


# ------------------------------ 1. 产出 ------------------------------ #
def test_digest_provider_reads_pages_and_reports_scopes(settings, tmp_path):
    store = _make_wiki(tmp_path, settings)
    provider = WikiDigestProvider(settings.model_copy(update={"wiki_root": str(tmp_path)}),
                                  store=store)
    snapshot = provider("")
    assert snapshot.digest["claim_count"] == 2
    assert snapshot.digest["scopes"] == ["workspace"]
    assert snapshot.note == ""


def test_digest_provider_is_honest_when_wiki_is_empty(settings, tmp_path):
    provider = WikiDigestProvider(settings.model_copy(update={"wiki_root": str(tmp_path)}))
    snapshot = provider("")
    assert snapshot.digest["claim_count"] == 0
    assert "还没有声明页" in snapshot.note, "空库与未装配必须区分开"


def test_digest_provider_sees_vault_only_for_the_owner(settings, tmp_path):
    root = tmp_path
    store = build_wiki_store(settings.model_copy(update={"wiki_root": str(root)}))
    store.write_page(_claim("cl-a", user_id="user-A"), user_id="user-A")
    provider = WikiDigestProvider(settings.model_copy(update={"wiki_root": str(root)}),
                                  store=store)
    assert provider("user-A").digest["scopes"] == ["workspace", "vault"]
    assert provider("user-A").digest["claim_count"] == 1
    # 另一个账号看同一个 root：作用域照样查，但**一条也读不到**（行级隔离）
    other = provider("user-B")
    assert other.digest["claim_count"] == 0, "不得读到他人 Vault 的声明"
    assert other.digest["user_id"] == "user-B"


def test_startup_compile_is_skipped_when_switch_is_off(settings, tmp_path):
    off = settings.model_copy(update={"wiki_root": str(tmp_path),
                                      "wiki_compile_auto_run": False})
    assert compile_on_startup(off)["skipped"]


def test_startup_compile_actually_writes_the_machine_cache(settings, tmp_path):
    """「跑通」的证据是**文件真的落盘**，不是返回了一个 report 对象。"""
    store = _make_wiki(tmp_path, settings)
    on = settings.model_copy(update={"wiki_root": str(tmp_path),
                                     "wiki_compile_auto_run": True})
    report = compile_on_startup(on, store)
    assert report["page_count"] == 2
    compiled = tmp_path / "workspace" / "_compiled"
    assert (compiled / "agent-digest.json").is_file()
    assert (compiled / "pages.json").is_file()
    lines = [ln for ln in (compiled / "claims.jsonl").read_text(
        encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2, "claims.jsonl 每行一条声明"


def test_runtime_writes_are_compiled_on_the_following_read(settings, tmp_path):
    """运行期新写的页面不能等下次重启才进机器缓存 —— 编译要跟着读取走。"""
    on = settings.model_copy(update={"wiki_root": str(tmp_path),
                                     "wiki_compile_auto_run": True})
    store = build_wiki_store(on)
    store.write_page(_claim("cl-1"))
    provider = WikiDigestProvider(on, store=store)
    provider("")
    claims_file = tmp_path / "workspace" / "_compiled" / "claims.jsonl"
    assert len(claims_file.read_text(encoding="utf-8").strip().splitlines()) == 1

    # 再写一页，同一进程内立刻再读：缓存必须追平，不能返回旧快照
    store.write_page(_claim("cl-2"))
    snapshot = provider("")
    assert snapshot.digest["claim_count"] == 2
    assert len(claims_file.read_text(encoding="utf-8").strip().splitlines()) == 2


# ------------------------------ 2. 注入 ------------------------------ #
def test_prompt_puts_digest_before_material(settings, tmp_path):
    """"已知事实"必须在资料**之前**，并注明可能过期 —— 否则模型会当成证据引用。"""
    prompt = ChatOrchestrator._render_prompt(
        "杭州天气", [{"text": "资料正文", "evidence_ids": ["ev-1"], "source_id": "s"}],
        "", {"claims": [{"claim_id": "cl-1", "subject_id": "杭州", "object_id": "晴",
                         "status": "TRUE", "confidence": 0.9}], "truncated": False})
    assert prompt.index("【已知事实】") < prompt.index("【资料】")
    assert "可能过期" in prompt
    assert "cl-1" in prompt


def test_empty_digest_leaves_no_placeholder(settings):
    prompt = ChatOrchestrator._render_prompt("问", [], "", {"claims": []})
    assert "【已知事实】" not in prompt, "空 digest 不能留占位，占位会被当成一条事实"


def test_plan_event_carries_wiki_summary(settings, tmp_path):
    store = _make_wiki(tmp_path, settings)
    provider = WikiDigestProvider(settings.model_copy(update={"wiki_root": str(tmp_path)}),
                                  store=store)
    orchestrator = ChatOrchestrator(_Gateway(), search=None, verify_answer=False,
                                    wiki_digest=provider)
    events = _events(orchestrator)
    plan = next(e for e in events if e.type == "plan")
    assert plan.data["wiki"]["claims"] == 2
    assert plan.data["wiki"]["wired"] is True
    done = events[-1]
    assert done.data["wiki"]["claims"] == 2


def test_no_provider_is_reported_not_silently_skipped(settings):
    orchestrator = ChatOrchestrator(_Gateway(), search=None, verify_answer=False)
    events = _events(orchestrator)
    done = events[-1]
    assert any("Wiki 未装配" in n for n in done.data["notes"])


# ------------------------------ 3. 工具回路分支 ------------------------------ #
def test_tool_answer_takes_over_the_turn_and_emits_process_first(settings):
    calls: list[dict] = []

    async def tool_answer(*, user_id, utterance, intent_label="", model_kwargs=None):
        calls.append({"user_id": user_id, "utterance": utterance,
                      "intent_label": intent_label})
        return ToolAnswer(_Report(ran=True, answer="工具答的"), skipped="")

    gateway = _Gateway()
    orchestrator = ChatOrchestrator(gateway, search=None, verify_answer=False,
                                    tool_answer=tool_answer)
    events = _events(orchestrator)
    types = [e.type for e in events]
    assert "tools" in types, "工具过程必须先于答案发出"
    assert types.index("tools") < types.index("chunk")
    assert events[-1].data["source"] == "function_call"
    chunk = next(e for e in events if e.type == "chunk")
    assert chunk.text == "工具答的"
    assert gateway.streamed == [], "回路答出来之后不该再走一遍普通问答"
    assert calls[0]["user_id"] == "13800000201"


def test_skipped_tool_layer_falls_back_and_says_why(settings):
    async def tool_answer(**kwargs):
        return ToolAnswer(IntakeReport(disabled=True), skipped="function_calling_enable=false")

    gateway = _Gateway(("常规", "答案"))
    orchestrator = ChatOrchestrator(gateway, search=None, verify_answer=False,
                                    tool_answer=tool_answer)
    events = _events(orchestrator)
    assert [e.type for e in events if e.type == "chunk"]
    assert gateway.streamed, "跳过工具层后必须走常规问答"
    assert any("function_calling_enable=false" in n for n in events[-1].data["notes"])


def test_tool_layer_crash_does_not_break_the_answer(settings):
    async def tool_answer(**kwargs):
        raise RuntimeError("回路炸了")

    gateway = _Gateway(("兜底",))
    orchestrator = ChatOrchestrator(gateway, search=None, verify_answer=False,
                                    tool_answer=tool_answer)
    events = _events(orchestrator)
    assert any(e.type == "chunk" for e in events)
    assert any("工具回路失败" in n for n in events[-1].data["notes"])


# ------------------------------ 4. 入口装配 ------------------------------ #
def test_entry_tool_answer_is_absent_when_switch_is_off(settings):
    off = settings.model_copy(update={"function_calling_enable": False})
    assert build_entry_tool_answer(off, gateway=None, wiki_store=None) is None, (
        "开关关闭时不该装配 —— 「装了但没开」会让排障误以为链路是好的")


def test_entry_tool_answer_is_built_when_switch_is_on(settings, tmp_path):
    on = settings.model_copy(update={"function_calling_enable": True,
                                     "wiki_root": str(tmp_path)})
    store = build_wiki_store(on)
    fn = build_entry_tool_answer(on, gateway=object(), wiki_store=store)
    assert callable(fn)


def test_entry_wiki_digest_is_built_and_readable(settings, tmp_path):
    on = settings.model_copy(update={"wiki_root": str(tmp_path)})
    provider = build_entry_wiki_digest(on)
    assert provider is not None
    assert provider("").digest["claim_count"] == 0


# ------------------------------ 5. 回路本身的前置闸 ------------------------------ #
def test_answer_with_tools_skips_when_registry_is_empty(settings):
    from ai_butler.scheduler.tools import ToolRegistry

    on = settings.model_copy(update={"function_calling_enable": True})
    result = asyncio.run(answer_with_tools(
        gateway=object(), registry=ToolRegistry(), user_id="u", utterance="杭州天气",
        settings=on))
    assert result.ran is False
    assert "工具清单为空" in result.skipped


def test_answer_with_tools_skips_when_model_cannot_use_tools(settings):
    from ai_butler.scheduler.tools import ToolRegistry

    class _NoTools:
        def tool_capability(self):
            return False, "模型不具备 function calling 能力"

    on = settings.model_copy(update={"function_calling_enable": True})
    registry = ToolRegistry()
    registry.register("search_materials", lambda **kw: {}, description="检索",
                      params_schema={"type": "object", "properties": {}},
                      capability="retrieval")
    result = asyncio.run(answer_with_tools(
        gateway=_NoTools(), registry=registry, user_id="u", utterance="杭州天气",
        settings=on))
    assert result.ran is False
    assert "不具备 function calling 能力" in result.skipped
