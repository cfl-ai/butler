"""电脑端定位（U105）：没有 GPS 的机器怎么知道自己「在哪」。

**为什么单列**：手机有 GPS，电脑没有。用户要求在电脑端做规划时
「用电脑测定位」——电脑能给的只有 **IP 归属地的城市级位置**（本机实测
`ipwho.is` 返回成都，经纬度 30.67/104.07）。

**这个定位的精度是城市级（数十公里），不是 GPS 级（米级）**，
因此 `PcFix.coarse` 恒为 True，并且：

- 用它做行程规划的**出发地**是合理的（城市级出发地本来就是城市）；
- 用它当「我现在站在哪条路上」是**不行的**——那必须走手机的 GPS；
- 两者不得混用：`coarse=True` 的定位进入逆地理时，结果要照实标注
  「基于城市级 IP 定位」，不能让用户以为系统知道他的精确位置。

**无网络时的行为**：不猜、不默认北京。抛错，由调用方决定是让用户
手动填坐标还是改用其他来源——静默返回一个「看起来合理」的城市
会让整条规划建立在假位置上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

__all__ = [
    "IPWHOIS_URL",
    "IpLocationError",
    "PcFix",
    "PcLocationError",
    "resolve_pc_location",
    "IpWhoIsLocation",
]

IPWHOIS_URL = "https://ipwho.is/"


class PcLocationError(RuntimeError):
    """电脑端定位失败。"""


#: 与 PcLocationError 同义的别名，便于调用方按「定位」语义捕获
IpLocationError = PcLocationError


@dataclass(frozen=True)
class PcFix:
    """一次电脑端定位结果。**恒为粗定位**。"""

    latitude: float
    longitude: float
    city: str = ""
    region: str = ""
    country: str = ""
    provider: str = "ipwhois"
    #: 粗定位标记。恒 True——IP 定位到不了米级，标 True 是诚实，
    #: 标 False 就是把城市级当 GPS 用。
    coarse: bool = True
    #: 名义精度（公里）。IP 定位的误差随运营商出口漂移，只作量级参考。
    accuracy_km: float = 25.0
    fetched_at: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {"latitude": self.latitude, "longitude": self.longitude,
                "city": self.city, "region": self.region,
                "country": self.country, "provider": self.provider,
                "coarse": self.coarse, "accuracy_km": self.accuracy_km,
                "fetched_at": self.fetched_at,
                "note": ("城市级 IP 定位（数十公里），不可当 GPS 用；"
                         "精确到道路需手机端 GPS")}

    def one_line(self) -> str:
        place = "/".join(x for x in (self.country, self.region, self.city) if x)
        return f"{place or '未知城市'}（IP 粗定位，约 {self.accuracy_km:.0f}km）"


class IpWhoIsLocation:
    """ipwho.is 的 IP 归属地定位。免费、无 key、本机实测可达。"""

    name = "ipwhois"

    def __init__(self, *, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None,
                 url: str = IPWHOIS_URL) -> None:
        if not url.startswith("https://"):
            raise PcLocationError(f"定位端点必须 https：{url!r}")
        self._timeout = timeout
        self._transport = transport
        self._url = url

    async def locate(self) -> PcFix:
        try:
            async with httpx.AsyncClient(timeout=self._timeout,
                                         transport=self._transport,
                                         follow_redirects=True) as client:
                response = await client.get(self._url)
                response.raise_for_status()
                payload: Any = response.json()
        except httpx.HTTPError as exc:
            raise PcLocationError(f"IP 定位请求失败：{exc}") from exc
        except ValueError as exc:
            raise PcLocationError(f"IP 定位返回不是 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise PcLocationError(f"IP 定位返回结构异常：{type(payload).__name__}")
        if payload.get("success") is False:
            raise PcLocationError(
                f"IP 定位未成功：{payload.get('message') or payload}")
        try:
            latitude = float(payload["latitude"])
            longitude = float(payload["longitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PcLocationError(
                f"IP 定位响应缺少坐标：{sorted(payload)}") from exc
        return PcFix(
            latitude=latitude, longitude=longitude,
            city=str(payload.get("city") or ""),
            region=str(payload.get("region") or ""),
            country=str(payload.get("country") or ""),
            provider=self.name,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


async def resolve_pc_location(*, override: tuple[float, float] | None = None,
                              port: Any = None) -> PcFix:
    """解析电脑端定位。

    优先级：
    1. `override`——显式给的坐标（用户手工填的、或测试注入的），最可靠；
    2. `port`——IP 定位端口；
    3. 都没有 → 抛错。**不猜一个默认城市**。
    """
    if override is not None:
        longitude, latitude = override
        return PcFix(latitude=latitude, longitude=longitude,
                     provider="override", coarse=True, accuracy_km=0.0,
                     fetched_at=datetime.now(timezone.utc).isoformat(
                         timespec="seconds"))
    if port is None:
        raise PcLocationError(
            "电脑端没有定位来源：既无显式坐标也没有 IP 定位端口。"
            "不猜默认城市——建立在假位置上的规划比没有规划更糟")
    return await port.locate()
