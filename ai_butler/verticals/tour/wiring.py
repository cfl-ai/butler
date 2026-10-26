"""旅游垂类的生产装配点（U110，填 E-86 的「有实现无装配」）。

计划书依据
----------
- 五、第二阶段表 11「旅游 Agent 属于**下垂类业务执行单元**，**不负责意图判断、
  任务分发**」；它的真实端口（采集 / 路线）此前只在 `tests/` 触达，
  没有被生产装配点接进来——这正是 E-86「有实现有测试 ≠ 有装配」的形态。

本模块把 `TourAgent` 接上真实端口，并规定它是**唯一装配点**（与 U108 的
`make_amap_source` 同形）：调用方不得自己 `TourAgent(...)`，否则会绕过
「官方 key 注入即切官方端点」「路线缺失即逐段降级」这些不变量。

装配的端口
----------
- 采集：`TourCollector` —— 天气走 Open-Meteo（真实、无 key、CC BY 4.0，带 TTL 缓存）；
  景区走高德逆地理（有 key 走官方、无 key 走拟人化，U108/U101）；交通本机无免费
  真实源，返回 low_confidence 的降级事实，**不假装成功、不报错**。
- 路线：`OSRMRouter`（U94/U95）—— 自带「逐段失败隔离降级」，缺网也不会整单崩。
- 路线优化器：不注入，用内置贪心最近邻（U38）。
"""

from __future__ import annotations

from typing import Any

from ai_butler.core.config import Settings, get_settings
from ai_butler.verticals.tour.agent import TourAgent
from ai_butler.verticals.tour.amap import make_amap_source
from ai_butler.verticals.tour.live_routing import OSRMRouter
from ai_butler.verticals.tour.sources import (
    SOURCE_SCENIC,
    SOURCE_TRAFFIC,
    SOURCE_WEATHER,
    CollectedFact,
)
from ai_butler.verticals.tour.weather import (
    OpenMeteoWeather,
    WeatherCache,
    WeatherCollector,
)


class TourCollector:
    """生产装配用的组合采集端口（U110）。

    把多个真实源桥接成 `sources.CollectorPort`：`fetch(kind, subject)` 按类别分发。
    任何本机没有免费真实源的类别，照实返回 `low_confidence` 的降级事实——
    这是「降级」不是「静默成功」（表 12 纪律）。
    """

    def __init__(self, weather_port: Any, *, amap: Any = None) -> None:
        #: 天气端口（current + geocode），如 Open-Meteo / WeatherCache
        self._weather = weather_port
        #: 高德源（AmapSource），用于景区逆地理；无 key 时调用方传 None（景区降级）
        self._amap = amap

    async def fetch(self, kind: str, *, subject: str) -> CollectedFact:
        if kind == SOURCE_WEATHER:
            # 天气是真实可采的（Open-Meteo 免费无 key）；地名 → 坐标由端口内 geocode 处理。
            return await WeatherCollector(self._weather).fetch(kind, subject=subject)
        if kind == SOURCE_SCENIC and self._amap is not None:
            return await self._fetch_scenic(subject)
        # 交通（及无高德 key 时的景区）本机无免费真实源：如实降级，不当成成功。
        return self._degraded(kind, subject)

    async def _fetch_scenic(self, subject: str) -> CollectedFact:
        """景区信息 = 地名解析 → 坐标 → 高德逆地理（行政层级 + 邻近道路）。"""
        point = await self._weather.geocode(subject)
        if point is None:
            return self._degraded(SOURCE_SCENIC, subject,
                                 note="地名解析不到坐标，景区信息缺失")
        place = await self._amap.regeo(longitude=point.longitude,
                                      latitude=point.latitude,
                                      datum_of_input="wgs84")
        return CollectedFact(
            kind=SOURCE_SCENIC, subject=subject, provider=place.provider,
            evidence_id=f"amap:{place.adcode}:{subject}",
            payload={
                "country": place.country, "province": place.province,
                "city": place.city, "district": place.district,
                "adcode": place.adcode, "official": place.official,
                "nearest_road": (None if place.nearest_road is None
                                 else place.nearest_road.name),
                "hierarchy": list(place.hierarchy()),
            },
            low_confidence=False,
        )

    @staticmethod
    def _degraded(kind: str, subject: str,
                  note: str = "暂无免费可达的真实源，已降级（不假装有）") -> CollectedFact:
        return CollectedFact(
            kind=kind, subject=subject, provider="unavailable",
            evidence_id=f"unavailable:{kind}:{subject}",
            payload={"status": "unavailable", "note": note},
            low_confidence=True,
        )


def build_tour_agent(*, settings: Settings | None = None, collector: Any = None,
                     weather_port: Any = None, amap: Any = None,
                     routing: Any = None) -> TourAgent:
    """TourAgent 的生产装配点（U110，填 E-86）。

    **唯一装配点**：调用方不应自己 `TourAgent(...)`，否则会绕过「官方 key 注入即
    切官方端点」「路线缺失即逐段降级」这些不变量。

    `collector` / `weather_port` / `amap` / `routing` 均可在测试中整体或部分覆盖，
    以便走假实现、不发真实网络请求；不覆盖时用项目默认的真实源。
    """
    if settings is None:
        settings = get_settings()

    if collector is None:
        if weather_port is None:
            weather_port = WeatherCache(
                OpenMeteoWeather(timezone_name=settings.weather_timezone,
                                timeout=settings.weather_timeout),
                ttl_seconds=settings.weather_ttl_seconds)
        if amap is None:
            amap = make_amap_source()
        # 无官方 key 时高德逆地理只能走拟人化旁路，景区信息不稳，故降级为 None，
        # 让景区走 `_degraded`——不拿拟人化旁路冒充可靠景区源。
        collector = TourCollector(weather_port, amap=amap if amap.official else None)

    if routing is None:
        routing = OSRMRouter()          # 自带逐段失败隔离降级（U94/U95）

    # 路线优化器不注入 → 内置贪心最近邻（U38）
    return TourAgent(collector=collector, optimizer=None, routing=routing)
