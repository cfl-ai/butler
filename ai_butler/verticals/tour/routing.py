"""行程编排与路线校验（裁定 U38）。

计划书依据
----------
- 五、第二阶段表 11「生成初步路线；**内置校验逻辑，检查路线时间冲突、距离不合理等问题**；
  条件判断交给 Jev 决策；**低置信信息做提示标注**」

裁定 U38
--------
规模化优化以 OR-Tools 为目标算法；本项目**先实现不依赖外部求解器的贪心最近邻 + 显式校验**，
OR-Tools 作为端口注入的升级路径。

**校验是底线，优化是增强** —— 所以校验永不缺席，求解器可以没有。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Sequence

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


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点球面距离（公里）。"""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _travel_minutes(a: Stop, b: Stop, *, kmph: float = 25.0) -> int:
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


def plan_route(stops: Sequence[Stop], *, origin: tuple[float, float] | None = None,
               start_minute: int = DEFAULT_START_MINUTE,
               optimizer: Optimizer | None = None) -> tuple[list[Stop], list[dict[str, Any]]]:
    """排定顺序并计算时间轴，返回 (行程点序列, 校验问题)。"""
    if not stops:
        return [], []

    order = list(optimizer(stops, origin)) if optimizer else greedy_order(stops, origin)
    if sorted(order) != list(range(len(stops))):
        raise ValueError("优化器返回的不是原点的排列")

    ordered: list[Stop] = []
    issues: list[dict[str, Any]] = []
    clock = start_minute
    previous: Stop | None = None

    for index in order:
        stop = stops[index]
        travel = _travel_minutes(previous, stop) if previous else 0
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

        ordered.append(Stop(
            stop_id=stop.stop_id, name=stop.name, open_minute=stop.open_minute,
            close_minute=stop.close_minute, dwell_minutes=stop.dwell_minutes,
            ticket_price=stop.ticket_price, travel_minutes=travel,
            latitude=stop.latitude, longitude=stop.longitude,
            evidence_ids=stop.evidence_ids, low_confidence=stop.low_confidence,
            closed=stop.closed))
        previous = stop

    total_travel = sum(s.travel_minutes for s in ordered)
    if ordered and total_travel > MAX_DAILY_TRAVEL_RATIO * MINUTES_PER_DAY:
        issues.append({
            "kind": ISSUE_DISTANCE_UNREASONABLE, "stop_id": "",
            "detail": (f"单日通勤合计 {total_travel} 分钟，"
                       f"超过可用时间的 {int(MAX_DAILY_TRAVEL_RATIO * 100)}%")})

    return ordered, issues


def low_confidence_stops(stops: Sequence[Stop]) -> tuple[str, ...]:
    """低置信信息提示标注（表 11 步骤3）。"""
    return tuple(s.name for s in stops if s.low_confidence)
