"""L4 垂类派发（U110，落地 U98）的守护测试 + TourAgent 生产装配点（填 E-86）。

分三层：
- L4 决策模型与注册表：关键词+结构特征打分器的解析、注册表、派发层；
- **标定守护（核心）**：旅游样本必须 ≥ 阈值、非旅游样本必须 < 阈值——
  U03 要求阈值在验证集上标定，本首版用可解释打分器，但「分离度」仍由样本钉住；
- TourAgent 生产装配点（`build_tour_agent`）：注入真实端口、端到端跑通一次行程。
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agents.base import resolve
from ai_butler.agents.state import new_state
from ai_butler.core.config import Settings
from ai_butler.scheduler.graph import DispatchPlan, SceneScheduler
from ai_butler.scheduler.intent_wiring import build_intent_router, describe
from ai_butler.scheduler.router import (
    LAYER_VERTICAL,
    IntentRouter,
    RouteRequest,
)
from ai_butler.scheduler.vertical import (
    DEFAULT_VERTICAL_THRESHOLD,
    VerticalDispatchLayer,
    VerticalDispatcher,
    VerticalMeta,
    VerticalRegistry,
    build_tour_meta,
    build_vertical_model,
    default_vertical_registry,
)
from ai_butler.verticals.tour.live_routing import Leg
from ai_butler.verticals.tour.sources import SOURCE_KINDS, CollectedFact
from ai_butler.verticals.tour.wiring import build_tour_agent


# --------------------------------------------------------------------------- #
# 标定样本（U03：阈值由样本分布钉住，不是拍脑袋）
# --------------------------------------------------------------------------- #
TRAVEL_POSITIVES = [
    "帮我规划一个周末去北京的旅游行程",
    "推荐几个上海周边的自驾游路线和景点",
    "暑假想去云南度假，给个攻略",
    "杭州西湖一日游怎么安排",
    "我想去三亚旅游，安排一下酒店和门票",
]
TRAVEL_NEGATIVES = [
    "今天天气怎么样",
    "帮我订个外卖",
    "打开客厅的灯",
    "播放一首周杰伦的歌",
    "帮我查一下快递到哪了",
]


# --------------------------------------------------------------------------- #
# 台账：U98 曾被标 RESOLVED 但代码从未落地（声明≠能力），U110 才真正落地
# --------------------------------------------------------------------------- #
def test_u110_registered_and_u98_carries_correction():
    from ai_butler.core import decisions

    u110 = decisions.get("U110")
    assert u110 is not None and u110.status == decisions.RESOLVED
    u98 = decisions.get("U98")
    assert u98 is not None
    # 更正尾注留在台账里：不抹掉「曾把声明当成能力」这件事
    assert "诚实更正" in u98.resolution


# --------------------------------------------------------------------------- #
# 注册表与模型
# --------------------------------------------------------------------------- #
def test_default_registry_contains_tour():
    reg = default_vertical_registry()
    assert "tour" in reg.keys()
    assert reg.get("tour").display == "旅游规划"
    assert reg.get("tour").factory.endswith("build_tour_agent")


def test_tour_meta_factory_resolves_to_assembly_point():
    meta = build_tour_meta()
    assert meta.factory.endswith("build_tour_agent")


def test_model_scores_travel_above_threshold():
    model = build_vertical_model()
    for utt in TRAVEL_POSITIVES:
        outcome = model.predict(utt, ("tour",))
        assert outcome is not None, f"旅游样本未被识别：{utt!r}"
        key, score = outcome
        assert key == "tour"
        assert score >= DEFAULT_VERTICAL_THRESHOLD, (
            f"旅游样本分数过低 {score}：{utt!r}")


def test_model_rejects_non_travel_below_threshold():
    model = build_vertical_model()
    for utt in TRAVEL_NEGATIVES:
        outcome = model.predict(utt, ("tour",))
        # 非旅游样本：要么判不出（None），要么分数低于阈值——绝不能误派发
        assert outcome is None or outcome[1] < DEFAULT_VERTICAL_THRESHOLD, (
            f"非旅游样本被误派发 {outcome}：{utt!r}")


def test_model_ignores_unregistered_labels():
    """候选标签里没有已注册垂类时，退化为对全部已注册垂类打分（仍是 None）。"""
    model = build_vertical_model()
    # 候选标签是无关的检索分类，与注册表不相交 → 退化为全注册表打分，
    # 这些负面样本不含旅游关键词 → None。
    assert model.predict("打开客厅的灯", ("fact_question", "small_talk")) is None


# --------------------------------------------------------------------------- #
# 派发层（L4 包装）
# --------------------------------------------------------------------------- #
def _dispatch(utterance: str, *, travel: bool) -> object:
    layer = VerticalDispatchLayer(build_vertical_model())
    request = RouteRequest(user_id="u", session_id="s", utterance=utterance,
                           labels=("tour",))
    return asyncio.run(layer.evaluate(request))


def test_dispatch_layer_accepts_travel():
    verdict = _dispatch("帮我规划北京三日游", travel=True)
    assert verdict is not None and verdict.accepted is True
    assert verdict.label == "tour"


def test_dispatch_layer_rejects_non_travel():
    verdict = _dispatch("打开客厅的灯", travel=False)
    assert verdict is None or verdict.accepted is False


# --------------------------------------------------------------------------- #
# 主路由接线（默认不接；开关打开才接，且保持冻结顺序的子序列）
# --------------------------------------------------------------------------- #
def test_describe_shows_l4_not_wired_by_default():
    d = describe(Settings())
    assert d["vertical_router_enable"] is False
    assert "未接线" in d["layers_skipped"][LAYER_VERTICAL]
    assert d["vertical_threshold_calibrated"] is False


def test_router_wires_l4_when_enabled():
    settings = Settings(vertical_router_enable=True)
    router = build_intent_router(settings)
    assert router is not None
    assert LAYER_VERTICAL in router.layer_names


# --------------------------------------------------------------------------- #
# TourAgent 生产装配点（填 E-86）
# --------------------------------------------------------------------------- #
class FakeCollector:
    """测试替身：照实返回全部类别，不触发网络。"""

    def __init__(self, *, fail_on: tuple[str, ...] = (), low: bool = False) -> None:
        self.fail_on = fail_on
        self.low = low
        self.calls: list[str] = []

    def fetch(self, kind, *, subject):
        self.calls.append(kind)
        if kind in self.fail_on:
            return None
        return CollectedFact(kind=kind, subject=subject,
                            payload={"value": kind}, provider="fake",
                            evidence_id=f"ev-{kind}", low_confidence=self.low)


class FakeRouting:
    """测试替身：固定一段真实路线事实。"""

    name = "fake"

    async def route(self, origin, destination, *, mode):
        return Leg(origin=origin, destination=destination, mode=mode,
                   distance_km=5.0, minutes=10, provider="fake", estimated=True)


def test_build_tour_agent_default_wires_ports():
    """默认装配点接上了真实采集与路线端口（不再只活在测试里）。"""
    agent = build_tour_agent()
    assert agent.collector is not None
    assert agent.routing is not None
    assert agent.optimizer is None      # 路线优化器用内置贪心


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


def _state(**extra):
    state = new_state(user_id="user-A", session_id="s1")
    state.update(extra)
    return state


_CANDIDATES = [
    {"stop_id": "s1", "name": "西湖", "open_minute": 480, "close_minute": 1080,
     "dwell_minutes": 180, "ticket_price": 0, "latitude": 30.25, "longitude": 120.15},
    {"stop_id": "s2", "name": "灵隐寺", "open_minute": 420, "close_minute": 1020,
     "dwell_minutes": 120, "ticket_price": 75, "latitude": 30.24, "longitude": 120.10},
]
_PARAMS = {"destination": "杭州", "start_date": "2026-11-01", "days": 2, "party_size": 2}


def test_build_tour_agent_runs_full_trip_with_injected_fakes():
    """装配点产出的 Agent 能端到端跑通一次行程（E-86 反例：装配之后真能跑）。"""
    agent = build_tour_agent(collector=FakeCollector(), routing=FakeRouting())
    out = _call(agent, _state(task_kind="trip_plan", task_params=_PARAMS,
                              candidate_stops=_CANDIDATES))
    result = out["tour_result"]
    assert result["handled"] is True
    assert result["plan"]["stops"] == ["西湖", "灵隐寺"]
    assert result["payment_list"]["executed"] is False
    # 采集底座被真实接上：source_pages 数量 = 采集类别数
    assert len(result["source_pages"]) == len(SOURCE_KINDS)


# --------------------------------------------------------------------------- #
# 派发器（U110 闭环：L4 命中 → 真有执行者）
# --------------------------------------------------------------------------- #
def test_dispatcher_resolves_tour_through_real_factory():
    """不注入任何替身，`resolve("tour")` 也必须装出真 Agent —— 否则又是一次 E-86。"""
    dispatcher = VerticalDispatcher()
    assert dispatcher.has("tour") is True
    agent = dispatcher.resolve("tour")
    assert agent is not None, f"垂类装配失败：{dispatcher.note_error('tour')}"
    assert agent.collector is not None and agent.routing is not None
    assert dispatcher.resolve("tour") is agent          # 缓存：不重复装配


def test_dispatcher_records_error_instead_of_silence():
    """装配点解析不出东西时，失败必须可见（`errors` 会被混进 degradation）。"""
    registry = VerticalRegistry([VerticalMeta(key="broken", display="坏垂类",
                                               factory="no.such.module:build")])
    dispatcher = VerticalDispatcher(registry)
    assert dispatcher.resolve("broken") is None
    assert "no.such.module" in dispatcher.note_error("broken")
    assert dispatcher.has("tour") is False


def test_dispatcher_bind_overrides_factory():
    dispatcher = VerticalDispatcher()
    sentinel = build_tour_agent(collector=FakeCollector())
    dispatcher.bind("tour", sentinel)
    assert dispatcher.resolve("tour") is sentinel


# --------------------------------------------------------------------------- #
# 端到端：L4 命中 → SceneScheduler 真的把任务交给垂类（U110 全链路）
# --------------------------------------------------------------------------- #
def _vertical_scheduler(*, verticals):
    return SceneScheduler(
        router=IntentRouter([VerticalDispatchLayer(build_vertical_model())]),
        agents={}, plan=DispatchPlan(chains={}), verticals=verticals)


def test_scheduler_dispatches_tour_to_vertical_agent():
    """垂类派发的端到端：意图 = tour → 执行者是 tour 垂类，不是阶段链。"""
    dispatcher = VerticalDispatcher()
    dispatcher.bind("tour", build_tour_agent(collector=FakeCollector(),
                                             routing=FakeRouting()))
    scheduler = _vertical_scheduler(verticals=dispatcher)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1",
        utterance="帮我规划杭州两日游行程攻略", labels=("tour",)))
    assert result.intent == "tour"
    assert result.layer == LAYER_VERTICAL
    assert result.dispatched == ["vertical:tour"]
    assert result.failed == []
    # 缺行程参数是**本职范围内失败**：如实要求补参数并留在垂类内处置（表 12），
    # 既不回退、也不假装规划成功。
    outcome = result.agent_results["vertical:tour"]["tour_result"]
    assert outcome["handled"] is False
    assert outcome["fallback"]["failure"] == "missing_parameter"
    assert outcome["ask"]


def test_scheduler_without_verticals_keeps_legacy_behavior():
    """不装配垂类派发器时行为零变化：tour 只是个普通意图，不会凭空冒出执行者。"""
    scheduler = _vertical_scheduler(verticals=None)
    result = asyncio.run(scheduler.run(
        user_id="user-A", session_id="s1",
        utterance="帮我规划杭州两日游行程攻略", labels=("tour",)))
    assert result.intent == "tour"
    assert result.dispatched == []
    assert result.agent_results == {}
