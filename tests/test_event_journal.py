"""P2 append-only 会话事件账本守护测试（裁定 U57）。

覆盖三条硬语义 + 两处接线：
- append-only：存储层无删改 API；篡改/删除历史 → verify 拒（fail-closed）
- 可验真：seq 连续 + sha256 链
- 先脱敏后入账：明文隐私不许越过 Redactor
- 崩溃恢复：新实例从 store 续写 seq 不重号；checkpoint + replay 重建状态
- 接线：harness loop 实时落账（StageRecord/终局快照）；
  scheduler 的 Trace 埋点经 journal_observer 落账
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ai_butler.core import decisions
from ai_butler.core.event_journal import (
    EV_CHECKPOINT,
    EV_RETRIEVAL,
    EV_SESSION_END,
    EV_SPAN,
    EV_STAGE,
    EVENT_TYPES,
    EventJournal,
    FileJournalStore,
    JournalError,
    JournalStore,
    MemoryJournalStore,
    journal_observer,
)
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.context import ContextItem
from ai_butler.harness.loop import (
    STAGE_COLLECT,
    AgenticLoop,
    Proposal,
)
from ai_butler.harness.hooks import HookChain
from ai_butler.harness.sandbox import SandboxResult
from ai_butler.scheduler.observe import Trace


# ------------------------------ 基础语义 ------------------------------ #

def test_seq_is_contiguous_and_monotonic():
    j = EventJournal("s1", MemoryJournalStore())
    seqs = [j.append(EV_SPAN, {"n": i}).seq for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]
    assert [e.seq for e in j.events] == [1, 2, 3, 4, 5]


def test_hash_chain_binds_each_event_to_its_predecessor():
    j = EventJournal("s1", MemoryJournalStore())
    e1 = j.append(EV_SPAN, {"a": 1})
    e2 = j.append(EV_SPAN, {"b": 2})
    assert e1.prev_hash == "0" * 64
    assert e2.prev_hash == e1.hash
    assert e2.hash != e1.hash


def test_unknown_event_type_is_rejected():
    j = EventJournal("s1", MemoryJournalStore())
    with pytest.raises(JournalError):
        j.append("not_a_type", {})


def test_append_after_close_is_rejected():
    j = EventJournal("s1", MemoryJournalStore())
    j.close()
    with pytest.raises(JournalError):
        j.append(EV_SPAN, {})


def test_empty_journal_id_is_rejected():
    with pytest.raises(JournalError):
        EventJournal("  ", MemoryJournalStore())


# ------------------------------ append-only / 验真 ------------------------------ #

def test_store_protocol_has_no_mutating_api():
    """存储端口只暴露 load/append——删改 API 在协议层就不存在。"""
    for banned in ("delete", "remove", "update", "modify", "truncate"):
        assert not hasattr(JournalStore, banned)


def test_tampered_payload_fails_verification():
    """改历史事件的 payload → 重建即拒（fail-closed，不静默续写）。"""
    store = MemoryJournalStore()
    EventJournal("s1", store).append(EV_SPAN, {"k": "v"})
    store._data["s1"][0]["payload"] = {"k": "tampered"}
    with pytest.raises(JournalError, match="篡改|hash"):
        EventJournal("s1", store)


def test_deleted_event_leaves_seq_gap_and_is_rejected():
    store = MemoryJournalStore()
    j = EventJournal("s1", store)
    j.append(EV_SPAN, {"n": 1})
    j.append(EV_SPAN, {"n": 2})
    del store._data["s1"][0]
    with pytest.raises(JournalError, match="seq 断档"):
        EventJournal("s1", store)


def test_forged_prev_hash_breaks_the_chain():
    """prev_hash 指向不存在的前序 → 拒绝（hash 层或链层，任一即可）。"""
    store = MemoryJournalStore()
    j = EventJournal("s1", store)
    j.append(EV_SPAN, {"n": 1})
    j.append(EV_SPAN, {"n": 2})
    store._data["s1"][1]["prev_hash"] = "f" * 64
    with pytest.raises(JournalError, match="hash|链断裂"):
        EventJournal("s1", store)


# ------------------------------ 脱敏 ------------------------------ #

def test_payload_is_redacted_before_entering_the_journal():
    """手机号/邮箱入账前必须变成占位符——明文不许越过这一步。"""
    j = EventJournal("s1", MemoryJournalStore(), redactor=Redactor())
    e = j.append(EV_SPAN, {"note": "联系 13812345678 或 a@b.com"})
    assert "13812345678" not in json.dumps(e.payload, ensure_ascii=False)
    assert "a@b.com" not in json.dumps(e.payload, ensure_ascii=False)
    assert "<手机号>" in json.dumps(e.payload, ensure_ascii=False)


def test_redaction_is_deep_nested_payload_too():
    j = EventJournal("s1", MemoryJournalStore(), redactor=Redactor())
    e = j.append(EV_SPAN, {"outer": {"inner": ["13812345678"]}})
    assert "13812345678" not in json.dumps(e.payload, ensure_ascii=False)


# ------------------------------ 崩溃恢复 ------------------------------ #

def test_new_instance_resumes_seq_without_reuse():
    """模拟进程重启：同 store 新建账本，seq 延续不重号，链仍完整。"""
    store = MemoryJournalStore()
    j1 = EventJournal("s1", store)
    j1.append(EV_SPAN, {"n": 1})
    j1.append(EV_SPAN, {"n": 2})
    j2 = EventJournal("s1", store)          # 重启
    e = j2.append(EV_SPAN, {"n": 3})
    assert e.seq == 3
    j2.verify()


def test_checkpoint_plus_replay_rebuilds_state():
    j = EventJournal("s1", MemoryJournalStore())
    j.checkpoint({"total": 10})
    j.append(EV_SPAN, {"delta": 5})

    def fold(state, event):
        if event.type == EV_SPAN and "delta" in event.payload:
            state["total"] += event.payload["delta"]
        return state

    assert j.replay(fold=fold)["total"] == 15


def test_replay_starts_from_the_last_checkpoint():
    j = EventJournal("s1", MemoryJournalStore())
    j.checkpoint({"total": 1})
    j.append(EV_SPAN, {"delta": 1})
    j.checkpoint({"total": 100})            # 后一个 checkpoint 覆盖
    j.append(EV_SPAN, {"delta": 1})

    def fold(state, event):
        state["total"] += event.payload.get("delta", 0)
        return state

    assert j.replay(fold=fold)["total"] == 101


def test_replay_refuses_untrusted_journal():
    """回放前先 verify——外部篡改 store 后，新实例连构造都过不了。"""
    store = MemoryJournalStore()
    j = EventJournal("s1", store)
    j.append(EV_SPAN, {"n": 1})
    store._data["s1"][0]["payload"] = {"n": "tampered"}
    with pytest.raises(JournalError):
        EventJournal("s1", store).replay(fold=lambda s, e: s)


# ------------------------------ FileJournalStore ------------------------------ #

def test_file_store_persists_and_resumes(tmp_path):
    store = FileJournalStore(tmp_path / "journal.jsonl")
    j1 = EventJournal("s1", store)
    j1.append(EV_SPAN, {"n": 1})
    j1.append(EV_SPAN, {"n": 2})
    j2 = EventJournal("s1", store)          # 换实例 = 重启
    assert [e.seq for e in j2.events] == [1, 2]
    assert j2.append(EV_SPAN, {"n": 3}).seq == 3
    j2.verify()


def test_file_store_rejects_corrupted_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")
    with pytest.raises(JournalError, match="JSON"):
        EventJournal("s1", FileJournalStore(path))


def test_file_store_ignores_other_journal_ids(tmp_path):
    path = tmp_path / "journal.jsonl"
    store = FileJournalStore(path)
    j1 = EventJournal("s1", store)
    j1.append(EV_SPAN, {"n": 1})
    j2 = EventJournal("s2", store)
    j2.append(EV_SPAN, {"n": 1})
    assert [e.seq for e in EventJournal("s1", store).events] == [1]


# ------------------------------ 与 trace.py 的契约 ------------------------------ #

def test_event_types_align_with_trace_records():
    """评测中台五类过程痕迹必须在账本事件类型里有一一对应（数据源契约）。"""
    from ai_butler.evaluation.trace import (
        ClaimedOutcome, ExceptionRecord, LlmCallRecord,
        RetrievalRecord, ToolCallRecord,
    )
    mapping = {
        RetrievalRecord: EV_RETRIEVAL,
        ToolCallRecord: "tool_call",
        LlmCallRecord: "llm_call",
        ExceptionRecord: "exception",
        ClaimedOutcome: "claimed_outcome",
    }
    for record_cls, event_type in mapping.items():
        assert event_type in EVENT_TYPES, (
            f"{record_cls.__name__} → {event_type} 不在事件类型表里")


# ------------------------------ 接线：scheduler observe ------------------------------ #

def test_journal_observer_bridges_spans_into_the_journal():
    j = EventJournal("s1", MemoryJournalStore())
    trace = Trace(user_id="u1", observer=journal_observer(j))
    with trace.span("route", labels=("book_hotel",)):
        pass
    trace.finish()
    types = [e.type for e in j.events]
    assert EV_SPAN in types
    span_events = [e for e in j.events if e.type == EV_SPAN]
    assert any(e.payload.get("node") == "route" for e in span_events)
    assert any(e.payload.get("kind") == "trace_summary" for e in span_events)


def test_scheduler_with_journal_records_session_lifecycle(loop):
    """SceneScheduler(journal=...) 跑完一个任务，账本里有 span 流 + session_end。"""
    from tests.test_scheduler_graph import _scheduler

    j = EventJournal("journal-task-1", MemoryJournalStore())
    scheduler = _scheduler()
    scheduler.journal = j
    result = loop.run_until_complete(scheduler.run(
        user_id="u1", session_id="sess-1", utterance="订酒店",
        labels=("book_hotel",), trace_id="t1"))
    assert result.trace_id == "t1"
    types = [e.type for e in j.events]
    assert EV_SPAN in types
    assert EV_SESSION_END in types
    j.verify()


@pytest.fixture(scope="module")
def loop():
    """模块级事件循环。

    与 test_scheduler_graph.py 同款：必须 set_event_loop——同进程内其他用例
    调用 asyncio.run() 后会把当前事件循环置空，asyncpg 某些路径会去取「当前」循环，
    产生只在整包连跑时才冒头的偶发失败（E 类经验，见该文件原文注释）。
    """
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


# ------------------------------ 接线：harness loop ------------------------------ #

class _FakeSandbox:
    def run(self, command, timeout=30):
        return SandboxResult(command=command, exit_code=0, stdout="", stderr="")


def _loop(proposals, **over):
    pending = list(proposals)

    async def _decide(_context, _history):
        if pending:
            return pending.pop(0)
        return Proposal(summary="没有更多提案", done=False)

    from ai_butler.harness.loop import AgenticLoop
    return AgenticLoop(
        decide=_decide,
        hooks=HookChain.default(qa_verdict={"action": "pass"}),
        sandbox=_FakeSandbox(),
        **over)


def test_loop_with_journal_records_stages_and_final_checkpoint():
    j = EventJournal("s-loop", MemoryJournalStore())
    result = asyncio.run(_loop([Proposal("修复完成", done=True,
                                         commands=("pytest -q",))]).run(
        goal="修复缺陷", items=[ContextItem("i1", "缺陷", relevance=1.0)],
        redactor=Redactor(), journal=j))
    assert result.converged is True
    types = [e.type for e in j.events]
    assert EV_STAGE in types
    assert EV_CHECKPOINT in types
    # 阶段事件与内存 records 一一对应（实时落账，不是事后补写）
    stage_events = [e for e in j.events if e.type == EV_STAGE]
    assert len(stage_events) == len(result.records)
    # 终局快照含收敛结论——崩溃恢复后能拿到终局
    final = j.events[-1]
    assert final.type == EV_CHECKPOINT
    assert final.payload["loop"]["converged"] is True
    assert STAGE_COLLECT in final.payload["loop"]["stages"][0]["stage"]
    j.verify()


def test_loop_without_journal_behaves_unchanged():
    """不传 journal 时行为与接线前完全一致（不冒充已持久化）。"""
    result = asyncio.run(_loop([Proposal("修复完成", done=True)]).run(
        goal="修复缺陷", items=[ContextItem("i1", "缺陷", relevance=1.0)],
        redactor=Redactor()))
    assert result.converged is True


def test_loop_journal_crashes_still_record_final_checkpoint():
    """decide 抛异常时，finally 仍落终局快照（4 处 return + 异常路径全覆盖）。"""
    async def _boom(_context, _history):
        raise RuntimeError("LLM 挂了")

    from ai_butler.harness.loop import AgenticLoop
    loop_obj = AgenticLoop(decide=_boom,
                           hooks=HookChain.default(qa_verdict={"action": "pass"}),
                           sandbox=_FakeSandbox())
    j = EventJournal("s-boom", MemoryJournalStore())
    with pytest.raises(RuntimeError):
        asyncio.run(loop_obj.run(
            goal="修复缺陷", items=[ContextItem("i1", "缺陷", relevance=1.0)],
            redactor=Redactor(), journal=j))
    assert j.events[-1].type == EV_CHECKPOINT
    j.verify()


# ------------------------------ 台账登记 ------------------------------ #

def test_u57_is_registered():
    keys = {d.key for d in decisions.DECISIONS}
    assert "U57" in keys, "事件账本属计划书未定义项，必须登记 U57"
