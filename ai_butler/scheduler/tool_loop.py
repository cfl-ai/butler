"""Function Calling 调用回路（U112）。

为什么单独一个模块
------------------
模型侧的工具调用是**循环**，不是一次函数调用：模型说「我要调 X」→ 系统执行 →
把结果还给模型 → 模型再决定要不要继续。把这段写在业务代码里，会让「工具该由谁执行、
失败怎么降级」散落到各个调用点 —— 正是 E-86「有实现没有装配」的形态。

所以收成一个可装配、可断言的回路，并且**默认不走**（`function_calling_enable` 关闭）。

关键不变量（为什么它比"直接让模型调 API"安全）
--------------------------------------------
模型发起的工具调用，**必须**走同一个 `ToolDispatcher`：

- U43–U45 三层权限求值；
- U59 审批凭据（付款类动作不放行就是不放行，模型不能自己"确认"）；
- 重复调用拦截、无增量终止；
- U60 协作式超时。

**function calling 不是绕过闸门的后门** —— 它只是换了个"谁来发起"。
模型能决定的只有「调什么、传什么参」，决定不了「有没有权限」。

2026 的技术背景（为什么不自己发明协议）
--------------------------------------
工具声明与调用信封遵循 OpenAI Chat Completions `tools` 的惯例，它与 MCP 的工具
声明同源，已被各厂与主流框架采纳。据此：换模型、换供应商都不用改这一层 ——
「大模型可更换」落在**协议**上，而不是落在代码分支上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_butler.core.config import Settings, get_settings
from ai_butler.infra.llm_gateway import ChatMessage, LLMGateway, ToolUnsupportedError
from ai_butler.scheduler.tools import ToolCall, ToolDispatcher, ToolResult

#: 单次工具结果回灌的字符上限。工具输出可能极长（例如整篇素材页），
#: 原样回灌会把上下文挤爆 —— 截断必须**有标记**，不能悄悄切掉。
TOOL_RESULT_MAX_CHARS = 2000


@dataclass
class ToolLoopResult:
    text: str
    #: 实际执行了几轮模型→工具→模型
    rounds: int = 0
    tool_results: tuple[ToolResult, ...] = ()
    #: 降级痕迹。空不代表顺利，只是没有可说的。
    degradation: tuple[str, ...] = ()
    #: 是否撞到 max_tool_rounds 上限。True 意味着**未收敛**，必须如实告知，
    #: 不能当成正常完成。
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.truncated


class FunctionCallLoop:
    """模型↔工具的有限轮次回路。

    关闭开关时**不应被构造到生产链路里**：ADP 既有路径仍是 Python 直调工具，
    这里只是同一套 ToolDispatcher 的第二个发起方。
    """

    def __init__(self, gateway: LLMGateway, dispatcher: ToolDispatcher, *,
                 settings: Settings | None = None,
                 max_rounds: int | None = None) -> None:
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.settings = settings or get_settings()
        self.max_rounds = max_rounds if max_rounds is not None else self.settings.max_tool_rounds

    async def run(self, *, user_id: str, messages: Sequence[ChatMessage],
                  tools: Sequence[Mapping[str, Any]], **chat_kwargs: Any) -> ToolLoopResult:
        """跑到「模型不再要求调工具」或「轮次用尽」为止。"""
        convo = list(messages)
        results: list[ToolResult] = []
        notes: list[str] = []

        for round_index in range(1, self.max_rounds + 1):
            reply = await self.gateway.chat(user_id=user_id, messages=convo,
                                            tools=tools, **chat_kwargs)
            if not reply.tool_calls:
                return ToolLoopResult(text=reply.text, rounds=round_index,
                                      tool_results=tuple(results),
                                      degradation=tuple(notes), truncated=False)

            for call in reply.tool_calls:
                # **模型是不可信调用方**：它会叫错工具名、会传错参数 —— 这不是异常，
                # 是常态。这类错误必须「记下来 + 告诉模型」，而不是让整条回路崩掉。
                # 守卫上限 / 权限拒绝由 dispatcher 以 ToolResult 表达，
                # 而"工具不存在"这类结构化异常在 dispatch 里是直接抛出，
                # 这里把它降成一次失败的工具结果。
                try:
                    result = self.dispatcher.dispatch(
                        ToolCall(name=call.name, payload=dict(call.arguments)),
                        user_id=user_id)
                except Exception as exc:                # noqa: BLE001 — 单工具失败不终止回路
                    notes.append(
                        f"工具 {call.name} 调用失败：{type(exc).__name__}: {exc}")
                    convo.append(ChatMessage(
                        role="user",
                        content=f"[工具调用结果] {call.name}：调用失败"
                                f"（{type(exc).__name__}: {exc}）—— 请换一种方式或改调其他工具"))
                    continue
                results.append(result)
                if not result.ok:
                    # 工具失败的**原因**要回到模型视野里，否则模型会以为调用成功继续编。
                    notes.append(f"工具 {call.name} 未成功：{result.note or result.error_code}")
                convo.append(ChatMessage(
                    role="user", content=self._render(call.name, result)))

            if notes and round_index == self.max_rounds:
                break

        return ToolLoopResult(
            text="", rounds=self.max_rounds, tool_results=tuple(results),
            degradation=tuple(notes), truncated=True)

    @staticmethod
    def _render(name: str, result: ToolResult) -> str:
        """把工具结果渲染成模型可读的文本消息。

        **没用 `role="tool"`**：项目的 `ChatMessage` 只定义了三种角色，
        为它敞开一个新角色要动三个供应商适配器 + 所有消息构造点，收益不抵风险。
        用 user 消息承载并在文本里点明出处，模型能读懂，链路也不用改。

        被审批拦下（U59）时要把票号一起给模型，否则它只会再调一次然后再次碰壁。
        """
        if not result.ok:
            extra = f"，审批票号 {result.approval_id}" if result.approval_id else ""
            return (f"[工具调用结果] {name}：失败"
                    f"（{result.error_code or result.note or '未给出原因'}{extra}）")
        extra = "（重复调用，复用上次结果）" if result.duplicate else ""
        payload = _stringify(result.output)
        clipped = payload[:TOOL_RESULT_MAX_CHARS]
        tail = "…（结果过长已截断）" if len(payload) > TOOL_RESULT_MAX_CHARS else ""
        return f"[工具调用结果] {name}{extra}：{clipped}{tail}"


def mention_tools_available(tools: Sequence[Mapping[str, Any]]) -> str | None:
    """给系统提示用的一句话：本次可向模型开放哪些工具。

    返回 None 表示**没有**可暴露的工具（都不带 params_schema）—— 此时不应在下发的
    提示里暗示"你可以调工具"，否则模型会凭空调用一个不存在的名字。
    """
    if not tools:
        return None
    names = [str(t.get("function", {}).get("name", "")).strip() for t in tools]
    names = [n for n in names if n]
    return "可用工具：" + "、".join(names) if names else None


#: 回路系统提示（U119）。**这决定了模型会不会用对工具**。
#: 只写「可用工具：A、B」时，模型遇到「明天会下雨吗」会判定天气工具查不到预报，
#: 转去调素材检索，最后答「我没有实时天气能力」—— 实测如此（能力有，但没被选中，
#: 等于没有，E-108 同族）。
_LOOP_SYSTEM = (
    "你是「AI 管家」的助手，可以调用工具取真实数据后再回答。\n"
    "选工具（按重要性排序）：\n"
    "1. 问天气（今天/明天/这周、下不下雨、多少度）→ 调 get_weather；"
    "问地点、商户、景区、在哪 → 调 search_poi；这两类**不要**用检索工具代替。\n"
    "2. 问内部资料、制度、流程、标准、条款 → 调 search_materials。\n"
    "3. 需要读某个知识库页面 → 调 read_wiki_page。\n"
    "4. 一个工具没查到，换另一个可能合适的工具再试一次，不要只调一个就说没有。\n"
    "回答要求：\n"
    "- 只用工具返回的数据作答；工具没有的信息就说没有，不要猜。\n"
    "- **纯文本**：不要用 Markdown，不要出现 *、#、-、|、加粗、表格、分点符号；"
    "需要分句就用短句或分号。\n"
    "- 中文，简洁直接，不超过 200 字。\n"
    "- 说明数据是哪来的（例如「据 Open-Meteo 数据」），不要编造出处。"
)


def loop_system_prompt(tools: Sequence[Mapping[str, Any]]) -> str | None:
    """拼一次回路的系统提示。没有可暴露的工具时返回 None（不下发空提示）。"""
    hint = mention_tools_available(tools)
    if hint is None:
        return None
    return _LOOP_SYSTEM + "\n" + hint



def _stringify(value: Any) -> str:
    import json

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


#: 导出：ToolUnsupportedError 从这个模块也能拿到，避免调用方去网关里翻
__all__ = [
    "FunctionCallLoop",
    "TOOL_RESULT_MAX_CHARS",
    "ToolLoopResult",
    "ToolUnsupportedError",
    "loop_system_prompt",
    "mention_tools_available",
]
