"""真实路线接入的守护测试（U94/U95）。

分两层，两层都不能省：
- **MockTransport 层**（CI 常绿）：URL 拼接、profile 映射、解析、降级标注、
  逐段失败隔离、调用次数纪律。mock 证明的是「我们怎么用这个 API」。
- **真实网络层**（`LIVE_ROUTING=1` 才跑）：对 routing.openstreetmap.de
  发真实请求，验证「foot 比 car 慢」这个 **U94 选型的核心事实**。
  mock 永远证明不了这件事——被拒绝的 demo 服务器在 mock 里也「能用」，
  它只有在真实网络上才暴露静默冒充。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import pytest

from ai_butler.verticals.tour.agent import TourAgent
from ai_butler.verticals.tour.live_routing import (
    DEFAULT_TIMEOUT,
    OSRM_BASE_URL,
    PROFILE_BY_MODE,
    OSRMRouter,
    RoutingError,
    fetch_legs,
)
from ai_butler.verticals.tour.models import Stop, TRANSPORT_MODES
from ai_butler.verticals.tour.routing import build_timeline, plan_route

LIVE = os.environ.get("LIVE_ROUTING") == "1"


# --------------------------------------------------------------------------- #
#  fixtures
# --------------------------------------------------------------------------- #
def _osrm_response(*, distance_m: float = 1180.0, duration_s: float = 948.0,
                   code: str = "Ok") -> httpx.Response:
    return httpx.Response(200, json={
        "code": code,
        "routes": [{"distance": distance_m, "duration": duration_s}]})


def _router(handler) -> OSRMRouter:
    return OSRMRouter(transport=httpx.MockTransport(handler))


def _stops() -> list[Stop]:
    return [
        Stop("s1", "第一站", dwell_minutes=60, latitude=39.909, longitude=116.404),
        Stop("s2", "第二站", dwell_minutes=45, latitude=39.915, longitude=116.410),
        Stop("s3", "第三站", dwell_minutes=30, latitude=39.920, longitude=116.398),
    ]


class _FakeRouter:
    """记录调用、可定点抛错的假路由端口。"""

    def __init__(self, *, fail_for: tuple[str, ...] = (),
                 minutes: int = 20, provider: str = "osrm:car") -> None:
        self.calls: list[tuple[tuple[float, float], tuple[float, float], str]] = []
        self.fail_for = fail_for
        self.minutes = minutes
        self.provider = provider

    async def route(self, origin, destination, *, mode: str):
        self.calls.append((origin, destination, mode))
        for marker in self.fail_for:
            if marker in f"{origin}->{destination}":
                raise RoutingError(f"模拟失败 {marker}")
        from ai_butler.verticals.tour.live_routing import Leg
        return Leg(origin=origin, destination=destination, mode=mode,
                   distance_km=1.0, minutes=self.minutes,
                   provider=self.provider,
                   degraded_from=("transit" if mode == "transit" else None))


# --------------------------------------------------------------------------- #
#  profile 映射与 URL 纪律（U94）
# --------------------------------------------------------------------------- #
def test_every_transport_mode_has_a_profile():
    """交通方式必须全部有落点——漏一个就是「参数收了、计算时没它」（U95 缺口形态）。"""
    assert set(PROFILE_BY_MODE) == set(TRANSPORT_MODES)


def test_transit_degrades_to_car_profile():
    """transit 没有免费真实源（U94）：映射到 car，且降级标记必须能传出去。"""
    assert PROFILE_BY_MODE["transit"] == "car"


def test_url_uses_multi_profile_instance_not_the_demo_server():
    """U94 的核心守护：默认实例必须是三 profile 的 osm.de，不是会静默冒充的 demo。

    这条断言挡的是「哪天有人图省事把 base_url 改回 router.project-osrm.org」——
    在 mock 里那个服务器看起来完全正常，只有在真实网络上才暴露
    walking 与 driving 返回完全一致。
    """
    assert "routing.openstreetmap.de" in OSRM_BASE_URL
    assert "router.project-osrm.org" not in OSRM_BASE_URL
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _osrm_response()

    asyncio.run(_router(handler).route((39.909, 116.404), (39.915, 116.410),
                                       mode="drive"))
    assert seen, "没有发出请求"
    assert seen[0].startswith(
        "https://routing.openstreetmap.de/routed-car/route/v1/driving/")


def test_walk_and_bike_use_their_own_profiles():
    """walk→routed-foot、bike→routed-bike（U94 三 profile 各司其职）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[str(request.url).split("/routed-")[1].split("/")[0]] = "hit"
        return _osrm_response()

    router = _router(handler)
    asyncio.run(router.route((39.909, 116.404), (39.915, 116.410), mode="walk"))
    asyncio.run(router.route((39.909, 116.404), (39.915, 116.410), mode="bike"))
    assert set(seen) == {"foot", "bike"}


def test_parses_distance_and_duration_into_minutes():
    """948 秒 → 16 分钟；距离转 km。"""
    async def go():
        return await _router(
            lambda r: _osrm_response(distance_m=1180.0, duration_s=948.0)
        ).route((39.909, 116.404), (39.915, 116.410), mode="walk")

    leg = asyncio.run(go())
    assert leg.minutes == 16
    assert leg.distance_km == 1.18
    assert leg.provider == "osrm:foot"
    # OSRM duration 恒为估计（纪律 2：拿估计当事实比没有估计更糟）
    assert leg.estimated is True


def test_transit_leg_carries_degraded_marker():
    async def go():
        return await _router(lambda r: _osrm_response()).route(
            (39.909, 116.404), (39.915, 116.410), mode="transit")

    leg = asyncio.run(go())
    assert leg.provider == "osrm:car"
    assert leg.degraded_from == "transit"


def test_osrm_error_code_raises_routing_error():
    async def go():
        return await _router(
            lambda r: _osrm_response(code="NoRoute")).route(
            (39.909, 116.404), (39.915, 116.410), mode="drive")

    with pytest.raises(RoutingError, match="NoRoute"):
        asyncio.run(go())


def test_http_failure_is_wrapped_as_routing_error():
    """端口契约：调用方只接 RoutingError，不接 httpx 异常族。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    async def go():
        return await _router(handler).route(
            (39.909, 116.404), (39.915, 116.410), mode="drive")

    with pytest.raises(RoutingError):
        asyncio.run(go())


def test_plain_http_base_url_is_refused():
    """路线响应可被中间件改写（实测过 502 劫持），明文链路拒接。"""
    with pytest.raises(RoutingError, match="https"):
        OSRMRouter(base_url="http://routing.example.com")


def test_unknown_mode_is_refused():
    async def go():
        return await _router(lambda r: _osrm_response()).route(
            (39.909, 116.404), (39.915, 116.410), mode="teleport")

    with pytest.raises(RoutingError, match="teleport"):
        asyncio.run(go())


# --------------------------------------------------------------------------- #
#  fetch_legs：逐段降级与调用纪律（U95）
# --------------------------------------------------------------------------- #
def test_fetch_legs_without_port_is_haversine_only():
    """端口缺席 → 全表 haversine，estimated=True，不许假装真实。"""
    table = asyncio.run(fetch_legs(_stops(), transport="walk", routing=None))
    assert table.basis() == "haversine"
    assert table.failures == ()
    assert all(leg.estimated for leg in table.legs.values())
    assert {leg.provider for leg in table.legs.values()} == {"haversine"}


def test_fetch_legs_calls_router_once_per_adjacent_pair():
    """真实路线只对相邻点有意义：3 站 = 2 次调用，不是 3² 次。"""
    fake = _FakeRouter(minutes=12)
    asyncio.run(fetch_legs(_stops(), transport="drive", routing=fake))
    assert len(fake.calls) == 2
    assert all(call[2] == "drive" for call in fake.calls)


def test_fetch_legs_isolates_failure_to_single_leg():
    """一段失败只降级那一段（haversine-fallback），失败原因必须可见。"""
    # 标记用第三站经度：注意 116.410 会被 Python 打印成 116.41，
    # 拿它当标记会误伤前后两段（本测试第一版就踩了这个坑）。
    fake = _FakeRouter(fail_for=("116.398",))
    table = asyncio.run(fetch_legs(_stops(), transport="drive", routing=fake))
    assert len(table.failures) == 1
    assert "116.398" in table.failures[0]
    providers = {key: leg.provider for key, leg in table.legs.items()}
    assert "haversine-fallback" in providers.values()
    assert "osrm:car" in providers.values()
    assert table.basis() == "mixed"


def test_fetch_legs_skips_legs_without_coordinates():
    """没有坐标就无从谈真实路线——不是失败，是输入不适用；且不浪费一次调用。"""
    stops = [Stop("s1", "有坐标", latitude=39.909, longitude=116.404),
             Stop("s2", "没坐标")]
    fake = _FakeRouter()
    table = asyncio.run(fetch_legs(stops, transport="walk", routing=fake))
    assert fake.calls == []
    assert table.legs[("s1", "s2")].provider == "no-coordinate"


def test_fetch_legs_rejects_unknown_transport():
    with pytest.raises(RoutingError, match="teleport"):
        asyncio.run(fetch_legs(_stops(), transport="teleport",
                               routing=_FakeRouter()))


def test_fetch_legs_propagates_transit_degradation():
    fake = _FakeRouter(provider="osrm:car")
    table = asyncio.run(fetch_legs(_stops(), transport="transit", routing=fake))
    assert table.degraded_from == "transit"


# --------------------------------------------------------------------------- #
#  时间轴：真实分钟数与估计标注（U95）
# --------------------------------------------------------------------------- #
def test_build_timeline_uses_leg_table_minutes():
    stops = _stops()
    table = asyncio.run(fetch_legs(stops, transport="drive",
                                   routing=_FakeRouter(minutes=42)))
    ordered, issues = build_timeline(stops, legs=table)
    assert [s.stop_id for s in ordered] == ["s1", "s2", "s3"]
    assert ordered[1].travel_minutes == 42
    assert ordered[2].travel_minutes == 42
    # OSRM 的 duration 是估计（不含实时路况），estimated 必须如实传递
    assert ordered[1].travel_estimated is True


def test_plan_route_without_legs_marks_everything_estimated():
    ordered, _ = plan_route(_stops())
    assert ordered[1].travel_minutes > 0
    assert ordered[1].travel_estimated is True


# --------------------------------------------------------------------------- #
#  Agent 集成：route 事实必须出现在输出里（U94/U95）
# --------------------------------------------------------------------------- #
def _tour_state(**extra: Any) -> dict[str, Any]:
    state = {
        "user_id": "u1",
        "task_kind": "trip_plan",
        "task_params": {"destination": "北京", "start_date": "2026-11-01",
                        "days": 1, "party_size": 2, "transport": "drive"},
        "candidate_stops": [
            {"stop_id": "s1", "name": "第一站", "dwell_minutes": 60,
             "latitude": 39.909, "longitude": 116.404},
            {"stop_id": "s2", "name": "第二站", "dwell_minutes": 45,
             "latitude": 39.915, "longitude": 116.410},
        ],
    }
    state.update(extra)
    return state


def test_agent_reports_real_route_basis_when_router_injected():
    agent = TourAgent(routing=_FakeRouter(minutes=15))
    result = asyncio.run(agent.run(_tour_state()))
    route = result["tour_result"]["route"]
    assert route["basis"] == "osrm:car"
    assert route["estimate_only"] is False
    assert route["degraded_from"] is None
    assert len(route["legs"]) == 1
    assert route["legs"][0]["minutes"] == 15
    assert result["tour_result"]["plan"]["route_basis"] == "osrm:car"


def test_agent_reports_haversine_basis_without_router():
    """没有路由端口时，输出必须自证是估计——不许悄悄看起来像真实路线。"""
    agent = TourAgent()
    result = asyncio.run(agent.run(_tour_state()))
    route = result["tour_result"]["route"]
    assert route["basis"] == "haversine"
    assert route["estimate_only"] is True
    assert result["tour_result"]["plan"]["estimate_only"] is True


def test_agent_surfaces_routing_failures_without_failing_the_plan():
    """一段取不到真实值：计划照给，失败原因必须出现在输出里（表 12 纪律）。

    只有一段行程且它失败了 → basis 回到 `haversine`（路线质量确实就是估计），
    但 `failures` 非空把「试过真实、没成功」与「从没试过」区分开——
    这两个状态在输出里长得一样才是事故。
    """
    agent = TourAgent(routing=_FakeRouter(fail_for=("116.41",)))
    result = asyncio.run(agent.run(_tour_state()))
    route = result["tour_result"]["route"]
    assert result["tour_result"]["handled"] is True
    assert route["basis"] == "haversine"
    assert route["estimate_only"] is True
    assert len(route["failures"]) == 1


# --------------------------------------------------------------------------- #
#  真实网络层（LIVE_ROUTING=1 才跑）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_ROUTING=1（真实网络冒烟）")
def test_live_foot_is_slower_than_car_for_the_same_pair():
    """U94 选型的事实内核：同一对坐标，foot 必须显著慢于 car。

    这正是 demo 服务器通不过的测试——它对两种 profile 返回完全一致的结果。
    """
    origin, destination = (39.909, 116.404), (39.915, 116.410)
    router = OSRMRouter()

    async def go(mode: str):
        return await router.route(origin, destination, mode=mode)

    car = asyncio.run(go("drive"))
    foot = asyncio.run(go("walk"))
    assert foot.minutes > car.minutes, (
        f"foot({foot.minutes}min) 未慢于 car({car.minutes}min)："
        f"要么 profile 串了，要么打到了会静默冒充的服务器")
    assert foot.provider == "osrm:foot"


@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_ROUTING=1（真实网络冒烟）")
def test_live_route_returns_positive_distance():
    router = OSRMRouter()
    leg = asyncio.run(router.route((39.909, 116.404), (39.915, 116.410),
                                   mode="bike"))
    assert leg.distance_km > 0
    assert leg.minutes >= 1
    assert leg.provider == "osrm:bike"
