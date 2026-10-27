"""主状态机的生产入口（U111）。

守四件事：
1. **入口闸**：只有说「行程/旅游」这类话才交给垂类 —— 「帮我订杭州的酒店」是交易意图，
   不能被拉去生成行程（L4 的关键词分里「酒店」权重同样高）；
2. **参数抽取**：用户说了目的地/天数/日期就要抽出来，抽不到才让垂类问；
   抽错了比抽不到更坏（会生成别人的行程）；
3. **如实呈现**：参数缺失 → 原样问用户；方案生成失败 → 说失败，不编景点；
   公交查不到按驾车估 → 必须标出来；
4. **接线**：`scene` 装了才走，且只在真答出来时抢答；非垂类命中一律退回既有链路。
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from ai_butler.retrieval.chat import ChatOrchestrator
from ai_butler.scheduler.scene_entry import (
    answer_via_scene,
    has_core_signal,
    render_tour_answer,
)
from ai_butler.verticals.tour.utterance import extract_trip_params

TODAY = date(2026, 10, 27)


# ------------------------------ 1. 入口闸 ------------------------------ #
def test_core_signal_gate_keeps_booking_out():
    assert has_core_signal("帮我规划杭州两日游行程")
    assert has_core_signal("去西湖怎么玩")
    assert not has_core_signal("帮我订杭州的酒店"), "订酒店是交易意图，不是行程规划"
    assert not has_core_signal("杭州明天天气怎么样")
    assert not has_core_signal("你好")


# ------------------------------ 2. 参数抽取 ------------------------------ #
def test_extract_params_from_natural_utterance():
    got = extract_trip_params("帮我规划杭州两日游行程，明天出发，2个人", today=TODAY)
    assert got == {"destination": "杭州", "days": 2, "party_size": 2,
                   "start_date": "2026-10-28"}


def test_extract_params_ignores_date_digits_as_days():
    """「11月1日到杭州」里的 `1日` 不是「一日游」—— 必须先抽日期再从文本里挖掉。"""
    got = extract_trip_params("11月1日到杭州旅游，两个人", today=TODAY)
    assert got["start_date"] == "2026-11-01"
    assert "days" not in got
    assert got["party_size"] == 2


def test_extract_params_strips_request_prefix_and_trip_suffix():
    assert extract_trip_params("帮我安排一下杭州三日游的行程", today=TODAY)["destination"] == "杭州"
    assert extract_trip_params("上海自驾游4天", today=TODAY)["destination"] == "上海"


def test_extract_params_returns_empty_when_nothing_is_clear():
    """抽不到就是抽不到：留空让垂类去问，绝不瞎猜一个目的地。"""
    assert extract_trip_params("我想出去玩", today=TODAY) == {}
    assert extract_trip_params("订酒店", today=TODAY) == {}


# ------------------------------ 3. 结果呈现 ------------------------------ #
def _handled_result() -> dict:
    return {
        "user_id": "u", "task_kind": "trip_plan", "handled": True,
        "plan": {"destination": "杭州", "start_date": "2026-10-28", "days": 2,
                 "party_size": 2, "stops": ["西湖", "灵隐寺"], "total_cost": 90.0,
                 "depart": "09:00", "end": "18:40", "depart_assumed": True},
        "time": {"depart": "09:00", "end": "18:40", "depart_assumed": True,
                 "timeline": [
                     {"name": "西湖", "arrive": "09:00", "leave": "10:30",
                      "dwell_minutes": 90, "travel_minutes": 0},
                     {"name": "灵隐寺", "arrive": "10:39", "leave": "12:09",
                      "dwell_minutes": 90, "travel_minutes": 9,
                      "travel_estimated": True}]},
        "route": {"basis": "osrm:car", "degraded_from": "transit", "estimate_only": False},
        "issues": ["灵隐寺 17:00 关门，已提前安排"],
        "source_pages": [{"kind": "weather"}, {"kind": "scenic"}],
    }


def test_render_handled_plan_with_timeline_and_sources():
    text, sources = render_tour_answer(_handled_result())
    assert "杭州 2 天行程" in text
    assert "09:00 出发" in text and "假定值" in text, "假定的出发时刻必须标出来"
    assert "1. 09:00 到 西湖" in text and "灵隐寺" in text
    assert "估计值" in text, "逐段是估计就得标估计"
    assert "90 元" in text
    assert "transit 路线查询不可用" in text, "公交降级成驾车必须说出来"
    assert "注意：灵隐寺" in text
    labels = [item["label"] for item in sources]
    assert "Open-Meteo 气象数据" in labels and "高德地图 POI" in labels
    for marker in ("*", "|", "**"):
        assert marker not in text, "答案里不许出现 Markdown 记号"


def test_render_missing_params_asks_instead_of_planning():
    text, sources = render_tour_answer({
        "handled": False, "ask": "还需要你补充：出行日期、出行人数",
        "note": "参数缺失属本职范围内失败"})
    assert text == "还需要你补充：出行日期、出行人数"
    assert sources == []


def test_render_failure_is_honest_and_never_invents_stops():
    text, _ = render_tour_answer({
        "handled": False, "note": "方案生成失败属本职范围内失败，降级提示而非回退",
        "failures": [{"kind": "weather", "reason": "地名解析不到坐标"}]})
    assert "方案生成失败" in text
    assert "未取到" in text and "地名解析不到坐标" in text
    assert "西湖" not in text, "取不到点就不许凭空写景点"


# ------------------------------ 4. 接线 ------------------------------ #
class _Verticals:
    def __init__(self, keys=("tour",)) -> None:
        self._keys = keys

    def keys(self):
        return self._keys

    def has(self, key: str) -> bool:
        return key in self._keys


class _Scheduler:
    """假主状态机。记录 run 的入参，验证「参数/候选点真的进了 State」。"""

    def __init__(self, payload=None, intent="tour", degradation=()) -> None:
        self.verticals = _Verticals()
        self._payload = payload
        self._intent = intent
        self._degradation = list(degradation)
        self.calls: list[dict] = []

    async def run(self, *, user_id, session_id, utterance, labels, extra_state=None):
        self.calls.append({"user_id": user_id, "session_id": session_id,
                           "utterance": utterance, "labels": tuple(labels),
                           "extra_state": dict(extra_state or {})})
        results = {}
        if self._payload is not None:
            # 子 Agent 返回的是**状态更新字典**，不是结果本身（真实形态）
            results[f"vertical:{self._intent}"] = {"tour_result": self._payload}
        return SimpleNamespace(intent=self._intent, layer="L4", confidence=0.9,
                               dispatched=[f"vertical:{self._intent}"],
                               degradation=list(self._degradation),
                               agent_results=results)


def _run_scene(scheduler, utterance, *, poi=None):
    async def go():
        return await answer_via_scene(
            scheduler=scheduler, user_id="u-1", session_id="s-1",
            utterance=utterance, labels=(), poi_search=poi)
    return asyncio.run(go())


def test_scene_returns_none_when_this_axis_should_not_take_over():
    assert _run_scene(None, "帮我规划杭州两日游") is None
    assert _run_scene(SimpleNamespace(verticals=None), "帮我规划杭州两日游") is None
    scheduler = _Scheduler(payload=_handled_result())
    assert _run_scene(scheduler, "帮我订杭州的酒店") is None
    assert scheduler.calls == [], "闸门不过就不该惊动主状态机"


def test_scene_passes_params_and_stops_into_state():
    scheduler = _Scheduler(payload=_handled_result())
    stops = [{"stop_id": "poi-1", "name": "西湖", "latitude": 30.2, "longitude": 120.1}]
    out = _run_scene(scheduler, "帮我规划杭州两日游，明天出发，2个人",
                     poi=lambda city: stops)
    assert out is not None and out.has_answer
    extra = scheduler.calls[0]["extra_state"]
    assert extra["task_params"]["destination"] == "杭州"
    assert extra["candidate_stops"] == stops
    assert "杭州 2 天行程" in out.text


def test_scene_skips_poi_lookup_when_params_are_incomplete():
    """参数没齐时垂类会先问，这时不该先花一次外部调用去取点。"""
    asked = object()
    scheduler = _Scheduler(payload={"handled": False, "ask": "还需要你补充：出行日期"})
    calls = []

    def poi(city):
        calls.append(city)
        return [{"stop_id": "poi-1", "name": "西湖"}]

    out = _run_scene(scheduler, "帮我规划杭州两日游", poi=poi)
    assert calls == []
    assert out is not None and "还需要你补充" in out.text


def test_scene_reports_failure_instead_of_pretending():
    scheduler = _Scheduler(payload=None, degradation=["垂类 tour 无可用执行者"])
    out = _run_scene(scheduler, "帮我规划杭州两日游")
    assert out is not None
    assert not out.has_answer
    assert "没有产出结果" in out.note and "无可用执行者" in out.note


class _Gateway:
    async def stream(self, *, user_id, messages, **kwargs):
        yield "常规答案"


def _collect(orchestrator, query):
    async def go():
        return [e async for e in orchestrator.stream(user_id="u-1", query=query)]
    return asyncio.run(go())


def test_chat_takes_scene_answer_and_emits_tools_then_chunk_then_done():
    class _Scene:
        async def __call__(self, **kwargs):
            text, sources = render_tour_answer(_handled_result())
            return SimpleNamespace(text=text, sources=sources, note="",
                                   detail={"units": [{"calls": []}], "scene": {}})

    orchestrator = ChatOrchestrator(_Gateway(), search=None, verify_answer=False,
                                    scene=_Scene())
    events = _collect(orchestrator, "帮我规划杭州两日游")
    types = [e.type for e in events]
    assert "tools" in types and types.index("tools") < types.index("chunk")
    done = events[-1]
    assert done.type == "done" and done.data["source"] == "scene_scheduler"
    labels = [item["label"] for item in done.data["sources"]]
    assert "高德地图 POI" in labels


def test_chat_falls_back_when_scene_has_no_answer():
    """垂类没答出来：写清原因退回常规链路，不能既不说原因也不给答案。"""
    class _EmptyScene:
        async def __call__(self, **kwargs):
            return None

    orchestrator = ChatOrchestrator(_Gateway(), search=None, verify_answer=False,
                                    scene=_EmptyScene())
    events = _collect(orchestrator, "帮我规划杭州两日游")
    done = events[-1]
    assert done.data.get("source") != "scene_scheduler"
    assert any("常规答案" in (e.text or "") for e in events)


def test_build_entry_scene_follows_the_switch(settings):
    """开关必须指到唯一装配点：关掉返回 None（链路不存在），打开返回可调用。"""
    from ai_butler.app import build_entry_scene

    off = settings.model_copy(update={"scene_scheduler_enable": False})
    assert build_entry_scene(off) is None

    on = settings.model_copy(update={"scene_scheduler_enable": True})
    scene = build_entry_scene(on)
    assert scene is not None and callable(scene)
