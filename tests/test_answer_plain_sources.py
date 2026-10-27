"""答案纯文本化 + 数据来源 + 工具调用明细（U119）。

守三件事：
1. **纯文本**：`*`、`|`、行首 `-` 不进答案；行中的连字符（`20-30`、`B-52`）必须保留 ——
   一刀切会把正文也吃掉；
2. **来源**：`done.sources` 真的带出来，且「跑了但 0 命中」标成未命中（不能冒充依据）；
3. **过程可见**：每次工具调用（工具名/入参/成败/结果摘要）都进报告，不是只报次数。
"""

from __future__ import annotations

import asyncio

from ai_butler.retrieval.chat import (
    ChatOrchestrator,
    PlainText,
    _retrieval_sources,
    _tool_sources,
    plain_text,
)
from ai_butler.scheduler.chat_intake import ToolAnswer
from ai_butler.scheduler.intake import (
    ASSIGNMENTS,
    IntakeReport,
    SlotKind,
    UnitOutcome,
    assign,
    discretize,
)
from ai_butler.retrieval.planner import QueryCategory
from ai_butler.scheduler.tools import ToolCall, ToolResult


MARKDOWN_ANSWER = """杭州明天天气如下：

- **天气**：多云
- **气温**：12-21 度

| 项目 | 值 |
|---|---|
| 降水 | 0% |

## 结论
B-52 这类编号要保留。"""


class _Gateway:
    def __init__(self, pieces: tuple[str, ...] = ("答案",)) -> None:
        self.pieces = pieces

    async def stream(self, *, user_id, messages, **kwargs):
        for piece in self.pieces:
            yield piece


def _collect(orchestrator, *, query="杭州明天天气"):
    async def run():
        return [e async for e in orchestrator.stream(user_id="u-1", query=query)]
    return asyncio.run(run())


# ------------------------------ 1. 清洗器 ------------------------------ #
def test_plain_text_drops_markdown_but_keeps_real_hyphens():
    text = plain_text(MARKDOWN_ANSWER)
    for marker in ("*", "|", "#"):
        assert marker not in text, marker
    assert "|---" not in text and "---" not in text, "表格分隔行必须整行丢掉"
    assert "天气：多云" in text, "列表符去掉后内容要留下"
    assert "12-21 度" in text, "行中的连字符是正文，不许删"
    assert "B-52" in text, "编号里的连字符同样要保留"


def test_plain_text_is_safe_on_every_chunk_boundary():
    """流式切片把 `**` 拆开时也不能漏出半个标记（成对匹配在这里会翻车）。"""
    whole = plain_text(MARKDOWN_ANSWER)
    for size in (1, 2, 3, 5, 7, 11):
        cleaner = PlainText()
        out: list[str] = []
        for index in range(0, len(MARKDOWN_ANSWER), size):
            out.append(cleaner.feed(MARKDOWN_ANSWER[index:index + size]))
        out.append(cleaner.flush())
        assert "".join(out) == whole, f"chunk={size} 与一次性清洗结果不一致"


# ------------------------------ 2. 来源 ------------------------------ #
def test_tool_sources_skip_failed_calls_and_flag_empty_hits():
    payload = {"units": [
        {"calls": [
            {"tool": "get_weather", "ok": True, "empty": False},
            {"tool": "search_materials", "ok": True, "empty": True},
            {"tool": "search_poi", "ok": False, "empty": False},
        ]},
    ]}
    labels = [item["label"] for item in _tool_sources(payload)]
    assert "Open-Meteo 气象数据" in labels
    assert any("未命中" in label for label in labels), "0 命中必须标注，不能冒充依据"
    assert not any("高德" in label for label in labels), "失败的调用不是来源"


def test_retrieval_sources_report_hits_and_wiki_claims():
    hits = [{"source_id": "doc-1", "evidence_ids": ["e1"]},
            {"source_id": "doc-2", "evidence_ids": ["e2"]}]
    sources = _retrieval_sources(hits, {"claims": 3})
    text = " ".join(item["label"] for item in sources)
    assert "2 条" in text and "doc-1" in text
    assert "3 条" in text
    assert _retrieval_sources([], {"claims": 0}) == [], "没有依据就别报来源"


# ------------------------------ 3. 走完整编排 ------------------------------ #
def _report_with_calls(answer: str) -> IntakeReport:
    calls = (
        ToolResult(call=ToolCall(name="get_weather",
                                 payload={"city": "杭州", "days": 2}),
                   ok=True, output={"city": "杭州", "temperature_c": 20.0,
                                    "forecast": [{"date": "2026-10-28"}],
                                    "count": 1}),
        ToolResult(call=ToolCall(name="search_materials", payload={"query": "杭州"}),
                   ok=True, output={"query": "杭州", "count": 0, "hits": []}),
    )
    result = type("_R", (), {"text": answer, "rounds": 2, "tool_results": calls,
                             "degradation": (), "truncated": False})()
    unit = discretize("杭州明天天气")[0]
    outcome = UnitOutcome(unit=unit, result=result)
    return IntakeReport(outcomes=(outcome,))


def test_tool_branch_answer_is_plain_and_carries_sources(settings):
    report = _report_with_calls(MARKDOWN_ANSWER)

    async def tool_answer(**kwargs):
        return ToolAnswer(report)

    orchestrator = ChatOrchestrator(_Gateway(), search=None, verify_answer=False,
                                    tool_answer=tool_answer)
    events = _collect(orchestrator)

    chunk = next(e for e in events if e.type == "chunk")
    assert "*" not in chunk.text and "|" not in chunk.text
    assert "天气：多云" in chunk.text

    done = events[-1]
    assert done.type == "done"
    labels = [item["label"] for item in done.data["sources"]]
    assert "Open-Meteo 气象数据" in labels
    assert any("未命中" in label for label in labels)


def test_report_exposes_every_tool_call_with_args_and_result():
    """过程可见的前提是**每次调用都有明细**：只报「调了 2 次」等于看不见过程。"""
    data = _report_with_calls("答案").as_dict()
    calls = data["units"][0]["calls"]
    assert len(calls) == 2
    first = calls[0]
    assert first["tool"] == "get_weather"
    assert "杭州" in first["args"] and "days" in first["args"]
    assert first["ok"] is True and first["empty"] is False
    assert "temperature" in first["result"] or "20.0" in first["result"]
    second = calls[1]
    assert second["tool"] == "search_materials"
    assert second["empty"] is True, "count=0 必须被识别成「未命中」"


def test_regular_branch_chunks_are_clean(settings):
    gateway = _Gateway(("**结论**：", "杭州明天", "\n- 多云"))
    orchestrator = ChatOrchestrator(gateway, search=None, verify_answer=False)
    events = _collect(orchestrator)
    text = "".join(e.text for e in events if e.type == "chunk")
    assert "*" not in text
    assert "结论" in text and "多云" in text
    assert events[-1].data["sources"] == [], "没有检索命中时不应编一个来源"


# ------------------------------ 4. 工具清单不能被裁掉 ------------------------------ #
def test_unknown_assignment_keeps_external_read():
    """「杭州明天会下雨吗」判成 unknown/none 时，天气工具必须在可见清单里。

    实测踩过：这一格只给 `CAP_RETRIEVAL`，模型看见的清单里没有 get_weather，
    于是如实回答「我没有天气查询工具」—— 工具注册着，但被裁剪掉了。
    """
    entry = ASSIGNMENTS[(QueryCategory.UNKNOWN.value, SlotKind.NONE.value)]
    assert "external_read" in entry.capabilities

    unit = discretize("杭州明天会下雨吗")[0]
    assert "external_read" in assign(unit).capabilities
