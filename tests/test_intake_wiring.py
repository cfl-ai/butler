"""U114：离散单元接通 function calling 回路的守护。

守的核心是**接通后的两条不变量**：
1. 开关关闭时**完全不消费模型调用**，行为与接通前逐字一致；
2. 下发给模型的工具**必须按调配能力裁剪** —— 模型看不见职责外的工具。
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_PAYMENT,
    CAP_RETRIEVAL,
    CAP_WIKI_WRITE,
)
from ai_butler.core.config import Settings
from ai_butler.core.permissions import AgentIdentity, CapabilityLevel, ToolPermission, UserRole
from ai_butler.infra.llm_gateway import ChatMessage, ChatResult, ModelToolCall
from ai_butler.scheduler.intake import (
    Assignment,
    IntakeContext,
    build_tools_for,
    run_units,
)
from ai_butler.scheduler.tools import ToolDispatcher, ToolRegistry

_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("search", lambda **kw: {"hits": 1}, description="检索",
                 params_schema=_SCHEMA, capability=CAP_RETRIEVAL,
                 required=CapabilityLevel.L0_READ)
    reg.register("pay", lambda **kw: {"paid": True}, description="支付",
                 params_schema=_SCHEMA, capability=CAP_PAYMENT)
    reg.register("write_wiki", lambda **kw: True, description="写公共知识库",
                 params_schema=_SCHEMA, capability=CAP_WIKI_WRITE)
    return reg


def _dispatcher(registry: ToolRegistry) -> ToolDispatcher:
    d = ToolDispatcher(registry)
    d.bind_permission(ToolPermission(
        agent=AgentIdentity(key="test_agent", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    return d


class FakeGateway:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.seen_tools: list[tuple[str, ...]] = []
        self.seen_first_messages: list[int] = []

    async def chat(self, *, user_id, messages, tools=None, **kwargs):
        self.seen_tools.append(tuple(t["function"]["name"] for t in (tools or [])))
        self.seen_first_messages.append(len(messages))
        return self.replies.pop(0) if self.replies else ChatResult(
            text="结束", provider="fake", model="fake", latency_ms=0)


def _reply(text: str, calls=()) -> ChatResult:
    return ChatResult(text=text, provider="fake", model="fake", latency_ms=0,
                      tool_calls=tuple(calls))


# --------------------------------------------------------------------------- #
# 工具裁剪
# --------------------------------------------------------------------------- #
def test_tools_are_filtered_by_assignment_capabilities():
    """调配只给了检索能力，模型就**看不见**支付与写库工具。"""
    tools = build_tools_for(Assignment("planner_agent", (CAP_RETRIEVAL,)), _registry())
    assert [t["function"]["name"] for t in tools] == ["search"]


def test_assignment_without_matching_tool_yields_empty():
    tools = build_tools_for(Assignment("x", (CAP_DATA_READ,)), _registry())
    assert tools == []


def test_uncategorized_tool_is_never_exposed():
    reg = ToolRegistry()
    reg.register("orphan", lambda: None, description="没归属",
                 params_schema=_SCHEMA)                      # 未声明 capability
    assert reg.as_openai_tools() == []
    assert build_tools_for(Assignment("a", (CAP_RETRIEVAL,)), reg) == []


# --------------------------------------------------------------------------- #
# 接通：关闭时
# --------------------------------------------------------------------------- #
def test_disabled_switch_consumes_nothing():
    """开关关闭：不发起任何模型调用，且如实标 disabled。"""
    gateway = FakeGateway([])
    report = asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(_registry()), registry=_registry(),
        user_id="user-A", utterance="杭州天气怎么样",
        settings=Settings(function_calling_enable=False)))
    assert report.disabled is True
    assert report.outcomes == ()
    assert gateway.seen_tools == []


# --------------------------------------------------------------------------- #
# 接通：打开时
# --------------------------------------------------------------------------- #
def test_enabled_runs_loop_with_filtered_tools():
    gateway = FakeGateway([_reply("杭州晴")])
    report = asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(_registry()), registry=_registry(),
        user_id="user-A", utterance="杭州天气怎么样",
        intent_label="fact_question",       # 路由结论由调用方给（本层不取代路由）
        settings=Settings(function_calling_enable=True)))
    assert report.disabled is False
    assert report.ran_count == 1
    # 下发的工具必须是裁剪过的（这里只有 retrieval 类的 search）
    assert gateway.seen_tools[0] == ("search",)


def test_each_unit_gets_its_own_loop_context():
    """多意图拆开后，每个单元**各自起一条回路**，不共用对话上下文。"""
    gateway = FakeGateway([_reply("杭州晴"), _reply("北京晴")])
    report = asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(_registry()), registry=_registry(),
        user_id="user-A", utterance="查一下杭州的天气，顺便看看北京的天气",
        intent_label="fact_question",
        settings=Settings(function_calling_enable=True)))
    assert len(report.outcomes) == 2
    assert report.ran_count == 2, "两个单元都应各自跑通一条回路"
    # 第二条回路的初始消息数不应因第一条而变多 —— 单元之间没有上下文污染
    assert len(gateway.seen_first_messages) == 2
    assert gateway.seen_first_messages[0] == gateway.seen_first_messages[1]


def test_unassigned_unit_is_recorded_not_forced():
    """调配表查不到的单元：如实记 unassigned，**不硬塞给模型**。"""
    gateway = FakeGateway([])
    report = asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(_registry()), registry=_registry(),
        user_id="user-A", utterance="你好呀",
        settings=Settings(function_calling_enable=True)))
    assert report.ran_count == 0
    assert report.unassigned, "调配不到的单元必须被点名，而不是静默丢弃"
    assert gateway.seen_tools == []


def test_unit_with_no_tool_for_capability_is_skipped_with_reason():
    """有能力结论但注册表里没有对应工具：记原因，不拿全量工具凑数。"""
    reg = ToolRegistry()                     # 空注册表
    gateway = FakeGateway([])
    report = asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(reg), registry=reg,
        user_id="user-A", utterance="杭州天气怎么样",
        intent_label="fact_question",
        settings=Settings(function_calling_enable=True)))
    assert report.ran_count == 0
    assert report.outcomes[0].skipped == "no_tools_for_capabilities"
    assert gateway.seen_tools == []


def test_context_is_consumed_before_the_loop():
    """指代在进回路前就被消解：模型看到的是自包含文本，不是「那里」。"""
    gateway = FakeGateway([_reply("晴")])
    asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(_registry()), registry=_registry(),
        user_id="user-A", utterance="那里天气怎么样",
        context=IntakeContext(location="杭州"), intent_label="fact_question",
        settings=Settings(function_calling_enable=True)))
    assert gateway.seen_first_messages, "应当发起了调用"


def test_resolved_text_is_what_the_model_sees():
    """模型拿到的是已消解的自包含文本，不是需要猜上下文的「那里」。"""
    gateway = FakeGateway([_reply("晴")])
    report = asyncio.run(run_units(
        gateway=gateway, dispatcher=_dispatcher(_registry()), registry=_registry(),
        user_id="user-A", utterance="那里天气怎么样",
        context=IntakeContext(location="杭州"), intent_label="fact_question",
        settings=Settings(function_calling_enable=True)))
    unit = report.outcomes[0].unit
    assert "杭州" in unit.text and "那里" not in unit.text
    assert unit.resolved == ("那里→杭州",)
    assert unit.self_contained is True
