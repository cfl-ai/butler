"""实时天气采集（U96/U97）。

计划书依据
----------
- 五、第二阶段表 11「实时信息查询：景区开放状态、**天气**、交通、门票、排队情况」
- 表 11「公共数据：**天气 API**、景区公开信息、交通实时状态；统一清洗、来源标记、去重」

裁定 U96（2026-10-26）
---------------------
主源 **Open-Meteo**（免费、无 key、CC BY 4.0）。U37 原选的和风天气保留为
**可选增强**——需要 key，本项目没有配置；没有 key 就明说没有，
不拿别家的数据冒充和风。地理编码（地名→坐标）用同一提供方的免费 API；
 nominatim 本机实测连接重置，不采用（U71「不做地址反查」的立场在地名解析
 上有一个例外：不反查「精确地址」，只解析「城市名」，且结果带歧义标记）。

裁定 U97（2026-10-26）
---------------------
「实时」必须被定义成可检验的语义：**数据获取时间距当前 ≤ TTL（默认 10 分钟）**。
每条天气事实带 `fetched_at`；缓存只吐新鲜数据，过期重新采集；
采集失败照实报错（表 12：失败不回退到「假装成功」）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

import httpx

from ai_butler.verticals.tour.sources import CollectedFact, CollectError

#: Open-Meteo 天气预报 API（免费无 key）
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
#: Open-Meteo 地理编码 API（免费无 key；地名 → 坐标）
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

#: WMO 天气码 → 中文（Open-Meteo `weather_code` 用的就是 WMO 4677 码表）
WMO_CODE_TEXT: dict[int, str] = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "小雨", 55: "中毛毛雨",
    56: "冻毛毛雨", 57: "冻小雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


class WeatherError(RuntimeError):
    """天气采集失败。**禁止回退**（表 12）：拿旧数据冒充新采集是回退的一种。"""


@dataclass(frozen=True)
class GeoPoint:
    """地名解析结果（U96）。"""

    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""
    #: 同名地点数量 > 1 时为 True（如「北京」在重庆也有一个）——
    #: 解析带歧义时必须在输出里说，不能悄悄取第一条当唯一答案
    ambiguous: bool = False


@dataclass(frozen=True)
class WeatherFact:
    """一条当前天气事实。**公共数据**，不含任何用户私有信息。"""

    latitude: float
    longitude: float
    temperature_c: float
    weather_code: int
    weather_text: str
    wind_kmh: float
    #: 获取时间（UTC ISO8601）——「实时」的可检验锚点（U97）
    fetched_at: str
    provider: str
    #: 地名解析结果（经 geocode 采集时有；直接给坐标时为 None）
    place: GeoPoint | None = None
    #: 是否来自缓存（U97：走缓存与走源必须可区分）
    from_cache: bool = False

    def age_seconds(self, *, now: float | None = None) -> float:
        """距获取时间过了多少秒。"""
        try:
            stamp = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
        except ValueError:
            return float("inf")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (now if now is not None else time.time())
                   - stamp.timestamp())

    def is_stale(self, *, ttl_seconds: float, now: float | None = None) -> bool:
        """是否已超 TTL（U97 的「实时」判定）。"""
        return self.age_seconds(now=now) > ttl_seconds

    def as_payload(self) -> dict[str, Any]:
        """转为采集结果 payload（source 页存原始码 + 译文两份）。"""
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "temperature_c": self.temperature_c,
            "weather_code": self.weather_code,
            "weather_text": self.weather_text,
            "wind_kmh": self.wind_kmh,
            "fetched_at": self.fetched_at,
            "from_cache": self.from_cache,
            "place": (None if self.place is None else {
                "name": self.place.name, "country": self.place.country,
                "admin1": self.place.admin1, "ambiguous": self.place.ambiguous}),
        }


class WeatherPort(Protocol):
    """天气采集端口。真机实现走 Open-Meteo；测试注入假实现。"""

    async def current(self, *, latitude: float, longitude: float) -> WeatherFact: ...

    async def geocode(self, name: str) -> GeoPoint | None: ...


class OpenMeteoWeather:
    """Open-Meteo 实现：当前天气 + 地名解析（U96）。

    `transport` 是测试缝（`httpx.MockTransport`）；`forecast_url` /
    `geocode_url` 可覆盖以便指向镜像。
    """

    name = "open-meteo"

    def __init__(self, *, timeout: float = 10.0,
                 timezone_name: str = "UTC",
                 forecast_url: str = OPEN_METEO_FORECAST_URL,
                 geocode_url: str = OPEN_METEO_GEOCODE_URL,
                 transport: httpx.BaseTransport | None = None) -> None:
        for url in (forecast_url, geocode_url):
            if not url.startswith("https://"):
                raise WeatherError(f"Open-Meteo URL 必须 https：{url!r}")
        self._timeout = timeout
        # 固定 UTC：`current.time` 原样进 fetched_at，本地时间会让
        # 「距现在多久」差出整个时区偏移（本模块第一版就踩了这个坑）
        self._timezone = timezone_name
        self._forecast_url = forecast_url
        self._geocode_url = geocode_url
        self._transport = transport

    async def current(self, *, latitude: float, longitude: float) -> WeatherFact:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": self._timezone,
        }
        async with httpx.AsyncClient(timeout=self._timeout,
                                     transport=self._transport) as client:
            try:
                response = await client.get(self._forecast_url, params=params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise WeatherError(f"Open-Meteo 天气预报请求失败：{exc}") from exc
            data: Mapping[str, Any] = response.json()
        current = data.get("current") or {}
        try:
            code = int(current["weather_code"])
            temperature = float(current["temperature_2m"])
        except (KeyError, TypeError, ValueError) as exc:
            # 错误信息要指名缺的是哪个字段。写成「缺少 current 字段：['time']」
            # 会把「现有字段」当成「缺少字段」，排查时指向完全相反的方向。
            missing = [k for k in ("weather_code", "temperature_2m")
                       if k not in current]
            raise WeatherError(
                f"Open-Meteo 响应缺少字段 {missing}"
                f"（current 现有：{sorted(current)}）") from exc
        fetched_at = str(current.get("time") or "")
        return WeatherFact(
            latitude=float(data.get("latitude", latitude)),
            longitude=float(data.get("longitude", longitude)),
            temperature_c=round(temperature, 1),
            weather_code=code,
            weather_text=WMO_CODE_TEXT.get(code, f"未知天气码 {code}"),
            wind_kmh=round(float(current.get("wind_speed_10m") or 0.0), 1),
            fetched_at=fetched_at,
            provider=self.name,
        )

    async def geocode(self, name: str) -> GeoPoint | None:
        query = (name or "").strip()
        if not query:
            return None
        params = {"name": query, "count": 5, "language": "zh", "format": "json"}
        async with httpx.AsyncClient(timeout=self._timeout,
                                     transport=self._transport) as client:
            try:
                response = await client.get(self._geocode_url, params=params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise WeatherError(f"Open-Meteo 地名解析请求失败：{exc}") from exc
            data: Mapping[str, Any] = response.json()
        results = data.get("results") or []
        if not results:
            return None
        top = results[0]
        try:
            return GeoPoint(
                name=str(top.get("name") or query),
                latitude=float(top["latitude"]),
                longitude=float(top["longitude"]),
                country=str(top.get("country") or ""),
                admin1=str(top.get("admin1") or ""),
                ambiguous=len(results) > 1,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError(f"地名解析响应缺少坐标字段：{exc}") from exc


class WeatherCache:
    """TTL 缓存 + 单飞（U97）。

    - 只吐**新鲜**数据（age ≤ ttl）；过期重新采集；
    - 同一坐标并发只打一次源（单飞）——免费 API 有限流，
    一次页面刷新不该变成 N 次上游请求；
    - 采集失败照实抛出，**不吐陈旧数据冒充新鲜**。
    """

    def __init__(self, port: WeatherPort, *, ttl_seconds: float = 600.0) -> None:
        if ttl_seconds <= 0:
            raise WeatherError("weather TTL 必须为正")
        self._port = port
        self._ttl = ttl_seconds
        self._facts: dict[tuple[float, float], WeatherFact] = {}
        self._inflight: dict[tuple[float, float], asyncio.Future] = {}

    @staticmethod
    def _key(latitude: float, longitude: float) -> tuple[float, float]:
        # 与 U71 同口径的粗化：3 位小数 ≈ 110 m。
        # 不粗化则「北京」两次查询坐标差 1e-6 就会打两次源。
        return (round(latitude, 3), round(longitude, 3))

    async def current(self, *, latitude: float, longitude: float) -> WeatherFact:
        key = self._key(latitude, longitude)
        cached = self._facts.get(key)
        if cached is not None and not cached.is_stale(ttl_seconds=self._ttl):
            # dataclasses.replace 保里子（place 等）只换 from_cache 标记
            return replace(cached, from_cache=True)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(
                self._port.current(latitude=latitude, longitude=longitude))
            self._inflight[key] = task
        try:
            fact = await asyncio.shield(task)
        finally:
            # shield 让等待方被取消时不牵连共享任务；
            # 只摘自己放进去的那个，别把后来者新建的任务顺手清掉
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
        self._facts[key] = fact
        return fact

    async def geocode(self, name: str) -> GeoPoint | None:
        return await self._port.geocode(name)


class WeatherCollector:
    """把天气端口桥接成统一采集端口（U37 的 `CollectorPort`）。

    `subject` 两种形态：`"lat,lon"` 直接取坐标；其它按地名解析
    （解析失败 = 采集失败，记入 `failures`，不猜坐标）。
    """

    def __init__(self, weather: WeatherPort) -> None:
        self._weather = weather

    async def fetch(self, kind: str, *, subject: str) -> CollectedFact:
        if kind != "weather":
            raise CollectError(
                f"WeatherCollector 只服务 weather，不服务 {kind!r}："
                f"没有免费可达的真实源，不假装有（U96）")
        text = (subject or "").strip()
        place: GeoPoint | None = None
        if text:
            head = text.split(",", 1)
            try:
                latitude, longitude = float(head[0]), float(head[1])
            except (IndexError, ValueError):
                point = await self._weather.geocode(text)
                if point is None:
                    raise CollectError(f"地名 {text!r} 解析不到坐标")
                place, latitude, longitude = point, point.latitude, point.longitude
        else:
            raise CollectError("天气采集必须有地名或坐标")
        fact = await self._weather.current(latitude=latitude, longitude=longitude)
        evidence_id = f"{fact.provider}:{fact.fetched_at}"
        return CollectedFact(
            kind="weather",
            subject=text or f"{latitude:.3f},{longitude:.3f}",
            payload={**fact.as_payload(),
                     **({"place_name": place.name} if place else {})},
            provider=fact.provider,
            evidence_id=evidence_id,
            low_confidence=bool(place and place.ambiguous),
        )
