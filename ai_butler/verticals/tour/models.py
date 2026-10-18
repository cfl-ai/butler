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
)

#: 分钟制时间轴（当天 0 点起的分钟数）
MINUTES_PER_DAY = 24 * 60


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

    def __post_init__(self) -> None:
        if self.days <= 0:
            raise ValueError("出行天数必须为正")
        if self.party_size <= 0:
            raise ValueError("出行人数必须为正")
        if self.transport not in TRANSPORT_MODES:
            raise ValueError(f"未知交通方式 {self.transport!r}，可选 {list(TRANSPORT_MODES)}")


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

    @property
    def total_cost(self) -> float:
        return round(sum(c.amount for c in self.costs), 2)

    @property
    def total_travel_minutes(self) -> int:
        return sum(s.travel_minutes for s in self.stops)

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
            "issues": list(self.issues),
            "replanned": self.replanned,
        }
