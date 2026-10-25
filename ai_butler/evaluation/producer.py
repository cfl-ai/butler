"""痕迹生产端 —— 为复评重新产出运行痕迹（`TraceProducer` 的真实实现）。

计划书依据
----------
- 三、P9「评测中台对全链路做客观评价」——要评就得有痕迹
- 三、P10 离线链路「评测迭代效果，**未达标持续循环修复**」——
  「再评一轮」必须有新痕迹，复读旧痕迹等于什么都没验
- U57（append-only 事件账本）：`evaluation/trace.py` 的五类痕迹
  与账本事件一一对应，即 P9 的数据源契约

为什么这一层不能省
------------------
`recheck.Rechecker` 没有 producer 时只能返回 `UNVERIFIED`。
如果一直不实现 producer，那条「复评」分支就永远不会被走到——
即 E-77 的第四种形态：**接口建好了，但没有任何实现能进去**。
本模块补的就是这个实现。

为什么不直接 import `retrieval.chat`
------------------------------------
`evaluation` 是**纯消费方**（只吃痕迹，不认识生产方）。让它反向依赖
retrieval 会把「评测」和「被测系统」焊在一起，之后换管道就得改评测代码。
这里改成接收 `stream_factory`——鸭子类型的事件流，谁产出都行。

两句「接上真实管道才知道」的话
------------------------------
**一、`revise` 是替换，不是续写。** 真实管道在复检推翻初稿时会发一条
`revise`（`retrieval/chat.py` 的弃答分支），那条事件带的是**用户真正
看到的那一版**。早先这里只认 `chunk`，一旦接上真实管道，痕迹里的
`answer` 会变成「初稿 + 弃答」拼起来的、谁也没说过的一段话——
用它去算输出质量与证据忠实度，两边都是错的。

**二、补丁落盘后必须重建管道。** 见 `TraceProducer` 的契约（E-83）：
模块对象是进程内缓存。`before_run` 就是这件事的落点，
由装配层提供（`agents/repair_loop.py` 的 `PipelineHolder` 会重载被测模块
并重建 orchestrator）。它挂在 `produce()` 开头而不是 `_one()` 里：
**一个用例的重跑组只该重建一次**——每跑一次就 reload 一遍模块，
既慢又会让 reload 的副作用次数取决于 `runs`（不可解释）。

未实现（如实登记）
------------------
事件流只提供「计划要调哪些工具」（`plan` 事件的 tools），**不提供实际的
工具调用记录**。所以本模块构造的痕迹里 `tool_calls` 恒为空，
工具调用真实性维度会判「无从判定」而不是「通过」。
不伪造 tool_calls —— 伪造出来的「工具都调了」会让复评放过真正的工具缺失。

同理 `llm_calls` 也为空：事件流不吐 token 与耗时，成本维度只能「无从判定」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Sequence

from ai_butler.evaluation.platform import EvaluationCase
from ai_butler.evaluation.trace import (
    AgentTrace,
    ExceptionRecord,
    RetrievalRecord,
)

__all__ = ["StreamFactory", "StreamTraceProducer", "COLLECTOR_USER_ID",
           "trace_from_events"]

#: 复评重放时使用的系统身份。理由同 `coder.CODER_USER_ID`：
#: 离线链路没有登录用户，不借用真实用户的话务与配额。
COLLECTOR_USER_ID = "system:p10-recheck"

#: `(query) -> AsyncIterator[event]`。event 只要有 `.type` / `.text` / `.data`。
StreamFactory = Callable[[str], AsyncIterator[Any]]


def trace_from_events(*, trace_id: str, question: str,
                      events: Sequence[Any]) -> AgentTrace:
    """把一次对话的事件流折成 `AgentTrace`。

    **复评现跑与在线埋点共用这一个折叠函数**（U91）：两处各折一遍，
    必然在两件最早被发现的事上分叉——`revise` 是替换还是续写
    （真实管道弃答时用户看到的是弃答那一版）、`done` 缺失算不算跑完。
    分叉的方向还都是「看起来正常」：折出来的痕迹能评，只是评的不是
    用户真正看到的那一版。

    事件流只提供「计划要调哪些工具」（`plan` 事件的 tools），不提供实际的
    工具调用记录，所以 `tool_calls` 恒为空、`llm_calls` 恒为空：
    工具调用真实性与成本两个维度判「无从判定」而不是「通过」。
    **不伪造**——伪造出来的「工具都调了」会让复评放过真正的工具缺失。
    """
    chunks: list[str] = []
    revised: str | None = None
    retrieved: tuple[str, ...] = ()
    exceptions: list[ExceptionRecord] = []
    completed = False

    for event in events:
        kind = str(getattr(event, "type", "") or "")
        data = getattr(event, "data", None) or {}
        text = str(getattr(event, "text", "") or "")

        if kind == "chunk":
            chunks.append(text)
        elif kind == "revise":
            # 复检推翻了初稿：这是**替换**，不是续写（见模块顶部「一」）
            revised = text
        elif kind == "error":
            exceptions.append(ExceptionRecord(
                stage="execution",
                error_code=str(data.get("code") or "unknown"),
                message=text))
        elif kind == "done":
            completed = True
            retrieved = tuple(str(x) for x in (data.get("retrieved_ids") or ()))

    return AgentTrace(
        trace_id=trace_id,
        question=question,
        answer=(revised if revised is not None else "".join(chunks)).strip(),
        retrieval=RetrievalRecord(query=question, retrieved_ids=retrieved),
        tool_calls=(),
        exceptions=tuple(exceptions),
        completed=completed,
        # `category` 放进 claimed_outcomes 之外的字段没有位置，
        # 且它不是「声称结果」，故不留。需要分类结论请读 `done` 事件本身。
        security=None,
    )


@dataclass
class StreamTraceProducer:
    """把对话管道的事件流折成 `AgentTrace`。

    `runs` 默认 2：稳定性维度**至少需要 2 次**运行才有分母。
    只跑 1 次会让该维度判「无从判定」而非「通过」——这是刻意的，
    因为一次运行看不出稳不稳定。
    """

    stream_factory: StreamFactory
    runs: int = 2
    #: 每次 `produce()` 开头调用一次：**重载被测模块 / 重建管道**的落点（E-83）。
    #: 不提供就沿用当前进程里的模块——补丁刚落盘时那评的是旧代码。
    #: 抛异常是允许的：复评会把该用例记为 errored 并因此判 UNVERIFIED，
    #: 这比交出一份基于旧代码的「没修好」诚实得多。
    before_run: Callable[[], None] | None = None
    #: 实际重建了几次（观测用）。`0` 表示整轮都没重建过——
    #: 看到 `rejected_no_effect` 时先看这个数，别急着怀疑补丁。
    refreshes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError("runs 至少为 1")

    async def produce(self, case: EvaluationCase) -> Sequence[AgentTrace]:
        if self.before_run is not None:
            self.before_run()
            self.refreshes += 1
        out: list[AgentTrace] = []
        for index in range(1, self.runs + 1):
            out.append(await self._one(case, index))
        return tuple(out)

    # ------------------------------------------------------------------ #
    async def _one(self, case: EvaluationCase, index: int) -> AgentTrace:
        # 用 case.question 而不是改写后的问题：评测的金标准是对着原问题写的，
        # 拿改写文本去跑，痕迹与用例就对不上了。
        events = [event async for event in self.stream_factory(case.question)]
        return trace_from_events(
            trace_id=f"{case.case_id}#run{index}",
            question=case.question, events=events)
