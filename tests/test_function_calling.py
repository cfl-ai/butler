"""U112 Function Calling 的守护测试。

守四件事：
1. **契约导出**：只有显式写了 params_schema 的工具才对模型可见；
2. **能力协商**：不支持工具的模型必须报 ToolUnsupportedError，而不是笼统的
   「供应商都不可用」—— 本机 qwen2.5vl:3b 踩过的就是这个坑；
3. **解析健壮**：脏数据（半截 JSON、缺 name、非 dict 参数）一律丢弃，不进执行层；
4. **回路护栏**：模型发起的调用走同一个 ToolDispatcher，撞轮次上限要如实标 truncated。
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agents.capabilities import CAP_RETRIEVAL
from ai_butler.core.config import Settings
from ai_butler.core.permissions import AgentIdentity, CapabilityLevel, ToolPermission, UserRole
from ai_butler.infra.llm_gateway import (
    ChatMessage,
    ChatResult,
    ModelToolCall,
    ToolUnsupportedError,
    _parse_ollama_tool_calls,
    parse_openai_tool_calls,
)
from ai_butler.scheduler.tool_loop import (
    FunctionCallLoop,
    ToolLoopResult,
    mention_tools_available,
)
from ai_butler.scheduler.tools import (
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
    ToolSpec,
)


# --------------------------------------------------------------------------- #
# 1. 契约导出
# --------------------------------------------------------------------------- #
_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    # capability 是 U114 的第二道闸：没有归属的工具不对模型暴露
    reg.register("search", lambda **kw: kw, description="检索素材", params_schema=_SCHEMA,
                 capability=CAP_RETRIEVAL)
    reg.register("internal_helper", lambda: None, description="内部工具，不对模型开放")
    return reg


def test_only_tools_with_schema_are_exposed():
    tools = _registry().as_openai_tools()
    names = [t["function"]["name"] for t in tools]
    assert names == ["search"], "没写参数契约的工具不应被模型看到"
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["description"] == "检索素材"
    assert tools[0]["function"]["parameters"]["required"] == ["q"]


def test_empty_registry_exports_nothing():
    assert ToolRegistry().as_openai_tools() == []
    assert mention_tools_available([]) is None


def test_params_schema_must_be_object():
    with pytest.raises(Exception):
        ToolRegistry().register("bad", lambda: None, params_schema={"type": "string"})


def test_exposed_to_model_flag():
    """暴露要过两道闸：参数契约（U112）+ 能力归属（U114）。"""
    spec = ToolSpec(name="s", handler=lambda: None, params_schema=_SCHEMA,
                    capability=CAP_RETRIEVAL)
    assert spec.exposed_to_model is True
    assert ToolSpec(name="i", handler=lambda: None).exposed_to_model is False
    # 只有契约没归属 → 不暴露（宁可少暴露，也不让模型看见职责外的工具）
    assert ToolSpec(name="j", handler=lambda: None,
                    params_schema=_SCHEMA).exposed_to_model is False


# --------------------------------------------------------------------------- #
# 2. 能力协商
# --------------------------------------------------------------------------- #
def test_unsupported_provider_rejects_tools_with_real_reason():
    from ai_butler.infra.llm_gateway import Provider

    class Dummy(Provider):
        name = "dummy"
        supports_tools = False

    provider = Dummy(Settings())
    with pytest.raises(ToolUnsupportedError) as info:
        provider.reject_unsupported_tools([{"type": "function"}], "whatever")
    assert "dummy" in str(info.value)
    assert "function calling" in str(info.value)


def test_ollama_model_whitelist_gate():
    from ai_butler.infra.llm_gateway import OllamaProvider

    settings = Settings(function_calling_models=("llama3.1:8b",))
    provider = OllamaProvider(settings)

    # 白名单外：本机 qwen2.5vl:3b 就是这类 —— 必须说清楚是模型不支持
    with pytest.raises(ToolUnsupportedError) as info:
        provider.reject_unsupported_tools([{"type": "function"}], "qwen2.5vl:3b")
    assert "qwen2.5vl:3b" in str(info.value)
    assert "function_calling_models" in str(info.value)

    # 白名单内：放行
    provider.reject_unsupported_tools([{"type": "function"}], "llama3.1:8b")
    # 不带工具时任何模型都放行（普通对话不受影响）
    provider.reject_unsupported_tools(None, "qwen2.5vl:3b")


def test_gateway_reports_tool_capability_without_calling_out():
    """能力结论要能**主动查**，不能只能靠抛异常才知道为什么不能用。

    这条出口给的是可诊断的说明：换供应商 / 登记白名单是两种修法，
    糊成一句「供应商都不可用」就等于没给原因。
    """
    from ai_butler.infra.llm_gateway import LLMGateway

    settings = Settings(provider_order=("ollama",), function_calling_models=())
    gateway = LLMGateway(settings, None)
    capable, why = gateway.tool_capability()
    assert capable is False
    assert "function_calling_models" in why

    settings2 = Settings(provider_order=("ollama",),
                         function_calling_models=("qwen2.5vl:3b",))
    capable2, why2 = LLMGateway(settings2, None).tool_capability()
    assert capable2 is True, why2

    # 没有任何可用供应商时也要给出原因，而不是静默 False
    empty = LLMGateway(Settings(provider_order=()), None)
    capable3, why3 = empty.tool_capability()
    assert capable3 is False and why3


# --------------------------------------------------------------------------- #
# 3. 解析健壮
# --------------------------------------------------------------------------- #
def test_parse_openai_tool_calls():
    bundle = {"tool_calls": [
        {"id": "c1", "function": {"name": "search", "arguments": '{"q":"杭州"}'}},
        {"id": "c2", "function": {"name": "pay", "arguments": "{半截"}},       # 坏 JSON
        {"id": "c3", "function": {"name": "", "arguments": "{}"}},            # 没名字
        {"function": {"name": "x", "arguments": '"不是对象"'}},               # 非 dict
    ]}
    assert parse_openai_tool_calls(bundle) == (
        ModelToolCall(name="search", arguments={"q": "杭州"}, call_id="c1"),)


def test_parse_ollama_tool_calls_object_arguments():
    bundle = {"tool_calls": [{"function": {"name": "search", "arguments": {"q": "杭州"}}}]}
    assert _parse_ollama_tool_calls(bundle) == (
        ModelToolCall(name="search", arguments={"q": "杭州"}),)


def test_chat_result_defaults_to_no_tool_calls():
    result = ChatResult(text="hi", provider="p", model="m", latency_ms=1)
    assert result.tool_calls == ()


# --------------------------------------------------------------------------- #
# 4. 回路护栏
# --------------------------------------------------------------------------- #
class FakeGateway:
    """按脚本回放模型回复，不发真实请求。"""

    def __init__(self, replies: list[ChatResult]) -> None:
        self.replies = list(replies)
        self.calls: list[int] = []

    async def chat(self, *, user_id, messages, tools=None, **kwargs):
        self.calls.append(len(messages))
        return self.replies.pop(0) if self.replies else ChatResult(
            text="没了", provider="fake", model="fake", latency_ms=0)


def _dispatcher() -> ToolDispatcher:
    registry = ToolRegistry()
    registry.register("search", lambda payload=None, **kw: {"hits": 2},
                      description="检索", params_schema=_SCHEMA,
                      required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="test_agent", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    return dispatcher


def _reply(text: str = "", tool_calls=()) -> ChatResult:
    return ChatResult(text=text, provider="fake", model="fake", latency_ms=0,
                      tool_calls=tuple(tool_calls))


def test_loop_executes_model_tool_call_through_dispatcher():
    """模型说「调 search」→ 真的经 ToolDispatcher 执行 → 模型看到结果后收尾。"""
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="search", arguments={"q": "杭州"})]),
        _reply(text="查到 2 条"),
    ])
    loop = FunctionCallLoop(gateway, _dispatcher(), max_rounds=3)
    out = asyncio.run(loop.run(user_id="user-A",
                               messages=[ChatMessage(role="user", content="杭州攻略")],
                               tools=_registry().as_openai_tools()))
    assert isinstance(out, ToolLoopResult)
    assert out.text == "查到 2 条"
    assert out.rounds == 2
    assert out.truncated is False
    assert [r.call.name for r in out.tool_results] == ["search"]
    # 结果被回灌进对话：第二轮的消息数必须比第一轮多
    assert gateway.calls[1] > gateway.calls[0]


def test_loop_stops_at_max_rounds_and_says_so():
    """模型一直要调工具 → 撞上限必须标 truncated，不能当成正常完成。"""
    gateway = FakeGateway([_reply(tool_calls=[ModelToolCall(name="search", arguments={})])
                           for _ in range(9)])
    loop = FunctionCallLoop(gateway, _dispatcher(), max_rounds=3)
    out = asyncio.run(loop.run(user_id="user-A",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=_registry().as_openai_tools()))
    assert out.truncated is True
    assert out.ok is False
    assert out.rounds == 3
    assert len(out.tool_results) == 3


def test_unregistered_tool_call_is_recorded_not_crashed():
    """模型叫了个不存在的工具：回路不崩，但要把失败如实告诉模型（degradation 有痕）。"""
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="不存在", arguments={})]),
        _reply(text="好吧"),
    ])
    loop = FunctionCallLoop(gateway, _dispatcher(), max_rounds=3)
    out = asyncio.run(loop.run(user_id="user-A",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=[]))
    assert out.text == "好吧"
    # 工具不存在时**没有** ToolResult 可收，但失败进 degradation 且回灌给了模型
    assert out.tool_results == ()
    assert any("不存在" in note for note in out.degradation), out.degradation
    assert out.ok is True


def test_loop_result_renders_failure_with_reason():
    from ai_butler.scheduler.tool_loop import FunctionCallLoop as FCL
    from ai_butler.scheduler.tools import ToolResult

    failed = ToolResult(call=ToolCall(name="pay", payload={}), ok=False,
                        note="需要人工确认", error_code="NEEDS_APPROVAL", approval_id="AP-1")
    text = FCL._render("pay", failed)
    assert "失败" in text and "NEEDS_APPROVAL" in text and "AP-1" in text

    ok_result = ToolResult(call=ToolCall(name="search", payload={}), ok=True,
                           output={"hits": 2})
    assert "search" in FCL._render("search", ok_result)
