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

    # 模型名必须**显式传入**：默认值来自 .env.* 的 OLLAMA_MODEL，
    # 而 CI 生成的 .env.test 不保证有这个键（缺失时模型为 None），
    # 用例就会因为「环境里没配模型」而红 —— 那是环境问题，不是能力协商的问题。
    settings = Settings(provider_order=("ollama",), function_calling_models=(),
                        ollama_model="qwen2.5vl:3b")
    gateway = LLMGateway(settings, None)
    capable, why = gateway.tool_capability()
    assert capable is False
    assert "function_calling_models" in why

    settings2 = Settings(provider_order=("ollama",),
                         function_calling_models=("qwen2.5vl:3b",),
                         ollama_model="qwen2.5vl:3b")
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


def test_readonly_round_runs_tools_concurrently():
    """全 L0 轮次并发执行：Barrier 证明两条工具真的在同窗口并行。

    两个 handler 都在 threading.Barrier(2) 上等对方——只有并行执行才能
    双双通过；若退化成串行，先到的会在 timeout 后 BrokenBarrierError，
    该工具结果转失败，断言即红。
    """
    import threading
    barrier = threading.Barrier(2, timeout=5)

    def barrier_tool(payload=None, **kw):
        barrier.wait()
        return {"ok": True}

    registry = ToolRegistry()
    for name in ("ra", "rb"):
        registry.register(name, barrier_tool, description="只读探针",
                          params_schema=_SCHEMA, required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="test_agent", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="ra", arguments={}),
                           ModelToolCall(name="rb", arguments={})]),
        _reply(text="完成"),
    ])
    loop = FunctionCallLoop(gateway, dispatcher, max_rounds=3)
    out = asyncio.run(loop.run(user_id="user-A",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=[]))
    assert out.text == "完成"
    # 结果必须按输入顺序合并，不因并发而乱序
    assert [r.call.name for r in out.tool_results] == ["ra", "rb"]
    assert all(r.ok for r in out.tool_results)


def test_mixed_writeread_round_stays_serial():
    """混有 L1+ 的轮次保持串行：写路径与审批票的顺序语义不能冒险。"""
    import threading
    barrier = threading.Barrier(2, timeout=1)

    def slow_read(payload=None, **kw):
        barrier.wait()
        return {"ok": True}

    def write_tool(payload=None, **kw):
        return {"written": True}

    registry = ToolRegistry()
    registry.register("read_slow", slow_read, description="只读",
                      params_schema=_SCHEMA, required=CapabilityLevel.L0_READ)
    registry.register("write_thing", write_tool, description="写",
                      params_schema=_SCHEMA, required=CapabilityLevel.L1_WRITE_OWN)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="test_agent", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="read_slow", arguments={}),
                           ModelToolCall(name="write_thing", arguments={})]),
        _reply(text="完成"),
    ])
    loop = FunctionCallLoop(gateway, dispatcher, max_rounds=3)
    out = asyncio.run(loop.run(user_id="user-A",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=[]))
    assert out.text == "完成"
    assert [r.call.name for r in out.tool_results] == ["read_slow", "write_thing"]
    by_name = {r.call.name: r for r in out.tool_results}
    # 证明点：read_slow 独自等待 Barrier 直至超时破裂——只有串行执行才会
    # 这样（若被并发，两条调用会同时到齐 Barrier 而双双通过）。
    assert by_name["read_slow"].ok is False
    assert "BrokenBarrierError" in (by_name["read_slow"].note or "")


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


# --------------------------------------------------------------------------- #
# 5. 工具专用路由（U117）：开关不是装饰
# --------------------------------------------------------------------------- #
def test_model_family_matching_does_not_overclaim():
    """按模型族判定，且**不能**把 qwen2.5vl 误判成支持工具。

    裸 `startswith` 会让白名单项 `qwen2.5` 命中 `qwen2.5vl` —— 而带 vl 的
    视觉模型恰恰没有任何工具能力（本机实测）。这条守的就是那个坑。
    """
    from ai_butler.core.config import DEFAULT_TOOL_CAPABLE_MODELS
    from ai_butler.infra.llm_gateway import model_supports_tools

    assert model_supports_tools("qwen3:8b", DEFAULT_TOOL_CAPABLE_MODELS)
    assert model_supports_tools("qwen2.5:7b", DEFAULT_TOOL_CAPABLE_MODELS)
    assert not model_supports_tools("qwen2.5vl:3b", DEFAULT_TOOL_CAPABLE_MODELS)
    assert not model_supports_tools("deepseek-chat", ())
    # 显式登记的完整模型名（带标签）也要能命中：写白名单的人不会总记得去标签
    assert model_supports_tools("llama3.1:8b", ("llama3.1:8b",))


def test_tool_order_prefers_the_designated_provider():
    """本机 Ollama 无工具能力 → 工具请求必须排到具备能力的供应商上。"""
    from ai_butler.infra.llm_gateway import LLMGateway

    settings = Settings(provider_order=("ollama",),
                        function_calling_provider="deepseek",
                        function_calling_model="deepseek-chat")
    gateway = LLMGateway(settings, None)
    assert gateway._tool_order() == ["deepseek", "ollama"], "不支持的留作最后，而不是去掉"
    capable, why = gateway.tool_capability()
    assert capable is True and "deepseek" in why

    # 普通请求的顺序**不受影响**：日常问答仍然先走本机模型
    assert gateway._targets(None) == ["ollama"]


def test_tool_capability_reports_the_real_tool_route_not_provider_order_head():
    from ai_butler.infra.llm_gateway import LLMGateway

    settings = Settings(provider_order=("ollama",), function_calling_models=())
    capable, why = LLMGateway(settings, None).tool_capability()
    assert capable is False
    assert "function_calling_models" in why, "真因不能被淹没（U112 立项原因）"


def test_route_for_tools_reroutes_when_the_chosen_model_cannot_call_tools():
    """对话入口不把「用户所选模型」硬塞进工具请求，否则每次都被拒 → 开关空转。"""
    from ai_butler.scheduler.chat_intake import _route_for_tools
    from ai_butler.infra.llm_gateway import LLMGateway

    settings = Settings(provider_order=("ollama",),
                        function_calling_provider="deepseek",
                        function_calling_model="deepseek-chat")
    gateway = LLMGateway(settings, None)

    kwargs, note = _route_for_tools(
        gateway, settings, {"provider": "ollama", "model": "qwen2.5vl:3b"})
    assert kwargs == {}, "改道时不能把旧供应商/模型/凭据带过去"
    assert "改道" in note and "deepseek" in note

    # 用户所选模型本身可用 → 原样尊重，不改道
    keep = {"provider": "openai-compatible", "model": "glm-4.6"}
    same, note2 = _route_for_tools(gateway, settings, keep)
    assert same == keep and note2 == ""


def test_route_for_tools_refuses_when_no_tool_provider_is_configured():
    """既没有可用模型、又没配专用供应商：如实跳过，不硬造一个能用的假象。"""
    from ai_butler.scheduler.chat_intake import _route_for_tools
    from ai_butler.infra.llm_gateway import LLMGateway

    settings = Settings(provider_order=("ollama",), function_calling_models=(),
                        function_calling_provider="")
    kwargs, why = _route_for_tools(LLMGateway(settings, None), settings, {})
    assert kwargs is None
    assert why


# --------------------- P1-C 并发执行段的副作用验证 --------------------- #
def test_concurrent_round_keeps_dispatcher_accounting_consistent():
    """数据一致性：并发轮结束后 dispatcher 记账与缓存必须完整、无丢失。"""
    registry = ToolRegistry()
    for name in ("ca", "cb"):
        registry.register(name, lambda payload=None, **kw: {"ok": True},
                          description="只读", params_schema=_SCHEMA,
                          required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="t", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="ca", arguments={}),
                           ModelToolCall(name="cb", arguments={})]),
        _reply(text="done"),
    ])
    loop = FunctionCallLoop(gateway, dispatcher, max_rounds=3)
    out = asyncio.run(loop.run(user_id="u",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=[]))
    assert out.text == "done"
    # 记账：两条都进 history，条数与成功数一致
    assert dispatcher.call_count == 2
    assert len(dispatcher.history()) == 2
    assert all(r.ok for r in dispatcher.history())
    # 缓存：两个签名都已登记（供后续重复拦截）
    assert len(dispatcher._cache) == 2


def test_concurrent_round_one_tool_fails_other_succeeds_order_kept():
    """错误处理：并发轮中一个工具抛异常，另一个正常——失败进 degradation，
    成功进结果，输入顺序在混合结果下依然保持。"""
    registry = ToolRegistry()
    registry.register("good", lambda payload=None, **kw: {"n": 1},
                      description="只读", params_schema=_SCHEMA,
                      required=CapabilityLevel.L0_READ)
    def boom(payload=None, **kw):
        raise RuntimeError("数据库连接失败")
    registry.register("bad", boom, description="只读", params_schema=_SCHEMA,
                      required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="t", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="bad", arguments={}),
                           ModelToolCall(name="good", arguments={})]),
        _reply(text="部分完成"),
    ])
    loop = FunctionCallLoop(gateway, dispatcher, max_rounds=3)
    out = asyncio.run(loop.run(user_id="u",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=[]))
    assert out.text == "部分完成"
    # dispatch 把 handler 异常转成 ok=False 的 ToolResult：失败者也进结果，
    # 且输入顺序在混合结果下依然保持
    assert [r.call.name for r in out.tool_results] == ["bad", "good"]
    by_name = {r.call.name: r for r in out.tool_results}
    assert by_name["bad"].ok is False and "RuntimeError" in (by_name["bad"].note or "")
    assert by_name["good"].ok is True
    assert any("bad" in n for n in out.degradation)              # 失败原因回灌模型
    assert out.ok is True                                        # 单工具失败不终止回路


def test_concurrent_round_duplicate_signatures_both_succeed():
    """同签名并发轮：串行路径会命中重复拦截，并发路径下两线程可能同时
    未命中缓存而都执行——对 L0 只读工具无害（幂等）。必须不崩、结果齐全、
    顺序保持。这条测试同时锁定并发与串行两条路径的公共行为。"""
    calls = {"n": 0}
    def counting(payload=None, **kw):
        calls["n"] += 1
        return {"n": calls["n"]}
    registry = ToolRegistry()
    registry.register("dup", counting, description="只读", params_schema=_SCHEMA,
                      required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="t", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    gateway = FakeGateway([
        _reply(tool_calls=[ModelToolCall(name="dup", arguments={}),
                           ModelToolCall(name="dup", arguments={})]),
        _reply(text="ok"),
    ])
    loop = FunctionCallLoop(gateway, dispatcher, max_rounds=3)
    out = asyncio.run(loop.run(user_id="u",
                               messages=[ChatMessage(role="user", content="x")],
                               tools=[]))
    assert out.text == "ok"
    assert len(out.tool_results) == 2
    assert all(r.call.name == "dup" for r in out.tool_results)
    # 幂等性：无论执行几次，输出语义一致、无异常泄漏
    assert all(r.ok for r in out.tool_results)


def test_concurrent_loops_leave_no_lingering_threads():
    """资源泄漏：多次并发轮回合跑完后线程数回落到基线
    （asyncio.run 关闭默认线程池，to_thread 的线程应被回收）。"""
    import threading
    registry = ToolRegistry()
    registry.register("tiny", lambda payload=None, **kw: {"ok": True},
                      description="只读", params_schema=_SCHEMA,
                      required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="t", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    baseline = threading.active_count()
    for _ in range(3):
        gateway = FakeGateway([
            _reply(tool_calls=[ModelToolCall(name="tiny", arguments={}),
                               ModelToolCall(name="tiny", arguments={"k": 1})]),
            _reply(text="ok"),
        ])
        loop = FunctionCallLoop(gateway, dispatcher, max_rounds=2)
        out = asyncio.run(loop.run(user_id="u",
                                   messages=[ChatMessage(role="user", content="x")],
                                   tools=[]))
        assert out.ok is True
    assert threading.active_count() == baseline   # 无泄漏
