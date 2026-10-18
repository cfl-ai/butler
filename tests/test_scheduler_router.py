"""P3 五层意图路由测试（含 JEV-003）。

计划书依据：三、P3 五层路由 / 多意图必须向用户确认 / 全链路降级
            + 一、项目概述「Jev 不输出自由文本」+ 八、multi_intent_auto_resolve=False
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.infra.llm_gateway import (
    CLARIFY_MULTI_INTENT,
    CLARIFY_OUT_OF_SCOPE,
    CLARIFICATION_CATEGORIES,
    JevClient,
    JevDecision,
)
from ai_butler.scheduler.router import (
    CLARIFICATION_TEMPLATES,
    DEFAULT_LAYER_TIMEOUT_SECONDS,
    LAYER_HARD_RULE,
    LAYER_JEV,
    LAYER_LLM,
    LAYER_VECTOR,
    LAYER_VERTICAL,
    LAYERS,
    HardRule,
    HardRuleLayer,
    IntentRouter,
    JevLayer,
    LayerVerdict,
    LLMFallbackLayer,
    RouteError,
    RouteRequest,
    VectorLayer,
    VerticalModelLayer,
    default_layers,
    render_clarification,
)

LABELS = ("book_hotel", "book_flight", "ask_weather", "cancel_order")


@pytest.fixture(scope="module")
def loop():
    """模块级事件循环。

    必须 set_event_loop：同进程内其他用例调用 asyncio.run() 后会把当前事件循环置空，
    若这里只 new_event_loop() 而不设置，asyncpg 在某些代码路径上会去取「当前」循环，
    产生只在整包连跑时才冒头的偶发失败（Event loop is closed / 连接挂在别的循环上）。
    """
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


def _req(text="帮我订个酒店", labels=LABELS):
    return RouteRequest(user_id="user-A", session_id="sess-1", utterance=text,
                        labels=tuple(labels))


def _run(loop, coro):
    return loop.run_until_complete(coro)


# ------------------------------ 冻结契约 ------------------------------ #
def test_five_layers_order_is_frozen():
    """计划书三、P3 逐字顺序，不得变更。"""
    assert LAYERS == ("hard_rule", "jev", "vector", "vertical_model", "llm_fallback")
    assert (LAYER_HARD_RULE, LAYER_JEV, LAYER_VECTOR, LAYER_VERTICAL, LAYER_LLM) == LAYERS


def test_request_requires_identity_and_labels():
    with pytest.raises(RouteError):
        RouteRequest(user_id="", session_id="s", utterance="x", labels=LABELS)
    with pytest.raises(RouteError):
        RouteRequest(user_id="u", session_id="s", utterance="   ", labels=LABELS)
    with pytest.raises(RouteError) as ei:
        RouteRequest(user_id="u", session_id="s", utterance="x", labels=())
    assert "文档未定义项 #1" in str(ei.value), "标签集缺失必须指向台账，不得自行编造"


def test_router_rejects_unknown_or_duplicate_layers():
    with pytest.raises(RouteError):
        IntentRouter([HardRuleLayer([]), type("X", (), {"name": "sixth_layer",
                                                        "evaluate": None})()])
    with pytest.raises(RouteError):
        IntentRouter([HardRuleLayer([]), HardRuleLayer([])])


# ------------------------------ 第 1 层 ------------------------------ #
def test_hard_rule_hits_and_respects_priority(loop):
    layer = HardRuleLayer([
        HardRule(r"取消", "cancel_order", priority=1),
        HardRule(r"取消订单.*紧急", "cancel_order", priority=9, note="紧急通道"),
    ])
    verdict = _run(loop, layer.evaluate(_req("取消订单，紧急")))
    assert verdict.accepted and verdict.confidence == 1.0
    assert "紧急通道" in verdict.note


def test_hard_rule_ignores_labels_outside_candidate_set(loop):
    layer = HardRuleLayer([HardRule(r"取消", "cancel_order")])
    assert _run(loop, layer.evaluate(_req("取消", labels=("book_hotel",)))) is None


def test_hard_rule_rejects_bad_regex():
    with pytest.raises(RouteError):
        HardRule(pattern="(", label="x")


# ------------------------------ 第 2 层 ------------------------------ #
class _StubJev:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def decide(self, *, user_id, question, allowed_labels, model=None):
        self.calls.append((user_id, question))
        return self.decision


def test_jev_accepts_high_confidence(loop):
    layer = JevLayer(_StubJev(JevDecision("book_hotel", 0.93, "jev")))
    verdict = _run(loop, layer.evaluate(_req()))
    assert verdict.accepted and verdict.label == "book_hotel" and verdict.confidence == 0.93


def test_jev_low_confidence_falls_through(loop):
    layer = JevLayer(_StubJev(JevDecision("book_hotel", 0.4, "fallback", reason="置信不足")))
    verdict = _run(loop, layer.evaluate(_req()))
    assert verdict.accepted is False and "置信不足" in verdict.note


def test_jev_shadow_mode_records_but_does_not_decide(loop):
    """计划书三、P3：影子模式只记录不决策。"""
    recorded = []
    layer = JevLayer(_StubJev(JevDecision("book_hotel", 0.99, "fallback", shadow=True,
                                          reason="jev_shadow_mode=true")),
                     on_shadow=recorded.append)
    verdict = _run(loop, layer.evaluate(_req()))
    assert verdict.accepted is False and verdict.shadow is True
    assert len(recorded) == 1 and recorded[0].label == "book_hotel"


# ------------------------------ JEV-003 ------------------------------ #
def test_jev003_clarification_pauses_dispatch_and_lower_layers_are_never_consulted(loop):
    """**JEV-003 核心不变量**：Jev 判定必须澄清后，路由层暂停分发，
    且任何更低层都不得覆盖该结论（multi_intent_auto_resolve=False）。"""
    consulted = []

    class _RecordingLayer:
        name = LAYER_VECTOR

        async def evaluate(self, request):
            consulted.append(self.name)
            return LayerVerdict(accepted=True, label="book_flight", confidence=0.99)

    class _AlsoRecording:
        name = LAYER_LLM

        async def evaluate(self, request):
            consulted.append(self.name)
            return LayerVerdict(accepted=True, label="ask_weather", confidence=0.99)

    router = IntentRouter([
        JevLayer(_StubJev(JevDecision(
            "book_hotel", 0.66, "jev", require_clarification=True,
            clarification_category=CLARIFY_MULTI_INTENT,
            candidate_labels=("book_hotel", "book_flight")))),
        _RecordingLayer(), _AlsoRecording(),
    ])
    decision = _run(loop, router.route(_req("订酒店，另外看看航班")))

    assert decision.must_pause is True
    assert decision.layer == LAYER_JEV
    assert consulted == [], "澄清结论一旦成立，更低层不得被调用"
    assert decision.label == "book_hotel", "不得自动选优先级，只能如实回传主意图"


def test_jev003_clarifying_question_is_rendered_from_enum_not_from_model(loop):
    """decisions.U25：计划书禁止 Jev 输出自由文本，话术由系统按枚举模板渲染。"""
    router = IntentRouter([JevLayer(_StubJev(JevDecision(
        "book_hotel", 0.5, "jev", require_clarification=True,
        clarification_category=CLARIFY_MULTI_INTENT,
        candidate_labels=("book_hotel", "book_flight"))))])
    decision = _run(loop, router.route(_req()))

    assert decision.clarifying_question == render_clarification(
        CLARIFY_MULTI_INTENT, ("book_hotel", "book_flight"))
    assert "book_hotel" in decision.clarifying_question
    assert decision.clarifying_question.endswith("？")


def test_clarification_is_not_gated_by_confidence_threshold(loop):
    """澄清是安全动作：置信再低也必须停下来问，否则低置信时会漏掉冲突。"""
    router = IntentRouter([JevLayer(_StubJev(JevDecision(
        "book_hotel", 0.01, "jev", require_clarification=True,
        clarification_category=CLARIFY_OUT_OF_SCOPE, candidate_labels=())))])
    decision = _run(loop, router.route(_req()))
    assert decision.must_pause is True
    assert decision.clarification_category == CLARIFY_OUT_OF_SCOPE


def test_clarification_templates_cover_every_category():
    assert set(CLARIFICATION_TEMPLATES) == set(CLARIFICATION_CATEGORIES)
    assert "{candidates}" in CLARIFICATION_TEMPLATES[CLARIFY_MULTI_INTENT]


# ------------------------------ 第 3 层 ------------------------------ #
class _Embedder:
    name = "stub"
    dim = 4

    def __init__(self, table):
        self.table = table

    def encode(self, texts):
        return [self.table.get(t, [0.0, 0.0, 0.0, 0.0]) for t in texts]


def test_vector_layer_hits_when_similarity_passes(loop):
    emb = _Embedder({"订酒店": [1.0, 0.0, 0.0, 0.0], "查天气": [0.0, 1.0, 0.0, 0.0],
                     "帮我订个酒店": [1.0, 0.0, 0.0, 0.0]})
    layer = VectorLayer(emb, {"book_hotel": ["订酒店"], "ask_weather": ["查天气"]},
                        threshold=0.5)
    verdict = _run(loop, layer.evaluate(_req("帮我订个酒店")))
    assert verdict.accepted and verdict.label == "book_hotel"


def test_vector_layer_falls_through_below_threshold(loop):
    emb = _Embedder({"订酒店": [1.0, 0.0, 0.0, 0.0], "无关": [0.1, 0.9, 0.0, 0.0]})
    layer = VectorLayer(emb, {"book_hotel": ["订酒店"]}, threshold=0.9)
    verdict = _run(loop, layer.evaluate(_req("无关")))
    assert verdict.accepted is False and "阈值" in verdict.note


def test_vector_layer_requires_exemplars():
    with pytest.raises(RouteError):
        VectorLayer(_Embedder({}), {})


# ------------------------------ 第 4 层 ------------------------------ #
class _VerticalModel:
    name = "stub-bert-router"

    def __init__(self, outcome):
        self.outcome = outcome

    def predict(self, utterance, labels):
        return self.outcome


def test_vertical_model_hits_and_respects_threshold(loop):
    layer = VerticalModelLayer(_VerticalModel(("book_flight", 0.8)), threshold=0.55)
    assert _run(loop, layer.evaluate(_req())).accepted is True

    low = VerticalModelLayer(_VerticalModel(("book_flight", 0.3)), threshold=0.55)
    verdict = _run(loop, low.evaluate(_req()))
    assert verdict.accepted is False and "阈值" in verdict.note


def test_vertical_model_discards_out_of_set_label(loop):
    layer = VerticalModelLayer(_VerticalModel(("hack_the_planet", 0.99)))
    verdict = _run(loop, layer.evaluate(_req()))
    assert verdict.accepted is False and "候选集外" in verdict.note


# ------------------------------ 第 5 层 ------------------------------ #
class _Gateway:
    def __init__(self, text):
        self.text = text

    async def chat(self, **kwargs):
        return type("R", (), {"text": self.text})()


def test_llm_fallback_parses_strict_json(loop):
    layer = LLMFallbackLayer(_Gateway('{"label": "ask_weather", "confidence": 0.7}'))
    verdict = _run(loop, layer.evaluate(_req()))
    assert verdict.accepted and verdict.label == "ask_weather"


def test_llm_fallback_refuses_free_text_and_out_of_set(loop):
    for text in ("我觉得你想订酒店", '{"label": "nope", "confidence": 0.9}',
                 '{"label": "book_hotel", "confidence": 3}'):
        layer = LLMFallbackLayer(_Gateway(text))
        with pytest.raises(RouteError):
            _run(loop, layer.evaluate(_req()))


# ------------------------------ 降级与编排 ------------------------------ #
class _BoomLayer:
    name = LAYER_JEV

    async def evaluate(self, request):
        raise RuntimeError("Jev 服务不可用")


class _BoomVectorLayer:
    name = LAYER_VECTOR

    async def evaluate(self, request):
        raise RuntimeError("向量库不可用")


class _SlowLayer:
    name = LAYER_JEV

    async def evaluate(self, request):
        await asyncio.sleep(5)
        return None


def test_layer_failure_degrades_to_the_next_layer(loop):
    """计划书三、P3：任一环节失败自动降级，不阻断主流程。"""
    router = IntentRouter([_BoomLayer(),
                           VerticalModelLayer(_VerticalModel(("book_hotel", 0.9)))],
                          layer_timeout_seconds=1.0)
    decision = _run(loop, router.route(_req()))
    assert decision.label == "book_hotel" and decision.layer == LAYER_VERTICAL
    assert decision.degraded is True
    assert any("Jev 服务不可用" in n for n in decision.degradation_notes)


def test_layer_timeout_degrades_to_the_next_layer(loop):
    router = IntentRouter([_SlowLayer(),
                           VerticalModelLayer(_VerticalModel(("book_hotel", 0.9)))],
                          layer_timeout_seconds=0.05)
    decision = _run(loop, router.route(_req()))
    assert decision.label == "book_hotel"
    assert any("超时" in n for n in decision.degradation_notes)


def test_all_layers_failing_does_not_crash_the_main_flow(loop):
    router = IntentRouter([_BoomLayer(), _BoomVectorLayer()], layer_timeout_seconds=0.05)
    decision = _run(loop, router.route(_req()))
    assert decision.label is None and decision.resolved is False
    assert decision.degraded is True
    assert any("五层路由均未给出可用意图" in n for n in decision.degradation_notes)
    assert len(decision.attempts) == 2


def test_attempts_record_every_layer_with_timing(loop):
    router = IntentRouter([
        HardRuleLayer([HardRule(r"不匹配", "book_hotel")]),
        JevLayer(_StubJev(JevDecision("book_flight", 0.9, "jev"))),
    ])
    decision = _run(loop, router.route(_req()))
    assert [a.layer for a in decision.attempts] == [LAYER_HARD_RULE, LAYER_JEV]
    assert all(a.elapsed_ms >= 0 for a in decision.attempts)
    assert decision.degraded is False, "正常逐层尝试不算降级"
    assert decision.trace_id


def test_normal_hit_is_not_marked_degraded(loop):
    router = IntentRouter([HardRuleLayer([HardRule(r"订.*酒店", "book_hotel")])])
    decision = _run(loop, router.route(_req()))
    assert decision.label == "book_hotel"
    assert decision.degraded is False and decision.degradation_notes == ()


def test_default_layers_keeps_frozen_relative_order():
    layers = default_layers(
        hard_rules=[HardRule(r"订", "book_hotel")],
        jev_client=_StubJev(JevDecision("book_hotel", 0.9, "jev")),
        embedder=_Embedder({"订酒店": [1.0, 0.0, 0.0, 0.0]}),
        exemplars={"book_hotel": ["订酒店"]},
        vertical_model=_VerticalModel(("book_hotel", 0.9)),
        gateway=_Gateway('{"label": "book_hotel", "confidence": 0.9}'))
    assert [layer.name for layer in layers] == list(LAYERS)
    with pytest.raises(RouteError):
        default_layers()


def test_default_layer_timeout_is_sane():
    assert 0 < DEFAULT_LAYER_TIMEOUT_SECONDS <= 30
