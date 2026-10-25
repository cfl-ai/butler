"""U74 五层意图路由接线：装配、派生、影子旁路、兜底、结论真被消费。

这一层要守的不是「层写得好不好」（那由 `test_scheduler_router.py` 守），而是
**写完的东西有没有通电**，以及通电时的三件纪律：

* **装配**：`create_app` 必须真的构造出 `IntentRouter` 并塞进编排器。
  开关开着却没装，必须当场报错 —— E-77 的形态就是「绿灯 + 空功能」。
* **不漂移**：L1 的正则**从 `planner.CATEGORY_SIGNALS` 派生**，
  所以同一条查询在路由层与规划层必须给出同一个分类（多意图/闲聊除外）。
* **旁路**：影子模式不得给请求增加延迟。原实现是「拿回决策后再判影子」，
  等于每次请求都白等一次模型（最坏 5s）。
* **兜底**：L2 抛错 / L3 模型缺失 / 路由整体炸掉，都要降级到确定性分类，
  且原因进 `notes`；不能变成「查不到」。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from ai_butler.app import build_chat_orchestrator, create_app
from ai_butler.core import access
from ai_butler.core.config import Settings
from ai_butler.core.input_guard import InputGuardError
from ai_butler.infra.llm_gateway import JevClient
from ai_butler.material_center.embedding import HashingEmbedder
from ai_butler.retrieval import chat as chat_mod
from ai_butler.retrieval.planner import QueryCategory, classify
from ai_butler.scheduler import intent_wiring as wiring
from ai_butler.scheduler.router import (
    LAYERS,
    LAYER_HARD_RULE,
    LAYER_LLM,
    LAYER_VECTOR,
    HardRuleLayer,
    IntentRouter,
    JevLayer,
    RouteDecision,
    RouteError,
    RouteRequest,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def settings(settings, tmp_path):
    return settings.model_copy(update={
        "storage_root": str(tmp_path),
        "account_store_root": str(tmp_path),
        "chat_verify_enable": False,
    })


class _Gateway:
    """假网关：够用即可，只记录调用。"""

    def __init__(self, pieces: tuple[str, ...] = ("好的",)) -> None:
        self.pieces = pieces
        self.calls: list[dict] = []

    async def stream(self, *, user_id, messages, model=None, max_tokens=None,
                     provider=None, api_key=None, base_url=None):
        self.calls.append({"messages": list(messages)})
        for piece in self.pieces:
            yield piece


class _StubRouter:
    """按需返回固定路由结论的替身（测「结论有没有被消费」，不测路由器本身）。"""

    def __init__(self, decision: RouteDecision | None = None, *, fail: Exception | None = None):
        self.decision = decision
        self.fail = fail
        self.requests: list[RouteRequest] = []

    async def route(self, request: RouteRequest) -> RouteDecision:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        assert self.decision is not None
        return self.decision


def _decision(label: str | None = LAYER_VECTOR, *, layer: str = LAYER_VECTOR,
              confidence: float = 0.9, pause: bool = False,
              question: str = "", notes: tuple[str, ...] = ()) -> RouteDecision:
    return RouteDecision(
        user_id="u1", session_id="s1", trace_id="t1", layer=layer, label=label,
        confidence=confidence, require_clarification=pause,
        clarifying_question=question,
        clarification_category="multi_intent" if pause else "",
        degraded=bool(notes), degradation_notes=notes)


def _collect(orchestrator, query: str, *, user_id: str = "u_test_member",
             tier: access.AccessTier = access.AccessTier.MEMBER) -> list[dict]:
    async def run():
        return [event.as_dict() async for event in orchestrator.stream(
            user_id=user_id, query=query, tier=tier)]
    return asyncio.run(run())


def _stored_events(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]


# --------------------------------------------------------------------------- #
# 一、装配：唯一入口 + 开关开着必须真装上
# --------------------------------------------------------------------------- #
def test_create_app_actually_constructs_the_intent_router(settings, redis_client):
    """E-77 的正向守护：装配出来的 App 必须持有路由，且三层真在管道里。"""
    app = create_app(settings, redis_client, chat_gateway=_Gateway())

    assert app.state.intent_router is not None
    assert app.state.chat_orchestrator.intent_router is app.state.intent_router
    assert app.state.intent_router.layer_names == (LAYER_HARD_RULE, "jev", LAYER_VECTOR)


def test_orchestrator_refuses_to_build_without_router_when_switch_is_on(settings):
    """开关开着却没装 → 直接报错。**静默退化比缺功能更坏**（E-77）。"""
    with pytest.raises(ValueError, match="未装配意图路由"):
        build_chat_orchestrator(gateway=_Gateway(), search=lambda q, u: [],
                                settings=settings, intent_router=None)


def test_orchestrator_allows_missing_router_when_switch_is_off(settings):
    """显式关掉是合法的：那时「没有路由」是被选择的，不是被遗忘的。"""
    off = settings.model_copy(update={"intent_router_enable": False})
    orchestrator = build_chat_orchestrator(gateway=_Gateway(), search=lambda q, u: [],
                                           settings=off, intent_router=None)
    assert orchestrator.intent_router is None


def test_wired_layers_are_a_subsequence_of_the_frozen_five():
    """表 6 冻结的是**层数与顺序**；只接三层不等于改了顺序。"""
    names = (LAYER_HARD_RULE, "jev", LAYER_VECTOR)
    assert names == tuple(sorted(names, key=LAYERS.index))
    assert set(names) <= set(LAYERS)


def test_describe_reports_three_wired_and_two_skipped(settings):
    view = wiring.describe(settings)
    assert view["layers_total"] == 5
    assert view["layers_wired"] == [LAYER_HARD_RULE, "jev", LAYER_VECTOR]
    assert set(view["layers_skipped"]) == {"vertical_model", "llm_fallback"}
    # 未标定这件事必须能被读出来，不能只写在注释里
    assert view["vector_threshold_calibrated"] is False


# --------------------------------------------------------------------------- #
# 二、L1 从 planner 派生：不得出现第二份正则
# --------------------------------------------------------------------------- #
def test_hard_rules_agree_with_planner_classification():
    """同一条句子，路由 L1 与 planner.classify 必须同分类 —— 否则正则在漂移。

    例外三类，原因是结构性的、不是漏测：
    - `multi_intent`：要「并列连接词 + 两类不同诉求」，单条正则表达不了；
    - `small_talk`：要「长度上限 + 寒暄词 + 无实体」，同样是组合条件；
    - `unknown`：本来就没命中，没有可比的结论。
    """
    layer = HardRuleLayer(wiring.build_hard_rules())
    exceptions = {QueryCategory.MULTI_INTENT, QueryCategory.SMALL_TALK,
                  QueryCategory.UNKNOWN}

    samples = (
        "帮我看下订单号 20231015008 到哪了",
        "错误码 E1024 是什么意思",
        "这两个套餐有什么区别",
        "帮我推荐一个适合学生的套餐",
        "未成年人能不能注册账号",
        "怎么修改绑定的手机号",
        "我上次说的那个需求你还有吗",
    )
    for query in samples:
        category, _why, _signals = classify(query)
        assert category not in exceptions, f"样例设计有误：{query} 命中 {category}"
        verdict = asyncio.run(layer.evaluate(RouteRequest(
            user_id="u1", session_id="s1", utterance=query, labels=wiring.INTENT_LABELS)))
        assert verdict is not None, f"{query} 在 L1 未命中，与 planner 不一致"
        assert verdict.label == category.value, (
            f"{query}: L1={verdict.label} vs planner={category.value}")


def test_hard_rule_priority_follows_signal_table_order():
    rules = wiring.build_hard_rules()
    prios = [r.priority for r in rules]
    assert prios == sorted(prios, reverse=True), "优先级必须严格递减"
    # 表首是 personal_recall（第一人称指代），所以它的优先级最高
    assert rules[0].label == QueryCategory.PERSONAL_RECALL.value
    assert {r.label for r in rules} <= set(wiring.INTENT_LABELS)


def test_exemplars_cover_every_label_without_duplicates():
    assert set(wiring.EXEMPLARS) == set(wiring.INTENT_LABELS)
    seen: set[str] = set()
    for label, samples in wiring.EXEMPLARS.items():
        assert len(samples) >= 5, f"{label} 的示例句太少，向量层会不稳"
        for text in samples:
            assert text.strip(), f"{label} 里有空示例"
            assert text not in seen, f"示例句重复：{text}"
            seen.add(text)


def test_intent_labels_are_derived_from_the_same_taxonomy_as_chat():
    """chat 侧另有一份派生（避免让检索层依赖装配模块），两处必须逐字相同。"""
    assert chat_mod.INTENT_LABELS_FROM_TAXONOMY == wiring.INTENT_LABELS


# --------------------------------------------------------------------------- #
# 三、L3：懒加载、冷启动不拖垮请求、失败只报一次
# --------------------------------------------------------------------------- #
def test_assembly_does_not_block_on_model_load(settings):
    """装配必须**立即返回**，模型加载丢给后台线程。

    `create_app` 会被几百个用例调用；若装配期同步加载 BGE（实测 6.9s + 建索引 2.8s），
    单次测试就会从秒级变成分钟级，而且线上启动会被拖住 10 秒。
    """
    calls: list[float] = []

    def slow_factory():
        calls.append(time.perf_counter())
        time.sleep(0.4)
        return HashingEmbedder(dim=64)

    started = time.perf_counter()
    router = wiring.build_intent_router(settings, embedder_factory=slow_factory)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2, f"装配被模型加载阻塞了 {elapsed:.2f}s"
    vector = [l for l in router.layers if l.name == LAYER_VECTOR][0]
    deadline = time.time() + 5
    while vector._layer is None and time.time() < deadline:
        time.sleep(0.02)
    assert vector._layer is not None, f"预热失败：{vector._error}"
    assert calls and calls[0] >= started, "预热必须在装配之后由后台线程触发"


def test_vector_layer_missing_model_degrades_and_says_why(settings):
    """模型缺失：降级而不是报错，但降级原因必须进 degradation_notes。"""
    def boom():
        raise RouteError("向量层模型缺失：models/bge-small-zh-v1.5")

    router = wiring.build_intent_router(settings, embedder_factory=boom)
    decision = asyncio.run(router.route(RouteRequest(
        user_id="u1", session_id="s1", utterance="你好呀朋友",
        labels=wiring.INTENT_LABELS)))
    assert decision.label is None
    assert any("向量层不可用" in n for n in decision.degradation_notes), \
        decision.degradation_notes
    # L1 仍然工作：降级不是整体失效
    hit = asyncio.run(router.route(RouteRequest(
        user_id="u1", session_id="s1", utterance="订单号 20231015008 到哪了",
        labels=wiring.INTENT_LABELS)))
    assert hit.layer == LAYER_HARD_RULE and hit.label == "identity_lookup"


# --------------------------------------------------------------------------- #
# 四、影子旁路：不得给请求加延迟
# --------------------------------------------------------------------------- #
class _SlowJev:
    """模型调用需要 0.6s 的替身，用来证明影子没挡住主链路。"""

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, *, user_id, question, allowed_labels, model=None):
        self.calls += 1
        await asyncio.sleep(0.6)
        from ai_butler.infra.llm_gateway import JevDecision

        return JevDecision("recommendation", 0.9, "jev", shadow=True)


def test_shadow_mode_is_off_the_request_path():
    """影子态下 `evaluate` 必须立刻返回 —— 原实现会 await 完整模型调用。"""
    recorded = []
    layer = JevLayer(_SlowJev(), on_shadow=recorded.append, shadow=True)

    async def run():
        started = time.perf_counter()
        verdict = await layer.evaluate(RouteRequest(
            user_id="u1", session_id="s1", utterance="随便问问",
            labels=wiring.INTENT_LABELS))
        elapsed = time.perf_counter() - started
        await layer.tasks.drain()
        return verdict, elapsed

    verdict, elapsed = asyncio.run(run())
    assert verdict.accepted is False and verdict.shadow is True
    assert elapsed < 0.2, f"影子仍在主链路上等待：{elapsed:.2f}s"
    assert len(recorded) == 1 and recorded[0].label == "recommendation"


def test_shadow_probe_failure_does_not_surface():
    """影子探测自己炸了，不能影响主链路，也不能起未处理异常。"""
    class _Broken:
        async def decide(self, **_kwargs):
            raise RuntimeError("模型挂了")

    layer = JevLayer(_Broken(), on_shadow=lambda d: None, shadow=True)

    async def run():
        verdict = await layer.evaluate(RouteRequest(
            user_id="u1", session_id="s1", utterance="随便问问",
            labels=wiring.INTENT_LABELS))
        await layer.tasks.drain()
        return verdict

    assert asyncio.run(run()).accepted is False


def test_router_drain_shadow_counts_finished_probes():
    recorded = []
    router = IntentRouter([JevLayer(_SlowJev(), on_shadow=recorded.append, shadow=True)])

    async def run():
        for _ in range(3):
            await router.route(RouteRequest(user_id="u1", session_id="s1",
                                            utterance="随便问问",
                                            labels=wiring.INTENT_LABELS))
        return await router.drain_shadow()

    assert asyncio.run(run()) == 3
    assert len(recorded) == 3


# --------------------------------------------------------------------------- #
# 五、Jev 的两处实现修复（E-78 截断 / E-80 澄清误报）
# --------------------------------------------------------------------------- #
class _RecordingGateway:
    def __init__(self, text: str) -> None:
        self.text = text
        self.kwargs: list[dict] = []

    async def chat(self, *, user_id, messages, model=None, max_tokens=None,
                   rate_limit=None, rate_bucket=None, temperature=None):
        self.kwargs.append({"max_tokens": max_tokens, "temperature": temperature})
        from ai_butler.infra.llm_gateway import ChatResult

        return ChatResult(text=self.text, provider="fake", model="fake", latency_ms=1)


def _living_settings(settings, **extra) -> Settings:
    """非影子态的基准。**不透传 jev_allow_clarify**，让默认值真的是默认值。"""
    base = {"jev_shadow_mode": False, "jev_confidence_threshold": 0.82}
    base.update(extra)
    return settings.model_copy(update=base)


def test_jev_max_tokens_leaves_room_for_echoing_the_label_list(settings):
    """E-78：64 token 时模型复述候选标签会把 JSON 截断，解析全灭。"""
    payload = json.dumps({"label": "fact_question", "confidence": 0.93,
                          "candidate_labels": ["fact_question", "procedure"],
                          "require_clarification": 0,
                          "clarification_category": ""}, ensure_ascii=False)
    gateway = _RecordingGateway(payload)
    client = JevClient(gateway, _living_settings(settings, jev_max_tokens=256))
    decision = asyncio.run(client.decide(user_id="u1", question="营业时间几点",
                                         allowed_labels=wiring.INTENT_LABELS))

    assert gateway.kwargs[0]["max_tokens"] == 256
    assert settings.jev_max_tokens == 256
    assert decision.accepted and decision.label == "fact_question"


def test_model_clarification_is_trusted_by_default(settings):
    """JEV-003 是冻结不变量，**默认必须采信**模型的澄清请求。

    实测依据：云端 deepseek-chat 的 16 次澄清里有 13 条标签本来就对，
    且集中在 multi_intent(7/7) 与 unknown(6/6) —— 正是该问的两类；
    关掉只多 1 个用例。这点收益不足以偏离冻结项，所以默认 True。
    """
    payload = json.dumps({"label": "unknown", "confidence": 0.93,
                          "candidate_labels": ["unknown", "multi_intent"],
                          "require_clarification": 1,
                          "clarification_category": "multi_intent"}, ensure_ascii=False)
    default = _living_settings(settings)
    assert default.jev_allow_clarify is True
    decision = asyncio.run(JevClient(_RecordingGateway(payload), default).decide(
        user_id="u1", question="帮我看看", allowed_labels=wiring.INTENT_LABELS))

    assert decision.require_clarification is True
    assert decision.clarification_category == "multi_intent"


def test_model_clarification_can_be_turned_off_for_weak_models(settings):
    """弱模型（本机 3B 实测 6 次澄清 6 次错）可以关掉采信，留作按模型配置。"""
    payload = json.dumps({"label": "unknown", "confidence": 0.93,
                          "candidate_labels": ["unknown", "multi_intent"],
                          "require_clarification": 1,
                          "clarification_category": "multi_intent"}, ensure_ascii=False)
    off = _living_settings(settings, jev_allow_clarify=False)
    decision = asyncio.run(JevClient(_RecordingGateway(payload), off).decide(
        user_id="u1", question="帮我看看", allowed_labels=wiring.INTENT_LABELS))

    assert decision.require_clarification is False, "关掉采信后仍带澄清字段，等于开关无效"
    assert decision.clarification_category == ""
    assert decision.source == "fallback"
    assert "jev_allow_clarify" in decision.reason


def test_jev_calls_are_deterministic(settings):
    """判定节点必须 temperature=0：否则同一条句子两次给出不同标签，评测无从比较。"""
    payload = json.dumps({"label": "fact_question", "confidence": 0.93,
                          "candidate_labels": [], "require_clarification": 0,
                          "clarification_category": ""}, ensure_ascii=False)
    gateway = _RecordingGateway(payload)
    asyncio.run(JevClient(gateway, _living_settings(settings)).decide(
        user_id="u1", question="营业时间几点", allowed_labels=wiring.INTENT_LABELS))

    assert gateway.kwargs[0]["temperature"] == 0.0


# --------------------------------------------------------------------------- #
# 六、路由结论必须被 planner 消费（且只补空缺）
# --------------------------------------------------------------------------- #
def test_router_label_fills_the_gap_left_by_rules():
    category, why, _ = classify("有没有便宜点的", intent_layer=LAYER_VECTOR,
                                intent_label="recommendation")
    assert category is QueryCategory.RECOMMENDATION
    assert "五层意图路由" in why and LAYER_VECTOR in why


def test_router_label_never_overrides_a_deterministic_hit():
    """概率结论不得覆盖确定性结论：否则同一条查询会时而查 A 时而查 B。"""
    category, why, _ = classify("公司年假几天", intent_layer=LAYER_VECTOR,
                                intent_label="comparison")
    assert category is QueryCategory.FACT_QUESTION
    assert "五层意图路由" not in why


def test_unknown_label_from_router_stays_unknown():
    category, why, _ = classify("随便说说", intent_layer=LAYER_VECTOR,
                                intent_label="unknown")
    assert category is QueryCategory.UNKNOWN
    assert "unknown" in why


def test_out_of_taxonomy_label_is_not_guessed():
    """标签越界时不能硬塞进某个分类，要按未登记处理并留痕。"""
    category, why, signals = classify("随便说说", intent_layer=LAYER_VECTOR,
                                      intent_label="book_hotel")
    assert category is QueryCategory.UNKNOWN
    assert "未登记" in why and "intent_label_invalid" in signals


# --------------------------------------------------------------------------- #
# 七、端到端：编排器真的用了路由结论
# --------------------------------------------------------------------------- #
def _orchestrator(settings, router, *, gateway=None, search=None):
    return build_chat_orchestrator(
        gateway=gateway or _Gateway(), search=search or (lambda q, u: []),
        settings=settings, intent_router=router)


def test_chat_consumes_the_router_label_and_reports_the_layer(settings):
    router = _StubRouter(_decision("recommendation"))
    events = _collect(_orchestrator(settings, router), "有没有便宜点的")

    plan_event = _stored_events(events, "plan")[0]
    assert plan_event["data"]["category"] == "recommendation"
    assert plan_event["data"]["intent"]["layer"] == LAYER_VECTOR
    assert plan_event["data"]["intent"]["wired"] is True
    assert "retrieval.hybrid_rerank" in plan_event["data"]["tools"]

    done = _stored_events(events, "done")[-1]
    assert done["data"]["intent_layer"] == LAYER_VECTOR
    assert done["data"]["intent_label"] == "recommendation"
    assert done["data"]["intent_wired"] is True
    # 候选标签集要**原样送给路由**，否则路由的枚举约束形同虚设
    assert router.requests[0].labels == wiring.INTENT_LABELS


def test_chat_keeps_deterministic_category_when_rules_hit(settings):
    router = _StubRouter(_decision("comparison"))
    events = _collect(_orchestrator(settings, router), "公司年假几天")

    plan_event = _stored_events(events, "plan")[0]
    assert plan_event["data"]["category"] == "fact_question"
    assert "命中信号" in plan_event["data"]["decided_by"]


def test_chat_turns_must_pause_into_a_clarify_turn(settings):
    """JEV-003 在链路上的落点：暂停分发，不进检索、不出 chunk。"""
    router = _StubRouter(_decision(None, layer="jev", pause=True,
                                   question="您这次的请求里包含多个意图，请问要先处理哪一个？"))
    events = _collect(_orchestrator(settings, router), "退款怎么弄另外发票怎么开")

    assert _stored_events(events, "clarify"), "必须发出 clarify 事件"
    assert not _stored_events(events, "chunk"), "暂停分发后不应继续回答"
    done = _stored_events(events, "done")[-1]
    assert done["data"]["clarified"] is True
    assert done["data"]["source"] == "intent_router"


def test_router_crash_degrades_but_still_answers(settings):
    router = _StubRouter(fail=RuntimeError("路由炸了"))
    events = _collect(_orchestrator(settings, router), "公司年假几天")

    done = _stored_events(events, "done")[-1]
    assert any("意图路由失败" in n for n in done["data"]["notes"])
    assert _stored_events(events, "chunk"), "路由故障不得让用户拿不到回答"
    assert done["data"]["intent_wired"] is True
    assert done["data"]["intent_layer"] is None


def test_input_guard_block_becomes_an_explicit_error(settings):
    """风控拒绝要单列错误码：混成「回答失败」会让用户反复重试同一种说法。"""
    router = _StubRouter(fail=InputGuardError("输入未通过风控：编码混淆"))
    events = _collect(_orchestrator(settings, router), "公司年假几天")

    errors = _stored_events(events, "error")
    assert errors and errors[0]["data"]["code"] == "input_blocked"
    assert not _stored_events(events, "chunk")


def test_unwired_orchestrator_reports_no_router(settings):
    """路由关掉时不能装作「判不出」——要说出「没接」。"""
    off = settings.model_copy(update={"intent_router_enable": False})
    events = _collect(_orchestrator(off, None), "公司年假几天")

    done = _stored_events(events, "done")[-1]
    assert done["data"]["intent_wired"] is False
    assert any("意图路由未装配" in n for n in done["data"]["notes"])


def test_guest_tier_does_not_get_private_tools_even_if_router_says_so(settings):
    """路由只是分类建议，**权限仍由 B 层裁剪**：私有库对访客照样被剔除。"""
    router = _StubRouter(_decision("personal_recall"))
    events = _collect(_orchestrator(settings, router), "那个订单怎么样了",
                      user_id="guest:abc", tier=access.AccessTier.GUEST)

    plan_event = _stored_events(events, "plan")[0]
    dropped = [d["tool"] for d in plan_event["data"]["dropped_tools"]]
    assert "vault.search" in dropped
