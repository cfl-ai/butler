"""评测中台的过程痕迹数据模型 —— 「管控过程」的地基。

计划书依据
----------
- 三、P9「全维度指标监控看板搭建」「日志沉淀与案例分类」

用户指令（2026-10-23，裁定 U55）
------------------------------
「真正的评测绝非仅靠大模型离线评分，而是需要全面覆盖结果与过程的
七大核心维度……既要管控大模型的最终输出，更要追踪整个 Agent 的
调用过程是否符合预期。」

本模块是**过程侧**的载体：检索、工具、大模型、异常四类痕迹，
外加 Agent 对用户**声称**的结果。没有痕迹，过程管控无从谈起。

设计要点
--------
1. **痕迹与金标准分离**：trace 是系统运行的产物（可能不完整），
   golden（相关素材 id、期望答案）是评测输入，由评测方单独提供
   （见 `evaluation/platform.py` 的 EvaluationCase）。
   两者混在一起，「没采集到」会伪装成「没召回」。
2. **fail-closed**：缺痕迹 = 该维度无从判定 = 不通过。
   「没有记录」不能当成「没有问题」——这是过程管控与结果打分
   最本质的区别。
3. **判责字段前置**：异常记录必须能归属到
   interface（接口故障）/ execution（任务执行超时）/ delivery（结果回传超时）
   并带 error_code；归属不了的异常在维度四按失败计
   （「超时了但不知道谁的」在评测里不可接受）。
4. **声称与事实分离**：ClaimedOutcome 只记 Agent **说了什么**，
   真伪由维度三拿 ToolCallRecord 对账——自称「下单成功」
   不算数，后端记录才算数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_butler.core.permissions import CapabilityLevel, UserRole

#: 异常归属阶段。**穷尽列出**；阶段外的值意味着「无从判责」。
EXCEPTION_STAGES: tuple[str, ...] = ("interface", "execution", "delivery")

#: 阶段的中文名（报告与日志用）
STAGE_TITLES: dict[str, str] = {
    "interface": "接口故障",
    "execution": "任务执行超时",
    "delivery": "结果回传超时",
}


class TraceError(ValueError):
    """痕迹数据不合法。宁可拒收，不带着坏数据评。"""


@dataclass(frozen=True)
class RetrievalRecord:
    """一次检索的痕迹。"""

    query: str
    #: 实际召回的素材 id（有序，保留检索器的排序）
    retrieved_ids: tuple[str, ...] = ()
    #: 各命中项的分数（与 retrieved_ids 等长；不强制提供）
    scores: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise TraceError("RetrievalRecord.query 不得为空")
        if self.scores and len(self.scores) != len(self.retrieved_ids):
            raise TraceError(
                f"scores 长度 {len(self.scores)} 与 retrieved_ids "
                f"长度 {len(self.retrieved_ids)} 不一致 —— 对不上号的分数会误导分析")


@dataclass(frozen=True)
class ToolCallRecord:
    """一次工具调用的痕迹。**后端视角，不是大模型的视角。**"""

    name: str
    #: 后端是否真的成功（大模型说成功不算）
    ok: bool = False
    #: 调用是否真的到达了后端。被权限闸拦下、被审批挂起都为 False——
    #: 没执行却声称成功，正是维度三要抓的「虚构」。
    executed: bool = True
    #: 失败时的错误码（维度四判责用；成功时留空）
    error_code: str = ""
    latency_ms: float = 0.0
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: 本调用所需的能力级（维度六据此独立复判权限）。
    #: 未声明按 L2（调外部工具）对待——与 scheduler.tools.ToolSpec.required 同口径。
    required: CapabilityLevel = CapabilityLevel.L2_SIDE_EFFECT

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise TraceError("ToolCallRecord.name 不得为空")
        if self.latency_ms < 0:
            raise TraceError("ToolCallRecord.latency_ms 不得为负")


@dataclass(frozen=True)
class LlmCallRecord:
    """一次大模型调用的痕迹（维度五计量用）。"""

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __post_init__(self) -> None:
        if min(self.prompt_tokens, self.completion_tokens) < 0:
            raise TraceError("token 计数不得为负 —— 负数会让成本指标失真")
        if self.latency_ms < 0:
            raise TraceError("LlmCallRecord.latency_ms 不得为负")


@dataclass(frozen=True)
class ExceptionRecord:
    """一次异常的痕迹。stage + error_code 是判责的最小充分信息。"""

    #: 归属阶段：interface / execution / delivery（见 EXCEPTION_STAGES）
    stage: str
    error_code: str = ""
    message: str = ""
    #: 超时时长（毫秒）。仅超时类异常填。
    timeout_ms: float | None = None

    @property
    def attributable(self) -> bool:
        """是否可判责：阶段合法且带错误码。"""
        return self.stage in EXCEPTION_STAGES and bool(self.error_code)


@dataclass(frozen=True)
class ClaimedOutcome:
    """Agent 对用户**声称**的结果，如「下单成功」。

    它只是一个待核实的主张；真伪由维度三拿工具调用记录对账。
    """

    claim: str
    #: 支撑该声称所依赖的工具名
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise TraceError("ClaimedOutcome.claim 不得为空")


@dataclass(frozen=True)
class SecurityContext:
    """被评测 Agent 的身份与角色（维度六据此**独立复判**权限）。

    不复用运行时的结论：评测方要自己判一遍，才能发现
    「运行时放行了但本不该放行」的越权（U43–U45 / U51）。
    """

    agent_key: str
    agent_level: CapabilityLevel = CapabilityLevel.L0_READ
    role: UserRole = UserRole.MEMBER


@dataclass
class AgentTrace:
    """一次 Agent 运行的完整痕迹 + 结果。

    同一问题的多次运行构成一个**运行组**（稳定性评测的分母），
    由 `evaluation/platform.py` 按 question 分组后送入各维度。
    """

    trace_id: str
    question: str
    answer: str
    retrieval: RetrievalRecord | None = None
    tool_calls: tuple[ToolCallRecord, ...] = ()
    llm_calls: tuple[LlmCallRecord, ...] = ()
    exceptions: tuple[ExceptionRecord, ...] = ()
    claimed_outcomes: tuple[ClaimedOutcome, ...] = ()
    security: SecurityContext | None = None
    #: 本次运行是否正常结束。False 表示带着未处理异常终止，
    #: 稳定性评测把这样的运行计入「结果不一致」。
    completed: bool = True

    def __post_init__(self) -> None:
        if not self.trace_id.strip():
            raise TraceError("AgentTrace.trace_id 不得为空")
        if not self.question.strip():
            raise TraceError("AgentTrace.question 不得为空")


# --------------------------------------------------------------------------- #
# 反序列化：State 里只有可 JSON 化的 dict，这里显式转回痕迹对象。
# 显式而不 magic：字段名对不上时立刻报错，而不是静默造出一条缺字段的痕迹
# （缺字段的痕迹会让维度「无从判定」——那是评测的失败，不是输入的失败）。
# --------------------------------------------------------------------------- #

def _as_str(value: Any, *, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TraceError(f"{where} 应为字符串，实际 {type(value).__name__}")
    return value


def _as_bool(value: Any, *, where: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TraceError(f"{where} 应为布尔值，实际 {type(value).__name__}")
    return value


def _as_float(value: Any, *, where: str, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraceError(f"{where} 应为数值，实际 {type(value).__name__}")
    return float(value)


def _as_int(value: Any, *, where: str, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceError(f"{where} 应为整数，实际 {type(value).__name__}")
    return value


def _as_tuple_of_str(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TraceError(f"{where} 应为列表，实际 {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        out.append(_as_str(item, where=f"{where}[{i}]"))
    return tuple(out)


def trace_to_dict(trace: AgentTrace) -> dict[str, Any]:
    """`AgentTrace` → dict（State 形态）。**与 `trace_from_dict` 严格对称。**

    这个方向此前是缺的：只有反序列化，没有序列化。后果不是「少一个工具函数」，
    而是任何一个真实生产端（`StreamTraceProducer` / `JournalTraceSource`）
    产出的 `AgentTrace` **进不了 State**——首轮痕迹只能由调用方手工拼 dict，
    于是「痕迹有生产者」和「评测读得到痕迹」两句话同时成立却接不上。

    对称性由 `test_trace_dict_roundtrip` 守着：字段改名时两边一起红。
    """
    if not isinstance(trace, AgentTrace):
        raise TraceError(f"trace_to_dict 只接 AgentTrace，实际 {type(trace).__name__}")
    return {
        "trace_id": trace.trace_id,
        "question": trace.question,
        "answer": trace.answer,
        "retrieval": (None if trace.retrieval is None else {
            "query": trace.retrieval.query,
            "retrieved_ids": list(trace.retrieval.retrieved_ids),
            "scores": list(trace.retrieval.scores)}),
        "tool_calls": [{
            "name": call.name, "ok": bool(call.ok),
            "executed": bool(call.executed), "error_code": call.error_code,
            "latency_ms": float(call.latency_ms), "arguments": dict(call.arguments),
            "required": int(getattr(call.required, "value", call.required)),
        } for call in trace.tool_calls],
        "llm_calls": [{
            "model": call.model, "prompt_tokens": int(call.prompt_tokens),
            "completion_tokens": int(call.completion_tokens),
            "latency_ms": float(call.latency_ms),
        } for call in trace.llm_calls],
        "exceptions": [{
            "stage": broken.stage, "error_code": broken.error_code,
            "message": broken.message, "timeout_ms": broken.timeout_ms,
        } for broken in trace.exceptions],
        "claimed_outcomes": [{
            "claim": claim.claim, "depends_on": list(claim.depends_on),
        } for claim in trace.claimed_outcomes],
        "security": (None if trace.security is None else {
            "agent_key": trace.security.agent_key,
            "agent_level": int(getattr(trace.security.agent_level, "value",
                                       trace.security.agent_level)),
            "role": int(getattr(trace.security.role, "value",
                                trace.security.role)),
        }),
        "completed": bool(trace.completed),
    }


def trace_from_dict(payload: Mapping[str, Any]) -> AgentTrace:
    """dict → AgentTrace。缺 trace_id/question 直接拒收。"""
    if not isinstance(payload, Mapping):
        raise TraceError(f"trace 应为 dict，实际 {type(payload).__name__}")

    trace_id = _as_str(payload.get("trace_id"), where="trace_id").strip()
    question = _as_str(payload.get("question"), where="question").strip()
    if not trace_id:
        raise TraceError("trace 缺少 trace_id")
    if not question:
        raise TraceError(f"trace {trace_id} 缺少 question")

    retrieval = None
    raw_retrieval = payload.get("retrieval")
    if raw_retrieval is not None:
        if not isinstance(raw_retrieval, Mapping):
            raise TraceError(f"trace {trace_id} 的 retrieval 应为 dict")
        scores = tuple(_as_float(s, where="retrieval.scores")
                       for s in (raw_retrieval.get("scores") or ()))
        retrieval = RetrievalRecord(
            query=_as_str(raw_retrieval.get("query"), where="retrieval.query"),
            retrieved_ids=_as_tuple_of_str(raw_retrieval.get("retrieved_ids"),
                                           where="retrieval.retrieved_ids"),
            scores=scores)

    tool_calls: list[ToolCallRecord] = []
    for i, raw in enumerate(payload.get("tool_calls") or ()):
        if not isinstance(raw, Mapping):
            raise TraceError(f"trace {trace_id} 的 tool_calls[{i}] 应为 dict")
        required = raw.get("required")
        tool_calls.append(ToolCallRecord(
            name=_as_str(raw.get("name"), where=f"tool_calls[{i}].name"),
            ok=_as_bool(raw.get("ok"), where=f"tool_calls[{i}].ok"),
            executed=_as_bool(raw.get("executed"), where=f"tool_calls[{i}].executed",
                              default=True),
            error_code=_as_str(raw.get("error_code"), where=f"tool_calls[{i}].error_code"),
            latency_ms=_as_float(raw.get("latency_ms"), where=f"tool_calls[{i}].latency_ms"),
            arguments=dict(raw.get("arguments") or {}),
            required=(CapabilityLevel(_as_int(required, where=f"tool_calls[{i}].required",
                                              default=int(CapabilityLevel.L2_SIDE_EFFECT)))
                      if required is not None else CapabilityLevel.L2_SIDE_EFFECT)))

    llm_calls: list[LlmCallRecord] = []
    for i, raw in enumerate(payload.get("llm_calls") or ()):
        if not isinstance(raw, Mapping):
            raise TraceError(f"trace {trace_id} 的 llm_calls[{i}] 应为 dict")
        llm_calls.append(LlmCallRecord(
            model=_as_str(raw.get("model"), where=f"llm_calls[{i}].model"),
            prompt_tokens=_as_int(raw.get("prompt_tokens"),
                                  where=f"llm_calls[{i}].prompt_tokens"),
            completion_tokens=_as_int(raw.get("completion_tokens"),
                                      where=f"llm_calls[{i}].completion_tokens"),
            latency_ms=_as_float(raw.get("latency_ms"), where=f"llm_calls[{i}].latency_ms")))

    exceptions: list[ExceptionRecord] = []
    for i, raw in enumerate(payload.get("exceptions") or ()):
        if not isinstance(raw, Mapping):
            raise TraceError(f"trace {trace_id} 的 exceptions[{i}] 应为 dict")
        timeout = raw.get("timeout_ms")
        exceptions.append(ExceptionRecord(
            stage=_as_str(raw.get("stage"), where=f"exceptions[{i}].stage"),
            error_code=_as_str(raw.get("error_code"), where=f"exceptions[{i}].error_code"),
            message=_as_str(raw.get("message"), where=f"exceptions[{i}].message"),
            timeout_ms=(_as_float(timeout, where=f"exceptions[{i}].timeout_ms")
                        if timeout is not None else None)))

    claimed: list[ClaimedOutcome] = []
    for i, raw in enumerate(payload.get("claimed_outcomes") or ()):
        if not isinstance(raw, Mapping):
            raise TraceError(f"trace {trace_id} 的 claimed_outcomes[{i}] 应为 dict")
        claimed.append(ClaimedOutcome(
            claim=_as_str(raw.get("claim"), where=f"claimed_outcomes[{i}].claim"),
            depends_on=_as_tuple_of_str(raw.get("depends_on"),
                                        where=f"claimed_outcomes[{i}].depends_on")))

    security = None
    raw_security = payload.get("security")
    if raw_security is not None:
        if not isinstance(raw_security, Mapping):
            raise TraceError(f"trace {trace_id} 的 security 应为 dict")
        level = raw_security.get("agent_level")
        role = raw_security.get("role")
        security = SecurityContext(
            agent_key=_as_str(raw_security.get("agent_key"), where="security.agent_key"),
            agent_level=(CapabilityLevel(
                _as_int(level, where="security.agent_level",
                        default=int(CapabilityLevel.L0_READ)))
                if level is not None else CapabilityLevel.L0_READ),
            role=(UserRole(_as_int(role, where="security.role",
                                   default=int(UserRole.MEMBER)))
                  if role is not None else UserRole.MEMBER))

    return AgentTrace(
        trace_id=trace_id, question=question,
        answer=_as_str(payload.get("answer"), where="answer"),
        retrieval=retrieval, tool_calls=tuple(tool_calls), llm_calls=tuple(llm_calls),
        exceptions=tuple(exceptions), claimed_outcomes=tuple(claimed),
        security=security,
        completed=_as_bool(payload.get("completed"), where="completed", default=True))


def traces_from_dicts(payloads: Sequence[Mapping[str, Any]]) -> tuple[AgentTrace, ...]:
    """批量反序列化。任一条不合法整体拒收——半套痕迹比没有更危险。"""
    return tuple(trace_from_dict(p) for p in payloads)
