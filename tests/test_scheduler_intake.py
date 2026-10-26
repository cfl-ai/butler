"""U114：输入接通层在**调度器**上的装配守护。

守的不是离散化本身（那是 `test_intake_discretize.py` 的活），
守的是「这条链路有没有真的被装配起来」（E-86 家族）：
只在测试里被调过的函数，等于生产里没有这条链路。

装配点是 `SceneScheduler._node_dispatch` —— **唯一的任务分发者**。

⚠️ 两套词表不许混用（E-107）：路由给的意图标签是 `DispatchPlan` 的分派名
（如 `"book_hotel"`），`assign()` 要的是 `QueryCategory`（如 `"procedure"`）。
拿分派名当分类用，`ASSIGNMENTS` 会静默全空 —— 表面接通、实际永不派发。
本文件的用例刻意使用**真实分派名**，就是为了守住这条界线。
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import CAP_PAYMENT, CAP_RETRIEVAL
from ai_butler.core import config as config_module
from ai_butler.core.config import Settings
from ai_butler.core.permissions import AgentIdentity, CapabilityLevel, ToolPermission, UserRole
from ai_butler.infra.llm_gateway import ChatResult
from ai_butler.scheduler.graph import DispatchPlan, SceneScheduler
from ai_butler.scheduler.router import HardRule, HardRuleLayer, IntentRouter
from ai_butler.scheduler.tools import ToolDispatcher, ToolRegistry

_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}}


# ------------------------------ 桩 ------------------------------ #
class _StubAgent(SubAgent):
    """记录自己看到的状态 —— 用来断言「接通结论真的到了下游」。"""

    name = "stub_agent"
    writes = ("stub_result",)

    def run(self, state):
        return {"stub_result": {"saw_intake": bool(state.get("intake"))}}


class FakeGateway:
    def __init__(self) -> None:
        self.seen_tools: list[tuple[str, ...]] = []

    async def chat(self, *, user_id, messages, tools=None, **kwargs):
        self.seen_tools.append(tuple(t["function"]["name"] for t in (tools or [])))
        return ChatResult(text="已处理", provider="fake", model="fake", latency_ms=0)


def _tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("search", lambda **kw: {"hits": 1}, description="检索",
                 params_schema=_SCHEMA, capability=CAP_RETRIEVAL,
                 required=CapabilityLevel.L0_READ)
    reg.register("pay", lambda **kw: {"paid": True}, description="支付",
                 params_schema=_SCHEMA, capability=CAP_PAYMENT)
    return reg


def _scheduler(*, gateway=None, registry=None) -> SceneScheduler:
    """路由标签用真实的**分派名**（`book_hotel` / `ask_procedure`）。"""
    router = IntentRouter([HardRuleLayer([
        HardRule(r"订.*酒店", "book_hotel"),
        HardRule(r"报销", "ask_procedure")])])
    reg = registry if registry is not None else _tool_registry()
    return SceneScheduler(
        router=router, agents={"stub_agent": _StubAgent()},
        plan=DispatchPlan(chains={"book_hotel": ("stub_agent",),
                                  "ask_procedure": ("stub_agent",)}),
        tool_registry=reg, gateway=gateway)


def _bind(scheduler: SceneScheduler) -> None:
    """给调度器的工具分发器绑定权限（否则一律拒绝分发）。"""
    scheduler.dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="test_agent", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))


@pytest.fixture
def fc_on(monkeypatch):
    """打开 function calling 开关（默认关闭，既有链路零变化）。

    graph 里是**函数内** import，取的是模块属性，所以替换模块属性即可生效。
    """
    monkeypatch.setattr(config_module, "get_settings",
                        lambda: Settings(function_calling_enable=True))


@pytest.fixture
def fc_off(monkeypatch):
    monkeypatch.setattr(config_module, "get_settings", lambda: Settings())


# ------------------------------ 关闭时：零变化 ------------------------------ #
def test_switch_off_writes_no_intake_channel(fc_off):
    """开关关闭：State 里**根本不出现** intake，与接通前逐字一致。"""
    scheduler = _scheduler(gateway=FakeGateway())
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="帮我订个酒店",
        labels=("book_hotel",)))
    assert result.intake == {}
    assert result.dispatched == ["stub_agent"]


def test_no_gateway_means_the_layer_does_not_exist(fc_on):
    """没装网关就没有 function calling 回路：不写空壳冒充「处理过」。"""
    scheduler = _scheduler(gateway=None)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="帮我订个酒店",
        labels=("book_hotel",)))
    assert result.intake == {}
    assert result.dispatched == ["stub_agent"]


def test_session_expired_skips_intake(fc_on):
    """会话失效：与子 Agent 跳过执行同一口径，接通层也不跑。"""
    scheduler = _scheduler(gateway=FakeGateway())
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="帮我订个酒店",
        labels=("book_hotel",), login_status=False))
    assert result.intake == {}


# ------------------------------ 打开时：真跑 ------------------------------ #
def test_enabled_intake_lands_in_state_and_reaches_agents(fc_on):
    """打开后：单元跑通回路，结论进 State，且**下游 Agent 看得到**。"""
    gateway = FakeGateway()
    scheduler = _scheduler(gateway=gateway)
    _bind(scheduler)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="帮我订个酒店",
        labels=("book_hotel",)))

    assert result.intake, "接通层必须在唯一装配点上真的跑起来"
    assert result.intake["ran_count"] == 1
    assert result.intake["units"][0]["slot_kind"] == "none"
    # 裁剪生效：模型只看得见职责内的检索工具，看不见支付
    assert gateway.seen_tools[0] == ("search",)
    # 分发权没被接管：仍然是路由 + DispatchPlan 决定谁执行
    assert result.dispatched == ["stub_agent"]
    assert result.agent_results["stub_agent"]["stub_result"]["saw_intake"] is True


def test_dispatch_label_is_not_used_as_a_query_category(fc_on):
    """分派名（`book_hotel`）不得被当成任务类型：否则单元会**永远** unassigned。

    这是接通时最容易糊过去的地方 —— 用 `QueryCategory` 的值当路由标签写测试，
    用例全绿，生产里一个单元都派不出去。
    """
    scheduler = _scheduler(gateway=FakeGateway())
    _bind(scheduler)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="帮我订个酒店",
        labels=("book_hotel",)))
    unit = result.intake["units"][0]
    assert unit["category"] != "book_hotel"
    assert unit["agent"], "有调配结论才可能有 agent；空 agent 说明查表没命中"


def test_intake_failure_is_recorded_not_crashed(fc_on):
    """接通层炸了：**留痕且不阻断主流程**，不能把一次请求整体打挂。"""

    class BoomGateway:
        async def chat(self, *, user_id, messages, tools=None, **kwargs):
            raise RuntimeError("供应商抽风")

    scheduler = _scheduler(gateway=BoomGateway())
    _bind(scheduler)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="帮我订个酒店",
        labels=("book_hotel",)))
    assert "error" in result.intake, "失败必须如实标出来，不能写成 ran_count=0 了事"
    assert result.dispatched == ["stub_agent"], "接通失败不该阻断既有分发"


def test_unassignable_unit_is_visible_not_swallowed(fc_on):
    """调配表查不到的单元：如实记 unassigned，且不拿全量工具凑数。

    「怎么申请报销」判得出任务类型（procedure）但判不出信息类型（none），
    `(procedure, none)` 在调配表里没有 —— 此时正确行为是**不派发**，
    而不是把注册表里全部工具丢给模型去猜。
    """
    gateway = FakeGateway()
    scheduler = _scheduler(gateway=gateway)
    _bind(scheduler)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1", utterance="怎么申请报销",
        labels=("ask_procedure",)))
    assert result.intake["ran_count"] == 0
    assert result.intake["unassigned"], "调配不到的单元必须被点名"
    assert gateway.seen_tools == [], "没有调配结论就不该发起模型调用"


def test_tool_registry_is_shared_with_the_dispatcher():
    """回路里的工具调用走的是调度器**同一个**分发器 —— 不是绕过闸门的后门。"""
    reg = _tool_registry()
    scheduler = _scheduler(gateway=FakeGateway(), registry=reg)
    assert isinstance(scheduler.dispatcher, ToolDispatcher)
    assert scheduler.dispatcher.registry is reg, (
        "接通层必须复用调度器的注册表，另建一份等于绕开权限闸门")
