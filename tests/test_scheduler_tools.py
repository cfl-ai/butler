"""P3 工具统一调度与资源节流测试。

计划书依据：三、P3「实现工具统一调度、重复调用拦截、无增量终止；
调用数据中台、RAG、Wiki 自动透传 state 中的 user_id」
+「开发 Token 限流、检索量管控、资源节流逻辑；限流按账号维度统计；
Jev 调用独立配额统计」
"""

from __future__ import annotations

import pytest

from ai_butler.scheduler.throttle import (
    ResourceThrottle,
    ThrottleError,
    ThrottlePolicy,
)
from ai_butler.scheduler.tools import (
    DEFAULT_MAX_CALLS,
    DEFAULT_NO_PROGRESS_LIMIT,
    ToolCall,
    ToolDispatcher,
    ToolError,
    ToolRegistry,
)


# ============================== 工具注册表 ============================== #
def _registry(**handlers):
    reg = ToolRegistry()
    for name, fn in handlers.items():
        reg.register(name, fn)
    return reg


def test_registry_registers_and_lists_tools():
    reg = _registry(search=lambda: "ok")
    assert reg.names() == ("search",)
    assert reg.get("search").name == "search"
    assert reg.specs()[0].description == ""


def test_registry_rejects_duplicates_and_bad_names():
    reg = _registry(search=lambda: 1)
    with pytest.raises(ToolError):
        reg.register("search", lambda: 2)
    with pytest.raises(ToolError):
        reg.register("", lambda: 3)
    with pytest.raises(ToolError):
        reg.get("ghost")


def test_tool_call_validation_and_signature_is_key_order_independent():
    with pytest.raises(ToolError):
        ToolCall("")
    a = ToolCall("rag.search", {"q": "西湖", "top": 3})
    b = ToolCall("rag.search", {"top": 3, "q": "西湖"})
    assert a.signature == b.signature, "同义入参必须得到同一签名"
    assert ToolCall("rag.search", {"q": "黄山"}).signature != a.signature


# ============================== 统一调度 ============================== #
def test_dispatch_executes_through_the_registry():
    reg = _registry(echo=lambda text="": f"echo:{text}")
    dispatcher = ToolDispatcher(reg)
    result = dispatcher.dispatch(ToolCall("echo", {"text": "hi"}))
    assert result.ok and result.output == "echo:hi"
    assert dispatcher.call_count == 1


def test_duplicate_call_is_intercepted_and_not_reexecuted():
    """计划书三、P3「重复调用拦截」——同签名第二次不得真正执行。"""
    calls: list[str] = []

    def handler(text=""):
        calls.append(text)
        return f"got:{text}"

    dispatcher = ToolDispatcher(_registry(echo=handler))
    first = dispatcher.dispatch(ToolCall("echo", {"text": "x"}))
    second = dispatcher.dispatch(ToolCall("echo", {"text": "x"}))

    assert calls == ["x"], "重复调用被拦截，handler 只应执行一次"
    assert first.duplicate is False and second.duplicate is True
    assert second.output == first.output, "应复用上次结果"
    assert "重复调用被拦截" in second.note


def test_no_progress_termination_after_repeated_empty_results():
    """计划书三、P3「无增量终止」。"""
    dispatcher = ToolDispatcher(_registry(noop=lambda **_: None))
    for i in range(DEFAULT_NO_PROGRESS_LIMIT):
        result = dispatcher.dispatch(ToolCall("noop", {"i": i}))
        assert result.ok and result.progressed is False

    assert dispatcher.no_progress is True
    assert "无增量" in dispatcher.blocked_reason

    refused = dispatcher.dispatch(ToolCall("noop", {"i": 999}))
    assert refused.ok is False and "拒绝继续调用" in refused.note


def test_progress_resets_the_no_progress_streak():
    def sometimes(flag=False):
        return "数据" if flag else None

    dispatcher = ToolDispatcher(_registry(fetch=sometimes))
    dispatcher.dispatch(ToolCall("fetch", {"flag": False}))
    dispatcher.dispatch(ToolCall("fetch", {"flag": False}))
    dispatcher.dispatch(ToolCall("fetch", {"flag": True}))     # 有增量，清零
    dispatcher.dispatch(ToolCall("fetch", {"flag": False}))
    assert dispatcher.no_progress is False


def test_custom_progress_predicate():
    """自定义增量判定：默认「有非空输出即增量」不够用时，由工具自己声明。

    例如检索工具返回空结果集——输出非空，但对业务而言等于没查到东西。
    """
    registry = ToolRegistry()
    registry.register("fetch", lambda **_: {"items": []},
                      progresses=lambda out: bool(out.get("items")))
    dispatcher = ToolDispatcher(registry)
    for i in range(DEFAULT_NO_PROGRESS_LIMIT):
        assert dispatcher.dispatch(ToolCall("fetch", {"i": i})).progressed is False
    assert dispatcher.no_progress is True, "空结果集应判为无增量"


def test_max_calls_ceiling_stops_runaway_loops():
    dispatcher = ToolDispatcher(_registry(count=lambda i=0: i), max_calls=3)
    for i in range(3):
        assert dispatcher.dispatch(ToolCall("count", {"i": i})).ok
    blocked = dispatcher.dispatch(ToolCall("count", {"i": 99}))
    assert blocked.ok is False and "次数达上限" in dispatcher.blocked_reason


# ------------------------ user_id 自动透传 ------------------------ #
def test_user_id_is_injected_into_tools_that_need_it():
    """计划书三、P3「调用数据中台、RAG、Wiki 自动透传 state 中的 user_id」。"""
    seen: dict[str, str] = {}
    reg = ToolRegistry()
    reg.register("data.read_profile", lambda user_id="": seen.setdefault("uid", user_id),
                 needs_user_id=True)
    dispatcher = ToolDispatcher(reg)
    dispatcher.dispatch(ToolCall("data.read_profile"), user_id="user-A")
    assert seen["uid"] == "user-A"


def test_caller_cannot_forge_user_id():
    """调用方自己传的 user_id 必须被 state 的值覆盖，无从伪造。"""
    seen: dict[str, str] = {}
    reg = ToolRegistry()
    reg.register("wiki.read", lambda user_id="": seen.setdefault("uid", user_id),
                 needs_user_id=True)
    dispatcher = ToolDispatcher(reg)
    dispatcher.dispatch(ToolCall("wiki.read", {"user_id": "user-B"}), user_id="user-A")
    assert seen["uid"] == "user-A", "伪造的 user_id 必须被覆盖"


def test_tool_needing_user_id_is_refused_without_context():
    reg = ToolRegistry()
    reg.register("wiki.read", lambda user_id="": "x", needs_user_id=True)
    dispatcher = ToolDispatcher(reg)
    result = dispatcher.dispatch(ToolCall("wiki.read"), user_id="")
    assert result.ok is False and "未绑定账号" in result.note


def test_tool_failure_does_not_raise():
    def boom():
        raise RuntimeError("工具炸了")

    dispatcher = ToolDispatcher(_registry(boom=boom))
    result = dispatcher.dispatch(ToolCall("boom"))
    assert result.ok is False and "工具炸了" in result.note


def test_snapshot_reports_dispatch_statistics():
    dispatcher = ToolDispatcher(_registry(echo=lambda: "y"))
    dispatcher.dispatch(ToolCall("echo"))
    dispatcher.dispatch(ToolCall("echo"))
    snap = dispatcher.snapshot()
    assert snap["calls"] == 2 and snap["duplicates"] == 1
    assert snap["no_progress"] is False and snap["tools"] == ["echo"]


# ============================== 资源节流 ============================== #
def test_throttle_policy_validates_every_quota():
    with pytest.raises(ThrottleError):
        ThrottlePolicy(tokens_per_account_per_minute=0)
    with pytest.raises(ThrottleError):
        ThrottlePolicy(tool_calls_per_task=-1)


def test_throttle_requires_user_id_for_account_scoped_quotas():
    """计划书三、P3「限流按账号维度统计」。"""
    throttle = ResourceThrottle()
    with pytest.raises(ThrottleError) as ei:
        throttle.consume_tokens("", 100)
    assert "按账号维度" in str(ei.value)


def test_throttle_counts_are_isolated_per_account():
    throttle = ResourceThrottle(ThrottlePolicy(tokens_per_account_per_minute=100))
    throttle.consume_tokens("user-A", 80)
    throttle.consume_tokens("user-B", 10)
    assert throttle.used("user-A", ResourceThrottle.TOKENS) == 80
    assert throttle.used("user-B", ResourceThrottle.TOKENS) == 10
    assert throttle.remaining("user-B", ResourceThrottle.TOKENS) == 90


def test_token_quota_is_enforced():
    throttle = ResourceThrottle(ThrottlePolicy(tokens_per_account_per_minute=100))
    throttle.consume_tokens("user-A", 60)
    with pytest.raises(ThrottleError) as ei:
        throttle.consume_tokens("user-A", 50)
    assert "已达上限" in str(ei.value)


def test_jev_quota_is_independent_from_business_quotas():
    """计划书三、P3「Jev 调用独立配额统计」：业务话务不得挤占 Jev 配额，反之亦然。"""
    throttle = ResourceThrottle(ThrottlePolicy(
        tokens_per_account_per_minute=10, retrievals_per_account_per_minute=1,
        jev_calls_per_account_per_minute=5))
    throttle.consume_retrieval("user-A")                  # 检索配额用满
    with pytest.raises(ThrottleError):
        throttle.consume_retrieval("user-A")

    # 检索配额已满，但 Jev 配额不受影响
    assert throttle.consume_jev("user-A") == 1
    assert throttle.remaining("user-A", ResourceThrottle.JEV) == 4
    # 反之，Jev 用量也不计入检索
    assert throttle.used("user-A", ResourceThrottle.RETRIEVALS) == 1


def test_window_expiry_frees_the_quota():
    clock = [0.0]
    throttle = ResourceThrottle(ThrottlePolicy(retrievals_per_account_per_minute=1),
                                clock=lambda: clock[0])
    throttle.consume_retrieval("user-A")
    with pytest.raises(ThrottleError):
        throttle.consume_retrieval("user-A")
    clock[0] = 61.0                                        # 窗口滑过
    assert throttle.consume_retrieval("user-A") == 1


def test_tool_call_quota_is_per_task_and_resettable():
    throttle = ResourceThrottle(ThrottlePolicy(tool_calls_per_task=2))
    throttle.consume_tool_call("user-A")
    throttle.consume_tool_call("user-A")
    with pytest.raises(ThrottleError):
        throttle.consume_tool_call("user-A")
    throttle.reset_task("user-A")
    assert throttle.consume_tool_call("user-A") == 1


def test_snapshot_lists_every_bucket():
    throttle = ResourceThrottle()
    snap = throttle.snapshot("user-A")
    assert set(snap) == set(ResourceThrottle.BUCKETS)
    assert all(v["used"] == 0 for v in snap.values())


def test_dispatcher_consults_the_throttle():
    """工具统一调度与资源节流联动：配额用尽即拒绝调用。"""
    throttle = ResourceThrottle(ThrottlePolicy(tool_calls_per_task=1))
    dispatcher = ToolDispatcher(_registry(echo=lambda i=0: i), throttle=throttle)
    assert dispatcher.dispatch(ToolCall("echo", {"i": 1}), user_id="user-A").ok is True
    blocked = dispatcher.dispatch(ToolCall("echo", {"i": 2}), user_id="user-A")
    assert blocked.ok is False and "资源节流" in blocked.note


# ============================== 默认配额（实现参数）============================== #
def test_default_quotas_are_pinned():
    """默认配额是**实现参数**，必须被钉住 —— 改动要是有意的，就得同时改这条。

    此前这四条只有「显式传 policy」的用例，**默认值本身没有任何断言**：
    把 60000 改成 6 也不会有测试变红。
    """
    from ai_butler.scheduler import throttle as th

    assert th.DEFAULT_TOKENS_PER_ACCOUNT_PER_MINUTE == 60_000
    assert th.DEFAULT_RETRIEVALS_PER_ACCOUNT_PER_MINUTE == 30
    assert th.DEFAULT_JEV_CALLS_PER_ACCOUNT_PER_MINUTE == 120
    assert th.DEFAULT_TOOL_CALLS_PER_TASK == 50
    assert th.WINDOW_SECONDS == 60


def test_two_tool_call_ceilings_stay_in_step():
    """两处工具调用上限必须同步 —— 它们守的是同一件事的两层。

    · `ToolDispatcher.max_calls`：**单次调度**的上限（调度器自己兜底）
    · `ThrottlePolicy.tool_calls_per_task`：**按账号**的任务预算（资源节流）

    两者数值相同是有意的。但此前**没有任何测试把它们绑在一起**，
    改一处、忘另一处，就会出现「同一个上限有两个值」而无人察觉。
    """
    from ai_butler.scheduler.throttle import DEFAULT_TOOL_CALLS_PER_TASK

    assert DEFAULT_MAX_CALLS == DEFAULT_TOOL_CALLS_PER_TASK, (
        "单次调度上限与按账号任务预算不一致："
        f"DEFAULT_MAX_CALLS={DEFAULT_MAX_CALLS}, "
        f"DEFAULT_TOOL_CALLS_PER_TASK={DEFAULT_TOOL_CALLS_PER_TASK}"
    )
