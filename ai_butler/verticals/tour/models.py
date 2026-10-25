"""旅游垂类数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 出行方式
TRANSPORT_MODES: tuple[str, ...] = ("walk", "transit", "drive", "bike")

#: 行程必填参数（表 11 步骤1 逐条）
from ai_butler.verticals.base import ParamSpec

TRIP_PARAM_SPECS: tuple[ParamSpec, ...] = (
    ParamSpec("destination", True, "目的地"),
    ParamSpec("start_date", True, "出行日期"),
    ParamSpec("days", True, "出行天数"),
    ParamSpec("party_size", True, "出行人数"),
    ParamSpec("budget", False, "预算"),
    ParamSpec("transport", False, "交通方式"),
    ParamSpec("preferences", False, "偏好"),
    ParamSpec("taboos", False, "禁忌"),
    ParamSpec("depart_minute", False, "出发时刻"),
)

#: 分钟制时间轴（当天 0 点起的分钟数）
MINUTES_PER_DAY = 24 * 60

#: 未给出发时刻时的假定值（09:00）。**是假定，不是事实**，必须随结果标注。
DEFAULT_DEPART_MINUTE = 9 * 60


def clock_text(minute: int | None) -> str:
    """分钟制 → "HH:MM"。跨日（>24h）如实带上 +1d 前缀，不静默取模。"""
    if minute is None:
        return "--:--"
    day, rest = divmod(int(minute), MINUTES_PER_DAY)
    text = f"{rest // 60:02d}:{rest % 60:02d}"
    return f"+{day}d {text}" if day else text


@dataclass(frozen=True)
class Stop:
    """行程中的一个停留点。"""

    stop_id: str
    name: str
    #: 开放/关闭时刻（分钟制）；None 表示全天开放
    open_minute: int | None = None
    close_minute: int | None = None
    dwell_minutes: int = 60
    ticket_price: float = 0.0
    #: 与上一个点的预计通行时间（分钟），由规划器填
    travel_minutes: int = 0
    #: 抵达 / 离开的钟点（分钟制），由时间轴算出的**真实排程结果**。
    #: 只有 travel_minutes 而没有钟点，用户还得自己拿出发时间一路加——
    #: 「规划含时间」就落空了。
    arrive_minute: int | None = None
    leave_minute: int | None = None
    #: 上一段通行时间是否为**估计**（haversine 降级或无坐标；U95）
    travel_estimated: bool = False
    latitude: float | None = None
    longitude: float | None = None
    #: 证据（索引进公共 Workspace 的 source / claim）
    evidence_ids: tuple[str, ...] = ()
    #: 是否为低置信信息（表 11 步骤3「低置信信息做提示标注」）
    low_confidence: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.stop_id or not self.name:
            raise ValueError("停留点必须有 id 与名称")
        if self.dwell_minutes < 0 or self.ticket_price < 0:
            raise ValueError("停留时间与票价不得为负")


@dataclass(frozen=True)
class TripRequest:
    destination: str
    start_date: str
    days: int
    party_size: int
    budget: float | None = None
    transport: str = "transit"
    preferences: tuple[str, ...] = ()
    taboos: tuple[str, ...] = ()
    #: 出发时刻（分钟制）。None = 未指定，按 DEFAULT_DEPART_MINUTE 假定
    depart_minute: int | None = None

    def __post_init__(self) -> None:
        if self.days <= 0:
            raise ValueError("出行天数必须为正")
        if self.party_size <= 0:
            raise ValueError("出行人数必须为正")
        if self.transport not in TRANSPORT_MODES:
            raise ValueError(f"未知交通方式 {self.transport!r}，可选 {list(TRANSPORT_MODES)}")
        if self.depart_minute is not None and not (
                0 <= self.depart_minute < MINUTES_PER_DAY):
            raise ValueError(
                f"出发时刻必须在 0..{MINUTES_PER_DAY - 1} 分钟内，收到 {self.depart_minute}")

    @property
    def depart_assumed(self) -> bool:
        """出发时刻是不是我们假定的——假定的钟点必须让用户看见。"""
        return self.depart_minute is None


@dataclass(frozen=True)
class CostItem:
    name: str
    amount: float
    kind: str = "ticket"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("费用不得为负")


@dataclass
class TripPlan:
    request: TripRequest
    stops: list[Stop] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    costs: list[CostItem] = field(default_factory=list)
    replanned: bool = False
    #: 路线依据（U94/U95）：`osrm:<profile>` = 真实路网；`haversine` = 几何估计；
    #: `mixed` = 部分段落降级。**没有这个字段，「基于真实」就是一句客气话。**
    route_basis: str = "haversine"
    #: 降级来源（transit 无免费真实源 → car，U94）；None 表示未降级
    degraded_from: str | None = None
    #: 取真实路线时的逐段失败原因（禁回退纪律的可见性；U95）
    routing_failures: tuple[str, ...] = ()
    #: 时间轴的出发时刻（分钟制）
    start_minute: int = DEFAULT_DEPART_MINUTE

    @property
    def total_cost(self) -> float:
        return round(sum(c.amount for c in self.costs), 2)

    @property
    def total_travel_minutes(self) -> int:
        return sum(s.travel_minutes for s in self.stops)

    @property
    def end_minute(self) -> int | None:
        """行程结束时刻。没有停留点时 None——不假装是 0（那会是 00:00）。"""
        leaves = [s.leave_minute for s in self.stops if s.leave_minute is not None]
        return max(leaves) if leaves else None

    @property
    def depart_assumed(self) -> bool:
        return self.request.depart_minute is None

    def timeline(self) -> list[dict[str, Any]]:
        """逐站钟点表：用户要的「包括时间」就在这里。"""
        rows: list[dict[str, Any]] = []
        for stop in self.stops:
            rows.append({
                "stop_id": stop.stop_id, "name": stop.name,
                "arrive": clock_text(stop.arrive_minute),
                "leave": clock_text(stop.leave_minute),
                "travel_minutes": stop.travel_minutes,
                "travel_estimated": stop.travel_estimated,
                "dwell_minutes": stop.dwell_minutes,
            })
        return rows

    @property
    def estimate_only(self) -> bool:
        """整条路线是否**没有任何一段**走在真实路网上。"""
        return self.route_basis == "haversine"

    def as_summary(self) -> dict[str, Any]:
        """用于写入私有 Vault 的 **synthesis** 综合摘要页面（表 11）。"""
        return {
            "destination": self.request.destination,
            "start_date": self.request.start_date,
            "days": self.request.days,
            "party_size": self.request.party_size,
            "transport": self.request.transport,
            "stops": [s.name for s in self.stops],
            "total_cost": self.total_cost,
            "total_travel_minutes": self.total_travel_minutes,
            # 时间轴：出发/结束钟点 + 逐站钟点。假定出发时刻必须标出来，
            # 否则用户会以为这个 09:00 是他自己填的。
            "depart": clock_text(self.start_minute),
            "end": clock_text(self.end_minute),
            "depart_assumed": self.depart_assumed,
            "timeline": self.timeline(),
            "issues": list(self.issues),
            "replanned": self.replanned,
            #: 路线依据与降级必须进 synthesis 摘要（U95）：用户按估计时间
            #: 赶火车时，误点的责任在没人告诉他这是估计。
            "route_basis": self.route_basis,
            "estimate_only": self.estimate_only,
            "degraded_from": self.degraded_from,
            "routing_failures": list(self.routing_failures),
        }
