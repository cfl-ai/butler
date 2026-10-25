"""首轮痕迹的来源：从会话事件账本回放 `AgentTrace`（U84）。

计划书依据
----------
- 三、P9「全维度指标监控看板搭建」「日志沉淀与案例分类」
- 三、P10 触发时机「7×24 小时后台离线自动运行」——**离线**这个前提意味着
  首轮评测读的是**已经发生过的会话**，不是当场再跑一遍

补的是哪一段
------------
`core/event_journal.py`（U57）在模块顶部声称「前五类与
`evaluation/trace.py` 的过程痕迹逐一对齐（评测数据源）」。这句话当时只兑现了
**常量**：`EV_RETRIEVAL` / `EV_TOOL_CALL` / `EV_LLM_CALL` / `EV_EXCEPTION` /
`EV_CLAIMED_OUTCOME` 五个字符串定义在那里，而全项目**没有任何一处写入它们，
也没有任何一处读回它们**。

于是 `agents/repair_loop.py` 顶部那句「首轮痕迹：调用方给（线上日志 /
U57 事件账本回放）」是一句**声明**：账本回放器不存在，就算把账本递过去，
也只会得到一串空痕迹，评测会把每条用例都判成「没采集到」——那和「没缺陷」
长得一模一样。这正是项目铁律「声明 ≠ 能力」要抓的形态。

本模块把这条契约两侧都补上：

| 侧 | 函数/类 | 谁调用 |
|---|---|---|
| 写 | `write_trace(journal, trace)` | 线上链路每次运行结束时（埋点） |
| 读 | `JournalTraceSource.produce(case)` | 离线闭环的首轮评测 |

**一个会话 = 一次运行 = 一个 trace。**`session_start` 开一段，`session_end`
结一段，五类痕迹按 seq 归入**当前段**。没有 `session_end` 的段一律
`completed=False`：没正常结束的运行不该被算作「稳定」，
这是稳定性维度唯一诚实的判法（`evaluation/dimensions.py` 把
`completed=False` 计入「结果不一致」）。

三条纪律
--------
1. **端口与 `StreamTraceProducer` 一致**（`.produce(case)`，async）。评测方
   不该关心痕迹是「现跑出来的」还是「从账本回放出来的」——两条来源互换时，
   评测侧一行都不用改。
2. **不补、不猜**。缺 retrieval 就留 `None`（维度一判「无从判定」，
   而不是判「召回率 0」：没采集到 ≠ 没召回）。缺 `question` 直接抛错，
   不造一条 question 为空的痕迹。
3. **孤儿痕迹拒收**。五类痕迹事件若落在任何 `session_start` 之外，
   说明埋点写错了位置——这时抛 `TraceError`，不静默丢弃。
   丢弃的结果是「这段痕迹在账本里有、在评测里没有」，查起来极难。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ai_butler.core.event_journal import (
    EV_CLAIMED_OUTCOME,
    EV_EXCEPTION,
    EV_LLM_CALL,
    EV_RETRIEVAL,
    EV_SESSION_END,
    EV_SESSION_START,
    EV_TOOL_CALL,
    Event,
    EventJournal,
)
from ai_butler.core.permissions import CapabilityLevel, UserRole
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
)

__all__ = ["JournalTraceSource", "write_trace"]

#: 段内会被折进 trace 的五类事件（顺序即它们的语义顺序，不决定写入顺序）
_TRACE_EVENTS: tuple[str, ...] = (
    EV_RETRIEVAL, EV_TOOL_CALL, EV_LLM_CALL, EV_EXCEPTION, EV_CLAIMED_OUTCOME,
)


# --------------------------------------------------------------------------- #
# 一、写侧：把一次运行写进会话账本
# --------------------------------------------------------------------------- #
def write_trace(journal: EventJournal, trace: AgentTrace, *,
                extra: Mapping[str, Any] | None = None) -> tuple[Event, ...]:
    """把一次运行的痕迹写进会话账本。**返回写入的事件，便于断言与排障。**

    `extra` 是附加到 `session_start` 上的会话字段（如 `user_id` / `channel`），
    由调用方给：本模块不知道线上会话还有哪些标识，凭空补会与运行时对不上。

    顺序按「一次运行真实发生的顺序」：开始 → 检索 → 工具 → 大模型 →
    异常 → 声称 → 结束。这不是审美问题：账本是 append-only 的，
    顺序错了以后无法从 seq 上还原「异常发生在第几次调用之后」。
    """
    if not trace.question.strip():
        raise TraceError("trace.question 为空，不能入账（回放时会造出空痕迹）")

    events: list[Event] = [journal.append(EV_SESSION_START, {
        "trace_id": trace.trace_id,
        "question": trace.question,
        **_security_fields(trace.security),
        **dict(extra or {}),
    })]

    if trace.retrieval is not None:
        events.append(journal.append(EV_RETRIEVAL, {
            "query": trace.retrieval.query,
            "retrieved_ids": list(trace.retrieval.retrieved_ids),
            "scores": list(trace.retrieval.scores),
        }))

    for call in trace.tool_calls:
        events.append(journal.append(EV_TOOL_CALL, {
            "name": call.name, "ok": bool(call.ok),
            "executed": bool(call.executed),
            "error_code": call.error_code, "latency_ms": float(call.latency_ms),
            "arguments": dict(call.arguments),
            "required": _level_value(call.required),
        }))

    for call in trace.llm_calls:
        events.append(journal.append(EV_LLM_CALL, {
            "model": call.model,
            "prompt_tokens": int(call.prompt_tokens),
            "completion_tokens": int(call.completion_tokens),
            "latency_ms": float(call.latency_ms),
        }))

    for broken in trace.exceptions:
        events.append(journal.append(EV_EXCEPTION, {
            "stage": broken.stage, "error_code": broken.error_code,
            "message": broken.message,
            # None 要显式写进去：缺这个键与「不是超时」是两件事
            "timeout_ms": (None if broken.timeout_ms is None
                           else float(broken.timeout_ms)),
        }))

    for claim in trace.claimed_outcomes:
        events.append(journal.append(EV_CLAIMED_OUTCOME, {
            "claim": claim.claim, "depends_on": list(claim.depends_on)}))

    events.append(journal.append(EV_SESSION_END, {
        "trace_id": trace.trace_id,
        "answer": trace.answer,
        "completed": bool(trace.completed),
    }))
    return tuple(events)


def _level_value(level: CapabilityLevel) -> int:
    return int(getattr(level, "value", level))


def _role_value(role: UserRole) -> int:
    return int(getattr(role, "value", role))


def _security_fields(security: SecurityContext | None) -> dict[str, Any]:
    if security is None:
        return {}
    return {"agent_key": security.agent_key,
            "agent_level": _level_value(security.agent_level),
            "role": _role_value(security.role)}


# --------------------------------------------------------------------------- #
# 二、读侧：回放成 `AgentTrace`
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class JournalTraceSource:
    """从会话事件账本回放痕迹。**首轮评测的默认来源。**

    `agent_key` 给了就只取该 Agent 的会话：一个 journal 可能装着
    调度器与子 Agent 的多段运行，评错对象比没评更坏。
    """

    journal: EventJournal
    agent_key: str | None = None

    def traces(self) -> tuple[AgentTrace, ...]:
        """账本里的全部运行（按发生顺序）。"""
        return tuple(trace for trace in
                     (self._build(segment) for segment in self._segments())
                     if trace is not None)

    async def produce(self, case: EvaluationCase) -> Sequence[AgentTrace]:
        """按用例取痕迹。与 `StreamTraceProducer.produce` **同形**。

        没有 `await` 不是漏了：这里没有 IO（账本已在构造时校验并载入内存），
        但签名必须与流式生产端一致——否则评测方得知道痕迹是从哪来的。
        """
        return tuple(trace for trace in self.traces()
                     if trace.question == case.question)

    # ------------------------------------------------------------------ #
    def _segments(self) -> tuple[tuple[Event, list[Event], Event | None], ...]:
        """按 `session_start` 切段，返回 `(开始, 段内痕迹, 结束或 None)`。

        `结束为 None` 表示这段运行**没有正常收尾**（崩溃 / 被中止）。
        它不是「坏了」——它是真实发生过的一次运行，要保留下来让稳定性维度
        把它计入「结果不一致」，而不是让它从账本里凭空消失。
        """
        segments: list[tuple[Event, list[Event], Event | None]] = []
        current: Event | None = None
        body: list[Event] = []
        for event in self.journal.events:
            if event.type == EV_SESSION_START:
                if current is not None:
                    segments.append((current, body, None))
                current, body = event, []
            elif event.type in _TRACE_EVENTS:
                if current is None:
                    raise TraceError(
                        f"事件 seq={event.seq}（{event.type}）不在任何 "
                        f"session_start 之后——埋点位置错了，或者这一段会话"
                        f"的开始事件被删过。把它当正常痕迹回放，会让评测看到"
                        f"一段没有起点的运行")
                body.append(event)
            elif event.type == EV_SESSION_END:
                if current is None:
                    raise TraceError(
                        f"事件 seq={event.seq}（session_end）没有对应的开始事件")
                segments.append((current, body, event))
                current, body = None, []
            # 其余事件（span / stage / context_shadow / checkpoint）不参与
            # trace 组装：它们属于运行时观测，不是评测的七维度输入。
        if current is not None:
            segments.append((current, body, None))
        return tuple(segments)

    def _build(self, segment: tuple[Event, list[Event], Event | None]) -> AgentTrace | None:
        start, body, end = segment
        payload = start.payload or {}
        question = str(payload.get("question") or "")
        if not question.strip():
            raise TraceError(
                f"session_start（seq={start.seq}）没有 question，无法还原成痕迹："
                f"一条 question 为空的 trace 会被所有维度判成「无从判定」，"
                f"看起来像评测失败，实际是埋点缺字段")

        agent_key = str(payload.get("agent_key") or "")
        if self.agent_key is not None and agent_key != self.agent_key:
            return None

        retrieval: RetrievalRecord | None = None
        tools: list[ToolCallRecord] = []
        llms: list[LlmCallRecord] = []
        breaks: list[ExceptionRecord] = []
        claims: list[ClaimedOutcome] = []
        answer, completed = "", False

        for event in body:
            data = event.payload or {}
            if event.type == EV_RETRIEVAL:
                retrieval = _guard(event, "RetrievalRecord", lambda: RetrievalRecord(
                    query=str(data.get("query") or ""),
                    retrieved_ids=tuple(str(x) for x in
                                        (data.get("retrieved_ids") or ())),
                    scores=tuple(float(x) for x in (data.get("scores") or ()))))
            elif event.type == EV_TOOL_CALL:
                tools.append(_guard(event, "ToolCallRecord", lambda: ToolCallRecord(
                    name=str(data.get("name") or ""),
                    ok=bool(data.get("ok", False)),
                    executed=bool(data.get("executed", True)),
                    error_code=str(data.get("error_code") or ""),
                    latency_ms=float(data.get("latency_ms") or 0.0),
                    arguments=dict(data.get("arguments") or {}),
                    required=CapabilityLevel(int(
                        data.get("required", _level_value(CapabilityLevel.L2_SIDE_EFFECT)))))))
            elif event.type == EV_LLM_CALL:
                llms.append(_guard(event, "LlmCallRecord", lambda: LlmCallRecord(
                    model=str(data.get("model") or ""),
                    prompt_tokens=int(data.get("prompt_tokens") or 0),
                    completion_tokens=int(data.get("completion_tokens") or 0),
                    latency_ms=float(data.get("latency_ms") or 0.0))))
            elif event.type == EV_EXCEPTION:
                breaks.append(_guard(event, "ExceptionRecord", lambda: ExceptionRecord(
                    stage=str(data.get("stage") or ""),
                    error_code=str(data.get("error_code") or ""),
                    message=str(data.get("message") or ""),
                    timeout_ms=(None if data.get("timeout_ms") is None
                                else float(data["timeout_ms"])))))
            elif event.type == EV_CLAIMED_OUTCOME:
                claims.append(_guard(event, "ClaimedOutcome", lambda: ClaimedOutcome(
                    claim=str(data.get("claim") or ""),
                    depends_on=tuple(str(x) for x in
                                     (data.get("depends_on") or ())))))

        # 结束事件在段外（`_segments` 单独给出）：它决定这一跑的**结论**，
        # 不是段内的一条痕迹。没有它 → completed=False，如实反映「没跑完」。
        if end is not None:
            end_data = end.payload or {}
            answer = str(end_data.get("answer") or "")
            completed = bool(end_data.get("completed", True))

        security = None
        if agent_key:
            security = SecurityContext(
                agent_key=agent_key,
                agent_level=CapabilityLevel(int(
                    payload.get("agent_level",
                                _level_value(CapabilityLevel.L0_READ)))),
                role=UserRole(int(payload.get("role",
                                              _role_value(UserRole.MEMBER)))))

        return AgentTrace(
            trace_id=str(payload.get("trace_id") or
                         f"{self.journal.journal_id}#seq{start.seq}"),
            question=question, answer=answer, retrieval=retrieval,
            tool_calls=tuple(tools), llm_calls=tuple(llms),
            exceptions=tuple(breaks), claimed_outcomes=tuple(claims),
            security=security, completed=completed)


def _guard(event: Event, name: str, build: Any) -> Any:
    """把构造期的校验失败包上「哪条事件」的上下文。

    没有这一步，一条坏 payload 只会得到「RetrievalRecord.query 不得为空」，
    排障的人得自己去账本里翻是哪一条——而账本可能有上万行。
    """
    try:
        return build()
    except (TraceError, ValueError, TypeError) as exc:
        raise TraceError(
            f"事件 seq={event.seq}（{event.type}）无法还原成 {name}：{exc}") from exc
