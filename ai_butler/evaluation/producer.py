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

未实现（如实登记）
------------------
事件流只提供「计划要调哪些工具」（`plan` 事件的 tools），**不提供实际的
工具调用记录**。所以本模块构造的痕迹里 `tool_calls` 恒为空，
工具调用真实性维度会判「无从判定」而不是「通过」。
不伪造 tool_calls —— 伪造出来的「工具都调了」会让复评放过真正的工具缺失。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Sequence

from ai_butler.evaluation.platform import EvaluationCase
from ai_butler.evaluation.trace import (
    AgentTrace,
    ExceptionRecord,
    RetrievalRecord,
)

__all__ = ["StreamFactory", "StreamTraceProducer", "COLLECTOR_USER_ID"]

#: 复评重放时使用的系统身份。理由同 `coder.CODER_USER_ID`：
#: 离线链路没有登录用户，不借用真实用户的话务与配额。
COLLECTOR_USER_ID = "system:p10-recheck"

#: `(query) -> AsyncIterator[event]`。event 只要有 `.type` / `.text` / `.data`。
StreamFactory = Callable[[str], AsyncIterator[Any]]


@dataclass
class StreamTraceProducer:
    """把对话管道的事件流折成 `AgentTrace`。

    `runs` 默认 2：稳定性维度**至少需要 2 次**运行才有分母。
    只跑 1 次会让该维度判「无从判定」而非「通过」——这是刻意的，
    因为一次运行看不出稳不稳定。
    """

    stream_factory: StreamFactory
    runs: int = 2

    def __post_init__(self) -> None:
        if self.runs < 1:
            raise ValueError("runs 至少为 1")

    async def produce(self, case: EvaluationCase) -> Sequence[AgentTrace]:
        out: list[AgentTrace] = []
        for index in range(1, self.runs + 1):
            out.append(await self._one(case, index))
        return tuple(out)

    # ------------------------------------------------------------------ #
    async def _one(self, case: EvaluationCase, index: int) -> AgentTrace:
        chunks: list[str] = []
        retrieved: tuple[str, ...] = ()
        exceptions: list[ExceptionRecord] = []
        completed = False

        # 用 case.question 而不是改写后的问题：评测的金标准是对着原问题写的，
        # 拿改写文本去跑，痕迹与用例就对不上了。
        async for event in self.stream_factory(case.question):
            kind = str(getattr(event, "type", "") or "")
            data = getattr(event, "data", None) or {}
            text = str(getattr(event, "text", "") or "")

            if kind == "chunk":
                chunks.append(text)
            elif kind == "error":
                exceptions.append(ExceptionRecord(
                    stage="execution",
                    error_code=str(data.get("code") or "unknown"),
                    message=text))
            elif kind == "done":
                completed = True
                retrieved = tuple(str(x) for x in (data.get("retrieved_ids") or ()))

        return AgentTrace(
            trace_id=f"{case.case_id}#run{index}",
            question=case.question,
            answer="".join(chunks).strip(),
            retrieval=RetrievalRecord(query=case.question, retrieved_ids=retrieved),
            # 见模块顶部「未实现」：事件流不提供实际工具调用，这里不伪造
            tool_calls=(),
            exceptions=tuple(exceptions),
            completed=completed,
            # `category` 放进 claimed_outcomes 之外的字段没有位置，
            # 且它不是「声称结果」，故不留。需要分类结论请读 `done` 事件本身。
            security=None,
        )
