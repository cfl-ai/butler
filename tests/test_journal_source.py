"""首轮痕迹的来源：会话事件账本 → `AgentTrace` → 评测（U84）。

这一组的重点是**接上了没有**，不是「函数返回对不对」。

`core/event_journal.py` 声称五类痕迹事件与 `evaluation/trace.py` 一一对应，
但此前全项目没有一处写入它们，也没有一处读回。于是 `repair_loop` 顶部那句
「首轮痕迹：调用方给（事件账本回放）」是一句声明——把账本递过去，
得到的是一串空痕迹，评测会把每条用例判成「没采集到」，而「没采集到」
在报告里和「没问题」长得很像。

所以这里测三件事：
1. **往返不丢字段**（否则「采集到了」只剩一半，判据会静默降级）；
2. **不完整/错误的数据如实反映**（没跑完就不是 completed，孤儿痕迹拒收，
   缺检索就留 None 而不是造一个空检索）；
3. **闭环真的用得上**（能给评测中台喂出首轮报告，且账本空时不去改代码）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

import pytest

from ai_butler.agents.repair_loop import RepairLoop, build_repair_loop
from ai_butler.core.event_journal import (
    EV_RETRIEVAL,
    EV_SESSION_END,
    EV_SESSION_START,
    EventJournal,
    MemoryJournalStore,
)
from ai_butler.evaluation.journal_source import JournalTraceSource, write_trace
from ai_butler.evaluation.platform import EvaluationCase
from ai_butler.evaluation.trace import (
    AgentTrace,
    ClaimedOutcome,
    ExceptionRecord,
    LlmCallRecord,
    RetrievalRecord,
    SecurityContext,
    ToolCallRecord,
    TraceError,
    trace_from_dict,
    trace_to_dict,
)
from ai_butler.core.permissions import CapabilityLevel, UserRole

_Q = "这款书包有什么特点？"
_EVIDENCE = {
    "bag-1": "这款书包容量为20升，重量1.2千克，采用防水面料。",
    "bag-2": "书包设有独立的笔记本电脑隔层，可容纳15英寸笔记本。",
    "bag-3": "背部透气网垫设计，长时间背负不闷热。",
    "bag-4": "提供黑、蓝、灰三种配色可选。",
    "bag-5": "拉链通过5000次开合测试，五年质保。",
}
_GOLDEN = tuple(_EVIDENCE)


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
def _journal(journal_id: str = "s-1") -> EventJournal:
    return EventJournal(journal_id=journal_id, store=MemoryJournalStore())


def _full_trace(trace_id: str = "t1", *, question: str = _Q,
                retrieved: tuple[str, ...] = _GOLDEN,
                answer: str = "20升，防水面料，有独立笔记本隔层。",
                completed: bool = True) -> AgentTrace:
    return AgentTrace(
        trace_id=trace_id, question=question, answer=answer,
        retrieval=RetrievalRecord(
            query=question, retrieved_ids=retrieved,
            scores=tuple(round(0.9 - 0.1 * i, 2) for i in range(len(retrieved)))),
        tool_calls=(ToolCallRecord(name="order.create", ok=True, executed=True,
                                   error_code="", latency_ms=12.5,
                                   arguments={"sku": "bag-1"},
                                   required=CapabilityLevel.L2_SIDE_EFFECT),),
        llm_calls=(LlmCallRecord(model="m1", prompt_tokens=120,
                                 completion_tokens=40, latency_ms=300.0),),
        exceptions=(ExceptionRecord(stage="execution", error_code="TASK_TIMEOUT",
                                    message="超时", timeout_ms=5000.0),),
        claimed_outcomes=(ClaimedOutcome(claim="已下单",
                                         depends_on=("order.create",)),),
        security=SecurityContext(agent_key="butler", role=UserRole.MEMBER),
        completed=completed)


def _case_dict(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "case_id": "bag-1", "question": _Q,
        "expected_answer": "容量20升，防水面料，有笔记本隔层",
        "expected_points": ["20升", "防水", "隔层"],
        "relevant_ids": list(_GOLDEN), "evidence_texts": dict(_EVIDENCE)}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# 一、序列化：两个方向必须对称
# --------------------------------------------------------------------------- #
def test_trace_dict_roundtrip_keeps_every_field():
    """`trace_to_dict` 与 `trace_from_dict` 是一对，字段缺一个就静默降级。

    静默降级的样子：轨迹里明明有 `executed=False` 的调用（权限闸拦下），
    过了 State 之后变成默认 True，维度三立刻把它当成「真的执行过」。
    """
    trace = _full_trace()
    back = trace_from_dict(trace_to_dict(trace))
    assert back == trace


def test_trace_dict_keeps_timeout_none_as_none():
    """超时字段的 `None` 表示「不是超时」，不能落成 0（0 毫秒会像一次瞬间超时）。"""
    trace = AgentTrace(
        trace_id="t9", question=_Q, answer="",
        exceptions=(ExceptionRecord(stage="interface", error_code="E1"),))
    payload = trace_to_dict(trace)
    assert payload["exceptions"][0]["timeout_ms"] is None
    assert trace_from_dict(payload).exceptions[0].timeout_ms is None


# --------------------------------------------------------------------------- #
# 二、账本往返：写进去的痕迹能原样回放
# --------------------------------------------------------------------------- #
def test_write_trace_then_replay_gives_same_trace():
    journal = _journal()
    write_trace(journal, _full_trace())
    traces = JournalTraceSource(journal).traces()
    assert len(traces) == 1
    assert traces[0] == _full_trace()


def test_two_runs_become_two_traces():
    """稳定性维度的分母来自这里：两次运行必须是两个 trace，不是一份合并的。"""
    journal = _journal()
    write_trace(journal, _full_trace("t1"))
    write_trace(journal, _full_trace("t2", answer="另一个说法"))
    traces = JournalTraceSource(journal).traces()
    assert [t.trace_id for t in traces] == ["t1", "t2"]
    assert traces[1].answer == "另一个说法"


def test_unfinished_run_is_not_completed():
    """没有 session_end 的运行**保留**（它真的发生过），但 `completed=False`。

    丢掉它的后果比标错更坏：一次崩溃会从账本里消失，稳定性看起来满分。
    """
    journal = _journal()
    journal.append(EV_SESSION_START, {"trace_id": "t1", "question": _Q})
    journal.append(EV_RETRIEVAL, {"query": _Q, "retrieved_ids": list(_GOLDEN)})
    traces = JournalTraceSource(journal).traces()
    assert len(traces) == 1
    assert traces[0].completed is False
    assert traces[0].answer == ""


def test_missing_retrieval_stays_none():
    """没采集到检索 ≠ 没召回。留 None，维度一判「无从判定」。"""
    journal = _journal()
    journal.append(EV_SESSION_START, {"trace_id": "t1", "question": _Q})
    journal.append(EV_SESSION_END, {"trace_id": "t1", "answer": "不知道"})
    traces = JournalTraceSource(journal).traces()
    assert traces[0].retrieval is None


# --------------------------------------------------------------------------- #
# 三、坏数据拒收
# --------------------------------------------------------------------------- #
def test_orphan_trace_events_are_refused():
    """痕迹落在任何 session_start 之外：埋点写错位置，不能当正常痕迹回放。"""
    journal = _journal()
    journal.append(EV_RETRIEVAL, {"query": _Q, "retrieved_ids": ["bag-1"]})
    with pytest.raises(TraceError) as exc:
        JournalTraceSource(journal).traces()
    assert "不在任何 session_start 之后" in str(exc.value)


def test_session_end_without_start_is_refused():
    journal = _journal()
    journal.append(EV_SESSION_END, {"answer": "x"})
    with pytest.raises(TraceError) as exc:
        JournalTraceSource(journal).traces()
    assert "没有对应的开始事件" in str(exc.value)


def test_session_start_without_question_is_refused():
    """question 为空时拒收，不造一条「所有维度都无从判定」的痕迹。"""
    journal = _journal()
    journal.append(EV_SESSION_START, {"trace_id": "t1"})
    journal.append(EV_SESSION_END, {"answer": "x"})
    with pytest.raises(TraceError) as exc:
        JournalTraceSource(journal).traces()
    assert "没有 question" in str(exc.value)


# --------------------------------------------------------------------------- #
# 四、按用例取
# --------------------------------------------------------------------------- #
def test_produce_filters_by_question():
    journal = _journal()
    write_trace(journal, _full_trace("t1"))
    write_trace(journal, _full_trace("t2", question="另一个问题"))
    source = JournalTraceSource(journal)
    got = asyncio.run(source.produce(EvaluationCase.from_dict(_case_dict())))
    assert [t.trace_id for t in got] == ["t1"]


def test_agent_key_filter():
    """一个账本里可能装着多个 Agent 的会话，评错对象比没评更坏。"""
    journal = _journal()
    write_trace(journal, _full_trace("t1"))
    write_trace(journal, AgentTrace(trace_id="t2", question=_Q, answer="",
                                    security=SecurityContext(agent_key="other")))
    got = JournalTraceSource(journal, agent_key="butler").traces()
    assert [t.trace_id for t in got] == ["t1"]


# --------------------------------------------------------------------------- #
# 五、接进闭环
# --------------------------------------------------------------------------- #
class _NoopHarness:
    """只记录「有没有被叫到」。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, _state: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("repair")
        return {"p10_harness": {"ran": False, "reason": "测试替身不修",
                                "patch": {"applied": False, "diff": ""}}}


def test_repair_loop_uses_journal_as_first_trace_source():
    """首轮痕迹不用调用方手工拼 dict —— 账本回放 → 评测 → 缺陷。"""
    journal = _journal()
    # 只召回 4/5：维度一不通过，于是闭环有可执行的输入
    write_trace(journal, _full_trace("t1", retrieved=_GOLDEN[:4]))
    harness = _NoopHarness()
    loop = RepairLoop(evaluator=_platform(), harness=harness,
                      source=JournalTraceSource(journal), max_rounds=1)

    outcome = asyncio.run(loop.run(cases=[_case_dict()]))   # 注意：没传 traces
    assert outcome.defect_count >= 1
    assert outcome.rounds_run == 1
    # 缺陷必须**带定位提示**，否则 harness 拿不到可执行输入
    record = loop._trail().get("R1")
    assert record is not None and record.defects


def test_no_traces_stops_before_touching_code():
    """账本里一次运行都没有：**不改代码**。

    继续走的后果是评测把每条用例判成「没采集到」，那会被当成缺陷送进 harness。
    用「查不到证据」驱动改代码，是这条链路上最坏的一种自动化。
    """
    harness = _NoopHarness()
    loop = RepairLoop(evaluator=_platform(), harness=harness,
                      source=JournalTraceSource(_journal()), max_rounds=3)

    outcome = asyncio.run(loop.run(cases=[_case_dict()]))
    assert harness.calls == []                     # 一次都没叫
    assert outcome.requires_manual is True
    assert "没有痕迹" in outcome.reason
    assert loop._trail().get("R1").stopped == "no_traces"


def test_build_repair_loop_wires_the_journal():
    loop = build_repair_loop(journal=_journal(), patch_root=None)
    assert isinstance(loop.source, JournalTraceSource)


def test_no_journal_means_no_source():
    """对照组：不给账本就不该有来源——默认值不能偷偷去翻一个不存在的账本。"""
    assert build_repair_loop().source is None


# --------------------------------------------------------------------------- #
# 六、保密：写入侧仍过 Redactor
# --------------------------------------------------------------------------- #
def test_write_trace_is_redacted():
    """账本的脱敏发生在 `EventJournal.append`，写入侧不能绕开它。"""
    journal = _journal()
    write_trace(journal, _full_trace(answer="请联系 13800138000 这个号码"))
    raw = json.dumps([e.as_dict() for e in journal.events], ensure_ascii=False)
    assert "13800138000" not in raw
    # 痕迹本身仍可回放（只是被脱敏，不是被丢掉）
    assert JournalTraceSource(journal).traces()[0].trace_id == "t1"


# --------------------------------------------------------------------------- #
def _platform():
    """真的评测中台（替身会把「首轮痕迹接得上」这件事测成假的）。"""
    from ai_butler.agents.capabilities import grant
    from ai_butler.agents.eval_platform import EvaluationPlatformAgent
    return EvaluationPlatformAgent(tools=grant(EvaluationPlatformAgent, {}))
