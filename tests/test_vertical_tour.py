"""第二阶段｜旅游模块专项开发测试。

计划书依据：五、第二阶段 表 11（模块定位 / 职责边界 / 执行步骤 / 模块边界）
            + 表 12（回退规则）+ 验收标准
裁定：U37（实时数据源）、U38（路线规划）、U39（垂类脚手架）、U40（重规划条件）
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agents.base import resolve
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_JEV,
    CAP_WIKI_READ,
    CAP_WIKI_WRITE,
    grant,
)
from ai_butler.agents.state import new_state
from ai_butler.core import decisions
from ai_butler.agent_wiki.ontology import SOURCE
from ai_butler.verticals.base import (
    ACTION_ASK_USER,
    ACTION_DEGRADE,
    ACTION_RETRY,
    ACTION_ROLLBACK,
    FAILURE_COLLECT_FAILED,
    FAILURE_KINDS,
    FAILURE_MISSING_PARAM,
    FAILURE_OUT_OF_SCOPE,
    FAILURE_PLAN_FAILED,
    MAX_ROLLBACKS,
    ROLLBACK_ALLOWED,
    DutyBoundary,
    ParamSpec,
    VerticalAgent,
    VerticalError,
    decide_fallback,
    parse_params,
)
from ai_butler.verticals.tour.agent import (
    HANDOFF,
    IN_SCOPE,
    OUT_OF_SCOPE,
    TOUR_BOUNDARY,
    TourAgent,
)
from ai_butler.verticals.tour.models import (
    TRIP_PARAM_SPECS,
    CostItem,
    Stop,
    TripPlan,
    TripRequest,
)
from ai_butler.verticals.tour.orders import (
    OrderError,
    build_cost_items,
    build_payment_list,
)
from ai_butler.verticals.tour.replan import (
    DEFAULT_WEATHER_THRESHOLD,
    TRIGGER_CLOSURE,
    TRIGGER_DELAY,
    TRIGGER_WEATHER,
    TRIGGERS,
    ReplanSignals,
    evaluate_preconditions,
)
from ai_butler.verticals.tour.routing import (
    ISSUE_CLOSED,
    ISSUE_DISTANCE_UNREASONABLE,
    ISSUE_TIME_CONFLICT,
    MAX_LEG_MINUTES,
    haversine_km,
    greedy_order,
    low_confidence_stops,
    plan_route,
)
from ai_butler.verticals.tour.sources import (
    PROVIDER_OVERPASS,
    SOURCE_KINDS,
    SOURCE_SCENIC,
    SOURCE_WEATHER,
    CollectedFact,
    CollectError,
    collect_all,
    source_page_id,
    to_claim_fact,
    to_source_page,
)


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


def _req(**over):
    base = dict(destination="杭州", start_date="2026-11-01", days=2, party_size=2)
    base.update(over)
    return TripRequest(**base)


def _stops():
    return [
        Stop("s1", "西湖", 8 * 60, 18 * 60, 180, 0, latitude=30.25, longitude=120.15),
        Stop("s2", "灵隐寺", 7 * 60, 17 * 60, 120, 75, latitude=30.24, longitude=120.10),
    ]


class FakeCollector:
    def __init__(self, *, fail_on: tuple[str, ...] = (), low: bool = False):
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


# ============================== 台账 ============================== #
def test_phase_two_decisions_are_registered_and_resolved():
    keys = {d.key for d in decisions.DECISIONS if d.phase == "V1"}
    # U94–U97 = 2026-10-26 真实数据接入批次（路线源/交通方式分级/weather源/实时语义）；
    # U101/U103/U104 = 高德接入方式、坐标基准转换、镇乡层级缺口；
    # U105/U106 = 电脑端定位、规划含时间
    assert keys == {"U37", "U38", "U39", "U40", "U94", "U95", "U96", "U97",
                    "U101", "U103", "U104", "U105", "U106"}
    for key in keys:
        assert decisions.get(key).status == decisions.RESOLVED


# ============================== 回退规则（表 12）============================== #
def test_only_out_of_scope_may_roll_back():
    """表 12：**仅任务完全超出业务域才允许回退**。"""
    assert ROLLBACK_ALLOWED == frozenset({FAILURE_OUT_OF_SCOPE})
    assert decide_fallback(failure=FAILURE_OUT_OF_SCOPE).allowed is True


def test_in_domain_failures_are_never_allowed_to_roll_back():
    for failure, action in ((FAILURE_MISSING_PARAM, ACTION_ASK_USER),
                            (FAILURE_COLLECT_FAILED, ACTION_RETRY),
                            (FAILURE_PLAN_FAILED, ACTION_DEGRADE)):
        got = decide_fallback(failure=failure)
        assert got.allowed is False, f"{failure} 不得回退"
        assert got.action == action
        assert "禁止回退" in got.reason


def test_rollback_respects_the_global_limit():
    got = decide_fallback(failure=FAILURE_OUT_OF_SCOPE, rollbacks_used=MAX_ROLLBACKS)
    assert got.allowed is False and got.action == ACTION_DEGRADE
    assert "甩锅循环" in got.reason
    assert MAX_ROLLBACKS == 2, "表 12 写的是全局 2 次上限"


def test_unknown_failure_kind_is_rejected():
    with pytest.raises(VerticalError):
        decide_fallback(failure="whatever")


# ============================== 职责边界 ============================== #
def test_boundary_classifies_in_and_out_of_scope():
    assert TOUR_BOUNDARY.classify("trip_plan") == "in_scope"
    assert TOUR_BOUNDARY.classify("refund") == FAILURE_OUT_OF_SCOPE


def test_boundary_refuses_undeclared_task_kinds():
    """未声明的类别**必须显式失败**，不能猜——猜错就是把本职任务退回去甩锅。"""
    with pytest.raises(VerticalError) as ei:
        TOUR_BOUNDARY.classify("unknown_task")
    assert "未在" in str(ei.value)


def test_boundary_requires_in_scope_capability():
    with pytest.raises(VerticalError):
        DutyBoundary(vertical="x")
    with pytest.raises(VerticalError):
        DutyBoundary(vertical="")


def test_out_of_scope_lists_match_the_plan():
    assert "refund" in OUT_OF_SCOPE and "payment_confirm" in OUT_OF_SCOPE
    assert "food_order" in OUT_OF_SCOPE and "system_command" in OUT_OF_SCOPE
    assert set(IN_SCOPE) == {"trip_plan", "realtime_query", "trip_update",
                             "plan_output", "order_assist", "archive"}
    assert set(HANDOFF) == set(OUT_OF_SCOPE), "每个 ❌ 项都该有去向提示"


# ============================== 脚手架边界（U39）============================== #
def test_vertical_agent_has_no_routing_or_dispatch_power():
    """表 11 模块边界：无权修改路由规则、无权分发任务给其他 Agent。"""
    assert TourAgent.may_route is False
    assert TourAgent.may_dispatch_to_peers is False


def test_cross_module_request_is_a_description_not_an_action():
    got = VerticalAgent.out_of_scope_request(target="p8_payment", payload={"total": 1})
    assert got["relay_via"] == "butler_agent"
    assert got["target"] == "p8_payment"
    assert "中转" in got["note"]

    with pytest.raises(VerticalError):
        VerticalAgent.out_of_scope_request(target="", payload={})


# ============================== 参数解析（表 11 步骤1）============================== #
def test_parse_params_reports_missing_without_raising():
    got = parse_params({"destination": "杭州"}, TRIP_PARAM_SPECS)
    assert got.complete is False
    assert "出行日期" in got.missing
    assert "补充" in got.ask_text()


def test_parse_params_complete():
    got = parse_params({"destination": "杭州", "start_date": "2026-11-01", "days": 2,
                        "party_size": 2}, TRIP_PARAM_SPECS)
    assert got.complete is True and got.ask_text() == ""


def test_parse_params_treats_blank_and_empty_list_as_missing():
    got = parse_params({"destination": "  ", "start_date": "x", "days": [], "party_size": 1},
                       TRIP_PARAM_SPECS)
    assert "目的地" in got.missing and "出行天数" in got.missing


# ============================== 路线校验（U38）============================== #
def test_haversine_is_reasonable():
    # 杭州西湖 -> 灵隐寺 约 5 公里
    km = haversine_km((30.25, 120.15), (30.24, 120.10))
    assert 3 < km < 8


def test_greedy_order_picks_nearest_next():
    stops = [
        Stop("far", "远", latitude=31.0, longitude=121.0),
        Stop("near", "近", latitude=30.26, longitude=120.16),
    ]
    assert greedy_order(stops, origin=(30.25, 120.15))[0] == 1


def test_plan_route_assigns_travel_minutes():
    ordered, issues = plan_route(_stops(), origin=(30.27, 120.16))
    assert [s.name for s in ordered][0] == "西湖"
    assert ordered[0].travel_minutes == 0
    assert ordered[1].travel_minutes > 0
    assert issues == []


def test_plan_route_flags_time_conflict():
    late = [Stop("s1", "夜市", open_minute=20 * 60, close_minute=21 * 60,
                 dwell_minutes=180)]
    _, issues = plan_route(late, start_minute=9 * 60)
    assert any(i["kind"] == ISSUE_TIME_CONFLICT for i in issues)


def test_plan_route_flags_closed_scenic():
    _, issues = plan_route([Stop("s1", "闭园景区", closed=True)])
    assert any(i["kind"] == ISSUE_CLOSED for i in issues)


def test_plan_route_flags_unreasonable_distance():
    far = [Stop("a", "起点", latitude=30.25, longitude=120.15),
           Stop("b", "很远的地方", latitude=39.9, longitude=116.4)]
    _, issues = plan_route(far, origin=(30.25, 120.15))
    assert any(i["kind"] == ISSUE_DISTANCE_UNREASONABLE for i in issues)


def test_plan_route_rejects_bad_optimizer_output():
    with pytest.raises(ValueError):
        plan_route(_stops(), optimizer=lambda stops, origin: [0])


def test_low_confidence_stops_are_labelled():
    stops = [Stop("s1", "未核实景点", low_confidence=True), Stop("s2", "正常景点")]
    assert low_confidence_stops(stops) == ("未核实景点",)


# ============================== 采集底座（U37）============================== #
def test_collected_fact_rejects_private_fields():
    """表 11：公共采集底座不可读取隐私。"""
    with pytest.raises(CollectError) as ei:
        CollectedFact(kind=SOURCE_WEATHER, subject="杭州", evidence_id="e1",
                      provider="fake", payload={"user_id": "user-A"})
    assert "不得包含用户私有字段" in str(ei.value)


def test_collected_fact_requires_evidence():
    with pytest.raises(CollectError):
        CollectedFact(kind=SOURCE_WEATHER, subject="杭州", evidence_id="",
                      provider="fake", payload={})


def test_collected_fact_validates_kind():
    with pytest.raises(CollectError):
        CollectedFact(kind="nope", subject="x", evidence_id="e", provider="p", payload={})


def test_collect_all_reports_failures_without_raising():
    got = collect_all(FakeCollector(fail_on=(SOURCE_SCENIC,)), subject="杭州")
    assert got.ok is False and len(got.failures) == 1
    assert len(got.facts) == len(SOURCE_KINDS) - 1


def test_collect_all_ok():
    got = collect_all(FakeCollector(), subject="杭州")
    assert got.ok is True and len(got.facts) == len(SOURCE_KINDS)


def test_source_page_is_public_and_claim_binds_evidence():
    fact = CollectedFact(kind=SOURCE_WEATHER, subject="杭州", evidence_id="ev-1",
                         provider="fake", payload={"temp": 20})
    page = to_source_page(fact, now="2026-10-16T00:00:00Z")
    assert page.type == SOURCE and page.user_id is None, "source 页属公共 Workspace"
    assert page.id == source_page_id(fact)

    claim = to_claim_fact(fact, subject_id="杭州", claim_page_id="claim-1")
    assert claim["evidence_ids"] == ["ev-1"]


def test_claim_requires_evidence():
    fact = CollectedFact(kind=SOURCE_WEATHER, subject="杭州", evidence_id="ev-1",
                         provider="fake", payload={})
    object.__setattr__(fact, "evidence_id", "")
    with pytest.raises(CollectError):
        to_claim_fact(fact, subject_id="x", claim_page_id="c")


# ============================== 重规划判定（U40）============================== #
def test_no_trigger_means_no_llm_call():
    """原文「条件成立**才**调用大模型」——不成立时根本不该召模型。"""
    got = evaluate_preconditions(ReplanSignals())
    assert got.needs_llm is False and got.triggers == ()
    assert "不调用大模型" in got.reason


def test_closure_always_triggers():
    got = evaluate_preconditions(ReplanSignals(scenic_closed=True))
    assert got.needs_llm is True and TRIGGER_CLOSURE in got.triggers


def test_weather_triggers_only_at_threshold():
    assert evaluate_preconditions(
        ReplanSignals(weather_level=DEFAULT_WEATHER_THRESHOLD - 1)).needs_llm is False
    got = evaluate_preconditions(ReplanSignals(weather_level=DEFAULT_WEATHER_THRESHOLD))
    assert got.needs_llm is True and TRIGGER_WEATHER in got.triggers


def test_delay_triggers_only_when_exceeding_slack():
    """延误了但还来得及，就不该重规划。"""
    ok = evaluate_preconditions(ReplanSignals(transport_delay_minutes=30,
                                              schedule_slack_minutes=60))
    assert ok.needs_llm is False
    bad = evaluate_preconditions(ReplanSignals(transport_delay_minutes=90,
                                               schedule_slack_minutes=60))
    assert bad.needs_llm is True and TRIGGER_DELAY in bad.triggers


def test_triggers_are_returned_in_stable_order():
    got = evaluate_preconditions(ReplanSignals(
        scenic_closed=True, weather_level=4, transport_delay_minutes=999))
    assert got.triggers == tuple(t for t in TRIGGERS if t in got.triggers)


def test_signals_are_validated():
    with pytest.raises(ValueError):
        ReplanSignals(weather_level=9)
    with pytest.raises(ValueError):
        ReplanSignals(transport_delay_minutes=-1)


# ============================== 费用与支付清单 ============================== #
def test_cost_items_scale_by_party_size():
    plan = TripPlan(request=_req(party_size=3), stops=_stops())
    items = build_cost_items(plan, hotel_per_night=400, nights=1)
    ticket = next(i for i in items if "灵隐寺" in i.name)
    assert ticket.amount == 75 * 3
    assert any(i.kind == "hotel" for i in items)


def test_payment_list_is_a_request_not_an_execution():
    """表 11：旅游 Agent **仅生成待付费清单，无权直接执行付款**。"""
    plan = TripPlan(request=_req(), stops=_stops(),
                    costs=[CostItem("门票", 150)])
    got = build_payment_list(plan, user_id="user-A")
    assert got["executed"] is False
    assert got["requires_manual_confirmation"] is True
    assert got["relay_via"] == "butler_agent"
    assert "无权直接执行付款" in got["note"]
    assert got["total"] == 150


def test_payment_list_supports_merged_bills():
    plan = TripPlan(request=_req(), stops=_stops(),
                    costs=[CostItem("a", 10), CostItem("b", 20)])
    merged = build_payment_list(plan, user_id="user-A", merge=True)
    assert merged["merge"] is True and merged["merged_count"] == 2
    assert build_payment_list(plan, user_id="user-A", merge=False)["merge"] is False


def test_payment_list_requires_user_id():
    with pytest.raises(OrderError):
        build_payment_list(TripPlan(request=_req()), user_id="")


# ============================== 旅游 Agent 端到端 ============================== #
def _agent(**over):
    available = {CAP_DATA_READ: over.get("sdk"), CAP_WIKI_READ: over.get("wiki"),
                 CAP_WIKI_WRITE: over.get("writer"), CAP_JEV: over.get("jev")}
    # 垂类 Agent 没有 settings 字段（那是 PhaseAgent 的），但有 tools（能力裁剪）
    return TourAgent(tools=grant(TourAgent, available),
                     collector=over.get("collector"))


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


def test_agent_handles_a_full_trip():
    payload = _call(_agent(collector=FakeCollector()), _state(
        task_kind="trip_plan", task_params=_PARAMS, candidate_stops=_CANDIDATES,
        replan_signals={"scenic_closed": True}))["tour_result"]
    assert payload["handled"] is True
    assert payload["plan"]["stops"] == ["西湖", "灵隐寺"]
    assert payload["replan"]["needs_llm"] is True
    assert payload["payment_list"]["executed"] is False
    assert len(payload["source_pages"]) == len(SOURCE_KINDS)
    assert payload["synthesis"]["scope"] == "vault"


def test_agent_falls_back_only_for_out_of_scope():
    payload = _call(_agent(), _state(task_kind="refund"))["tour_result"]
    assert payload["handled"] is False
    assert payload["fallback"]["allowed"] is True
    assert payload["fallback"]["action"] == ACTION_ROLLBACK
    assert payload["handoff_hint"] == "p8_payment"
    assert "原路返回路由中枢" in payload["note"]


def test_agent_does_not_roll_back_for_missing_params():
    payload = _call(_agent(), _state(task_kind="trip_plan",
                                     task_params={"destination": "杭州"}))["tour_result"]
    assert payload["handled"] is False
    assert payload["fallback"]["allowed"] is False
    assert payload["fallback"]["action"] == ACTION_ASK_USER
    assert "出行日期" in payload["ask"]


def test_agent_does_not_roll_back_when_collection_fails():
    payload = _call(_agent(collector=FakeCollector(fail_on=(SOURCE_SCENIC,))), _state(
        task_kind="trip_plan", task_params=_PARAMS,
        candidate_stops=_CANDIDATES))["tour_result"]
    assert payload["handled"] is False
    assert payload["fallback"]["action"] == ACTION_RETRY
    assert payload["failures"]


def test_agent_degrades_when_no_candidate_stops():
    payload = _call(_agent(), _state(task_kind="trip_plan",
                                     task_params=_PARAMS))["tour_result"]
    assert payload["handled"] is False
    assert payload["fallback"]["action"] == ACTION_DEGRADE


def test_agent_rejects_undeclared_task_kind():
    with pytest.raises(VerticalError):
        _call(_agent(), _state(task_kind="unknown"))


def test_agent_writes_only_its_own_field():
    out = _call(_agent(), _state(task_kind="trip_plan", task_params=_PARAMS,
                                 candidate_stops=_CANDIDATES))
    assert set(out) == {"tour_result"}


def test_agent_skips_when_session_is_invalid():
    assert _agent()(_state(login_status=False, task_kind="trip_plan")) == {}


def test_agent_requires_no_routing_capability():
    """旅游 Agent 不拥有路由能力——它连声明都不该声明。"""
    assert TourAgent.may_route is False
    from ai_butler.agents.capabilities import CAPABILITIES
    assert not set(TourAgent.capabilities) - set(CAPABILITIES)
