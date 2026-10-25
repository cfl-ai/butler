"""真实路线端口与 OSRM 实现（U94/U95）。

三条纪律写在前头，因为它们决定了这个模块「不做什么」
--------------------------------------------------
1. **拒绝会静默冒充的服务器**（U94 实证）：`router.project-osrm.org` 对
   `/route/v1/walking` 与 `/route/v1/driving` 返回**完全相同**的结果
   （2026-10-25 实测：3.2 min / 1.72 km，两种 profile 一字不差）——
   它静默用驾车数据冒充步行路线。要步行路线却拿到驾车路线，比没有路线更糟：
   校验逻辑会认真对待这份假数据，算出「时间冲突」的假结论。
   所以本模块只用 `routing.openstreetmap.de` 的三个真实 profile 实例。
2. **OSRM 的 duration 是估计，不是实时路况**：它由 OSM 的限速与道路等级算出，
   不含拥堵、封路、天气。每段都带 `estimated=True`——拿估计当事实，
   就是拿「看起来精确」冒充「真实」。
3. **transit 没有免费真实源**：OSRM 只有 car/foot/bike。公交涉及时刻表、
   换乘与专用道，驾车时间对它是错的估计。降级为 car 并带
   `degraded_from="transit"`，调用方必须如实标注。

端口与实现分离（U37 同款纪律）：CI 不依赖外部服务，第三方限流不会让测试变红。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import httpx

from ai_butler.verticals.tour.models import TRANSPORT_MODES
from ai_butler.verticals.tour.routing import LegMinutes, LegTable, Stop, _travel_minutes

#: 交通方式 → OSRM profile（U94）。transit 无免费真实源，降级 car。
PROFILE_BY_MODE: dict[str, str] = {
    "walk": "foot",
    "bike": "bike",
    "drive": "car",
    "transit": "car",
}

#: OSRM 三 profile 公共服务（OSM 数据，免费无 key；ODbL 需署名）
OSRM_BASE_URL = "https://routing.openstreetmap.de"
#: 默认超时（秒；实现参数）。免费公共服务慢于商业 API，10s 是宽限而非预期值
DEFAULT_TIMEOUT = 10.0


class RoutingError(RuntimeError):
    """取真实路线失败。**调用方按段降级，不整单失败**（表 12 禁回退纪律的变体）。"""


@dataclass(frozen=True)
class Leg:
    """一段真实路线的事实。"""

    origin: tuple[float, float]
    destination: tuple[float, float]
    mode: str
    distance_km: float
    minutes: int
    provider: str
    #: OSRM duration 由 OSM 限速推出，恒为估计（纪律 2）
    estimated: bool = True
    #: transit → car 的降级标记（纪律 3）；其余模式为 None
    degraded_from: str | None = None


class RoutingPort(Protocol):
    """路由端口。真机实现走 OSRM；测试注入假实现。"""

    async def route(self, origin: tuple[float, float],
                    destination: tuple[float, float], *, mode: str) -> Leg: ...


class OSRMRouter:
    """`routing.openstreetmap.de` 三 profile 客户端（U94）。

    `transport` 是测试缝：注入 `httpx.MockTransport` 后不发真实请求。
    `base_url` 只接受 https——路线响应体可以被中间件改写（本项目实测过
    502 劫持），明文链路上「真实数据」四个字没有意义。
    """

    name = "osrm"

    def __init__(self, *, base_url: str = OSRM_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not base_url.startswith("https://"):
            raise RoutingError(
                f"OSRM base_url 必须 https：{base_url!r}。"
                f"路线数据经明文链路可被中间件改写（实测过 502 劫持）")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def route(self, origin: tuple[float, float],
                    destination: tuple[float, float], *, mode: str) -> Leg:
        profile = PROFILE_BY_MODE.get(mode)
        if profile is None:
            raise RoutingError(
                f"未知交通方式 {mode!r}，可选 {list(TRANSPORT_MODES)}")
        lat1, lon1 = origin
        lat2, lon2 = destination
        url = (f"{self._base_url}/routed-{profile}/route/v1/driving/"
               f"{lon1},{lat1};{lon2},{lat2}")
        async with httpx.AsyncClient(timeout=self._timeout,
                                     transport=self._transport) as client:
            try:
                response = await client.get(url, params={"overview": "false"})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                # 端口契约：调用方只接 RoutingError，不接 httpx 的异常族
                raise RoutingError(f"OSRM 请求失败（profile={profile}）：{exc}") from exc
            data: dict[str, Any] = response.json()
        if data.get("code") != "Ok":
            raise RoutingError(
                f"OSRM 返回 code={data.get('code')!r}（profile={profile}）")
        routes = data.get("routes") or []
        if not routes:
            raise RoutingError(f"OSRM 未返回路线（profile={profile}）")
        first = routes[0]
        try:
            distance_m = float(first["distance"])
            duration_s = float(first["duration"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RoutingError(f"OSRM 响应缺少 distance/duration：{exc}") from exc
        return Leg(
            origin=origin, destination=destination, mode=mode,
            distance_km=round(distance_m / 1000.0, 3),
            minutes=max(1, int(round(duration_s / 60.0))),
            provider=f"osrm:{profile}",
            estimated=True,
            degraded_from=("transit" if mode == "transit" else None),
        )


def _haversine_leg(a: Stop, b: Stop) -> LegMinutes:
    """无真实源时的逐段降级（U95）。"""
    if None in (a.latitude, a.longitude, b.latitude, b.longitude):
        return LegMinutes(minutes=0, estimated=True, provider="no-coordinate")
    return LegMinutes(minutes=_travel_minutes(a, b), estimated=True,
                      provider="haversine")


async def fetch_legs(ordered: Sequence[Stop], *, transport: str,
                     routing: RoutingPort | None) -> LegTable:
    """按**最终顺序**取逐段真实通行时间（U94/U95）。

    - 端口缺席 → 全表 haversine，`estimated=True`；
    - 端口在场但某段失败 → **只降级那一段**（`haversine-fallback`），
      失败原因进 `failures`（表 12：失败不回退到「假装成功」，但原因必须可见）；
    - 只调 `len(ordered) - 1` 次——真实路线只对相邻点有意义。
    """
    if transport not in TRANSPORT_MODES:
        raise RoutingError(
            f"未知交通方式 {transport!r}，可选 {list(TRANSPORT_MODES)}")
    legs: dict[tuple[str, str], LegMinutes] = {}
    failures: list[str] = []
    degraded_from: str | None = "transit" if transport == "transit" else None

    for previous, stop in zip(ordered, ordered[1:]):
        key = (previous.stop_id, stop.stop_id)
        if routing is None:
            legs[key] = _haversine_leg(previous, stop)
            continue
        if None in (previous.latitude, previous.longitude,
                    stop.latitude, stop.longitude):
            # 没有坐标就无从谈「真实路线」——不是失败，是输入不适用
            legs[key] = _haversine_leg(previous, stop)
            continue
        try:
            leg = await routing.route(
                (previous.latitude, previous.longitude),
                (stop.latitude, stop.longitude), mode=transport)
        except Exception as error:                  # 任何异常 = 这一段取不到真实值
            failures.append(f"{previous.stop_id}->{stop.stop_id}: {error}")
            legs[key] = LegMinutes(minutes=_travel_minutes(previous, stop),
                                   estimated=True, provider="haversine-fallback")
            continue
        legs[key] = LegMinutes(minutes=leg.minutes, estimated=leg.estimated,
                               provider=leg.provider)
        if leg.degraded_from:
            degraded_from = leg.degraded_from

    return LegTable(legs=legs, degraded_from=degraded_from,
                    failures=tuple(failures))
