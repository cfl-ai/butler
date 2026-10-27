"""外部只读工具：天气 / POI（U118）。

为什么单独一个模块
------------------
工具处理器是**同步**的（`ToolDispatcher.dispatch` 是同步调用），而项目里已有的
真实外部端口是**异步**的（`verticals/tour/weather.py`、`verticals/tour/amap.py`）。
这里只做「同步 → 异步」的桥，**不重写 HTTP 与解析**：

- 天气：Open-Meteo（免费无 key，U96），复用 `WeatherCache` + `WeatherCollector`；
- POI：高德官方 `v3/place/text`（U109），复用 `AmapSource.search_places`。

U101/U108/U109 已经用真机把这两条路的参数坑踩完了（regeo 要 `经度,纬度` +
`coordsys`、官方天气 `extensions=all` 不回 `lives`、POI 检索必须 key 且不能走
拟人化旁路）。另写一套 HTTP 等于把踩过的坑再踩一遍（E-44：两处口径迟早分叉）。

桥的代价（如实写出来）
----------------------
同步桥会在**调用线程**上阻塞等网络返回。工具分发本身就是同步的
（`search_materials` 也是同步阻塞），所以这不是新引入的问题；
但它意味着「一次工具调用期间事件循环被占住」。当前装配点（对话入口、
调度器）本来就是串行跑单元，这个代价可接受；将来要并发跑多工具时，
应把 `ToolDispatcher.dispatch` 改成异步，而不是在这里加线程池。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, TypeVar

from ai_butler.verticals.tour.amap import AmapError, make_amap_source
from ai_butler.verticals.tour.sources import CollectError
from ai_butler.verticals.tour.weather import (
    MAX_FORECAST_DAYS,
    OpenMeteoWeather,
    WeatherCache,
    WeatherCollector,
)

T = TypeVar("T")

#: 一次天气采集最多等多久（秒）。超时如实报错，不返回「大概晴」。
WEATHER_TIMEOUT_SECONDS = 12.0
#: 天气缓存 TTL（秒）。U97：「实时」= 获取时间距当前 ≤ TTL。
WEATHER_TTL_SECONDS = 600.0
#: POI 单次最多返回几条。模型上下文有限，也给结果一个上限。
MAX_POI = 8


def _run(coro: Awaitable[T]) -> T:
    """在同步上下文里跑一个协程。

    **有运行中的事件循环时不能 `asyncio.run`**（会直接抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`），
    而工具分发恰好发生在 `run_units()` 这个协程里。所以有循环时丢到独立线程
    跑一个新循环：协程自己的上下文不被污染，也不去动外层循环的状态。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)          # type: ignore[arg-type]

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()  # type: ignore[arg-type]


def default_weather_port(settings: Any = None) -> WeatherCache:
    """默认天气端口：Open-Meteo + 10 分钟 TTL 缓存（U96/U97）。"""
    timezone_name = getattr(settings, "weather_timezone", "Asia/Shanghai")
    timeout = getattr(settings, "weather_timeout", WEATHER_TIMEOUT_SECONDS)
    return WeatherCache(
        OpenMeteoWeather(timezone_name=timezone_name, timeout=timeout),
        ttl_seconds=getattr(settings, "weather_ttl_seconds", WEATHER_TTL_SECONDS))


def fetch_weather(city: str, *, port: Any, days: int = 3) -> dict[str, Any]:
    """按地名取天气：实况 + 未来若干天预报（Open-Meteo，免费无 key，U119）。

    地名解析不到坐标时**如实失败**（`CollectError`），不猜一个坐标顶上。

    为什么默认带预报（`days=3`）：用户问「明天会下雨吗」「这几天怎么样」
    全都需要预报，而实况只能答「现在是几度」。只给实况时，模型会判定
    「天气工具查不到预报」转而调素材检索 —— 实测就是这样答出
    「我没有实时天气能力」的。能力有却不给到，等于没有（E-108 同族）。

    预报取不到时**不整体失败**：实况已经拿到就先把实况给出去，
    并用 `forecast_error` 如实说明预报缺席的原因。
    """
    name = (city or "").strip()
    if not name:
        raise ValueError("city 不得为空")
    try:
        fact = _run(WeatherCollector(port).fetch("weather", subject=name))
    except CollectError as exc:
        raise ValueError(f"取不到 {name!r} 的天气：{exc}") from exc
    payload = dict(fact.payload)
    result: dict[str, Any] = {
        "city": name,
        "place_name": payload.get("place_name") or payload.get("place", {}).get("name") or name,
        "temperature_c": payload.get("temperature_c"),
        "weather": payload.get("weather_text"),
        "wind_kmh": payload.get("wind_kmh"),
        "fetched_at": payload.get("fetched_at"),
        "from_cache": payload.get("from_cache"),
        "provider": fact.provider,
        # 不用 `low_confidence` 这个名字：模型会照着念「数据标注为低置信度」，
        # 用户看到的是「这个天气不准」。真实含义只是「同名地点不止一个」。
        "place_ambiguous": bool(fact.low_confidence),
        "note": ("同名地点不止一个，已按行政级别最高的一处解析" if fact.low_confidence else ""),
        "forecast": [],
        "forecast_error": "",
    }

    try:
        count = max(0, min(int(days), MAX_FORECAST_DAYS))
    except (TypeError, ValueError):
        count = 3
    if count == 0:
        result["forecast_error"] = "未请求预报（days=0）"
        return result

    latitude = float(payload.get("latitude") or 0.0)
    longitude = float(payload.get("longitude") or 0.0)
    try:
        rows = _run(port.forecast(latitude=latitude, longitude=longitude, days=count))
    except Exception as exc:                     # noqa: BLE001 —— 预报失败不拖垮实况
        result["forecast_error"] = f"预报不可用：{exc}"
        return result
    result["forecast"] = [row.as_payload() for row in rows]
    return result


def search_poi(keyword: str, *, city: str = "", limit: int = 5,
               source: Any) -> dict[str, Any]:
    """按关键字搜 POI（高德官方 v3/place/text，需 key）。

    无 key 时 `AmapSource` 会抛 `AmapError` —— 这里转成 `ValueError`，
    由 `ToolDispatcher` 表达成一次**失败的工具结果**（模型看到原因，链路不崩）。
    """
    kw = (keyword or "").strip()
    if not kw:
        raise ValueError("keyword 不得为空")
    try:
        count = max(1, min(int(limit), MAX_POI))
    except (TypeError, ValueError):
        count = 5
    try:
        pois = _run(source.search_places(keywords=kw, city=(city or "").strip(),
                                         offset=count))
    except AmapError as exc:
        raise ValueError(f"POI 检索失败：{exc}") from exc
    items = [{"name": p.name, "address": p.address, "type": p.poi_type,
              "location": list(p.location) if getattr(p, "location", None) else None,
              "adcode": getattr(p, "adcode", "")}
             for p in pois[:count]]
    return {"keyword": kw, "city": (city or "").strip(), "count": len(items),
            "pois": items, "provider": "amap", "official": True}


def build_amap_source(settings: Any = None) -> Any:
    """按配置造一个高德源。无 key 时返回的源**调用即报错**（不伪装可用）。"""
    if settings is None:
        return make_amap_source()
    key = (getattr(settings, "amap_key", "") or "").strip()
    return make_amap_source(key=key or None)


__all__ = [
    "MAX_POI", "WEATHER_TIMEOUT_SECONDS", "WEATHER_TTL_SECONDS",
    "build_amap_source", "default_weather_port", "fetch_weather", "search_poi",
]
