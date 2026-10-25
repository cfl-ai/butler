"""行程编排与路线校验（裁定 U38；真实路线接入 U94/U95）。

计划书依据
----------
- 五、第二阶段表 11「生成初步路线；**内置校验逻辑，检查路线时间冲突、距离不合理等问题**；
  条件判断交给 Jev 决策；**低置信信息做提示标注**」

裁定 U38
--------
规模化优化以 OR-Tools 为目标算法；本项目**先实现不依赖外部求解器的贪心最近邻 + 显式校验**，
OR-Tools 作为端口注入的升级路径。

**校验是底线，优化是增强** —— 所以校验永不缺席，求解器可以没有。

裁定 U94/U95（2026-10-26）
--------------------------
通行时间从「固定 25 km/h 的 haversine 估计」升级为**可注入真实路由端口**
（`live_routing.OSRMRouter`，routing.openstreetmap.de 三 profile）。
本文件保持**纯同步、无网络**：异步取数在 `live_routing.fetch_legs` 完成，
结果以 `LegTable` 传入 `build_timeline`。这样校验内核可以在没有任何网络的
CI 里被完整测试，也不会出现「校验逻辑依赖一次 HTTP 才能跑」的窘境。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ai_butler.verticals.tour.models import MINUTES_PER_DAY, Stop

#: 单段通行时间的合理上限（分钟；实现参数）
MAX_LEG_MINUTES = 180
#: 单日通行时间占比上限（超过即判「距离不合理」；实现参数）
MAX_DAILY_TRAVEL_RATIO = 0.45
#: 行程默认开始时刻（09:00，分钟制；实现参数）
DEFAULT_START_MINUTE = 9 * 60

#: 校验问题类别
ISSUE_TIME_CONFLICT = "time_conflict"
ISSUE_DISTANCE_UNREASONABLE = "distance_unreasonable"
ISSUE_CLOSED = "closed"
ISSUE_KINDS: tuple[str, ...] = (ISSUE_TIME_CONFLICT, ISSUE_DISTANCE_UNREASONABLE,
                                ISSUE_CLOSED)

#: 优化器签名：给定点集与起点，返回访问顺序（索引列表）
Optimizer = Callable[[Sequence[Stop], tuple[float, float] | None], Sequence[int]]

#: haversine 降级路径的默认均速（km/h；实现参数，与旧版固定值一致）
HAVERSINE_KMPH = 25.0


@dataclass(frozen=True)
class LegMinutes:
    """一段通行时间的事实记录（U94/U95）。

    `provider` 说明这段数字从哪来：`osrm:<profile>` = 真实路网；
    `haversine` = 几何估计；`haversine-fallback` = 真实源调用失败后的降级。
    **三者都是分钟数，但可信度不同**——所以必须带来源，不能只给数字。
    """

    minutes: int
    estimated: bool
    provider: str


@dataclass(frozen=True)
class LegTable:
    """按最终顺序的逐段通行时间表（U94/U95）。"""

    legs: Mapping[tuple[str, str], LegMinutes] = field(default_factory=dict)
    #: transit 无免费真实源，降级来源（U94）；None 表示未降级
    degraded_from: str | None = None
    #: 取真实路线时的逐段失败原因（表 12：失败禁回退，但原因必须可见）
    failures: tuple[str, ...] = ()

    def leg(self, from_stop_id: str, to_stop_id: str) -> LegMinutes | None:
        return self.legs.get((from_stop_id, to_stop_id))

    def basis(self) -> str:
        """整条路线的依据口径：`osrm:<profile>` / `haversine` / `mixed`。"""
        providers = {leg.provider for leg in self.legs.values()}
        if not providers:
            return "haversine"
        if providers == {"haversine"}:
            return "haversine"
        real = {p for p in providers if p.startswith("osrm:")}
        if not real:
            return "haversine"
        if len(real) == 1 and len(providers) == 1:
            return next(iter(real))
        return "mixed"


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点球面距离（公里）。"""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _travel_minutes(a: Stop, b: Stop, *, kmph: float = HAVERSINE_KMPH) -> int:
    if None in (a.latitude, a.longitude, b.latitude, b.longitude):
        return 0
    km = haversine_km((a.latitude, a.longitude), (b.latitude, b.longitude))
    return int(round(km / kmph * 60))


def greedy_order(stops: Sequence[Stop],
                 origin: tuple[float, float] | None = None) -> list[int]:
    """贪心最近邻排序（无坐标时保持原序）。

    **不引入求解器**：先把「能跑、可校验」做出来，OR-Tools 属于后续增强（U38）。
    """
    remaining = list(range(len(stops)))
    order: list[int] = []
    current = origin
    while remaining:
        if current is None:
            order.extend(remaining)
            break
        def key(i: int) -> float:
            s = stops[i]
            if None in (s.latitude, s.longitude):
                return float("inf")
            return haversine_km(current, (s.latitude, s.longitude))
        best = min(remaining, key=key)
        order.append(best)
        remaining.remove(best)
        s = stops[best]
        if None not in (s.latitude, s.longitude):
            current = (s.latitude, s.longitude)
    return order


def order_stops(stops: Sequence[Stop], *,
                origin: tuple[float, float] | None = None,
                optimizer: Optimizer | None = None) -> list[Stop]:
    """只排顺序，不算时间轴（U95 拆分的第二步前置）。

    顺序与时间轴拆开，是为了让「逐段取真实路线」这个**异步**步骤
    能插在中间——真实路线只对**最终顺序的相邻点**有意义（n-1 次调用），
    对全矩阵（n² 次）是浪费免费 API 的配额。
    """
    if not stops:
        return []
    order = list(optimizer(stops, origin)) if optimizer else greedy_order(stops, origin)
    if sorted(order) != list(range(len(stops))):
        raise ValueError("优化器返回的不是原点的排列")
    return [stops[i] for i in order]


def build_timeline(ordered: Sequence[Stop], *,
                   legs: LegTable | None = None,
                   start_minute: int = DEFAULT_START_MINUTE
                   ) -> tuple[list[Stop], list[dict[str, Any]]]:
    """按给定顺序计算时间轴，返回 (行程点序列, 校验问题)。

    `legs` 为 None 时整条路线走 haversine 降级（`travel_estimated=True`）——
    这是 CI 与无网络环境的路径，**降级可以发生，冒充不行**（U95）。
    """
    result: list[Stop] = []
    issues: list[dict[str, Any]] = []
    clock = start_minute
    previous: Stop | None = None

    for stop in ordered:
        travel = 0
        estimated = False
        if previous is not None:
            leg = legs.leg(previous.stop_id, stop.stop_id) if legs else None
            if leg is not None:
                travel, estimated = leg.minutes, leg.estimated
            else:
                travel = _travel_minutes(previous, stop)
                estimated = True
        clock += travel
        arrive = clock

        if stop.closed:
            issues.append({"kind": ISSUE_CLOSED, "stop_id": stop.stop_id,
                           "detail": f"{stop.name} 临时闭园"})

        if stop.open_minute is not None and arrive < stop.open_minute:
            # 到得太早：等到开门（不算冲突，但要标注）
            clock = stop.open_minute
            arrive = clock
        leave = arrive + stop.dwell_minutes
        if stop.close_minute is not None and leave > stop.close_minute:
            issues.append({
                "kind": ISSUE_TIME_CONFLICT, "stop_id": stop.stop_id,
                "detail": (f"{stop.name} 预计 {leave % MINUTES_PER_DAY // 60:02d}:"
                           f"{leave % 60:02d} 离开，已超过闭园时间")})
        clock = leave

        if travel > MAX_LEG_MINUTES:
            issues.append({
                "kind": ISSUE_DISTANCE_UNREASONABLE, "stop_id": stop.stop_id,
                "detail": f"从上一站到 {stop.name} 需 {travel} 分钟，超过 {MAX_LEG_MINUTES} 分钟"})

        result.append(Stop(
            stop_id=stop.stop_id, name=stop.name, open_minute=stop.open_minute,
            close_minute=stop.close_minute, dwell_minutes=stop.dwell_minutes,
            ticket_price=stop.ticket_price, travel_minutes=travel,
            travel_estimated=estimated,
            arrive_minute=arrive, leave_minute=leave,
            latitude=stop.latitude, longitude=stop.longitude,
            evidence_ids=stop.evidence_ids, low_confidence=stop.low_confidence,
            closed=stop.closed))
        previous = stop

    total_travel = sum(s.travel_minutes for s in result)
    if result and total_travel > MAX_DAILY_TRAVEL_RATIO * MINUTES_PER_DAY:
        issues.append({
            "kind": ISSUE_DISTANCE_UNREASONABLE, "stop_id": "",
            "detail": (f"单日通勤合计 {total_travel} 分钟，"
                       f"超过可用时间的 {int(MAX_DAILY_TRAVEL_RATIO * 100)}%")})

    return result, issues


def plan_route(stops: Sequence[Stop], *, origin: tuple[float, float] | None = None,
               start_minute: int = DEFAULT_START_MINUTE,
               optimizer: Optimizer | None = None,
               legs: LegTable | None = None) -> tuple[list[Stop], list[dict[str, Any]]]:
    """排定顺序并计算时间轴，返回 (行程点序列, 校验问题)。

    `legs` 给了就用真实路线事实（U95）；没给就走 haversine 降级。
    """
    ordered = order_stops(stops, origin=origin, optimizer=optimizer)
    return build_timeline(ordered, legs=legs, start_minute=start_minute)


def low_confidence_stops(stops: Sequence[Stop]) -> tuple[str, ...]:
    """低置信信息提示标注（表 11 步骤3）。"""
    return tuple(s.name for s in stops if s.low_confidence)
