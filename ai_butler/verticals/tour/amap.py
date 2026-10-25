"""高德真实源：逆地理（行政层级 + 当前道路）与实时天气。

**接入方式必须说清楚（U101）**

本模块走的是高德地图**网站自身**在用的接口（人打开 `www.amap.com`
时浏览器实际请求的那些），而不是高德开放平台的官方 Web 服务 API——
后者的 `restapi.amap.com` 每一项都要 `key`，本机实测返回
`INVALID_USER_KEY`，而注册官方 key 需要手机号 + 短信 + 实名认证，
非本机可自行完成的事项。

因此本模块的性质是**拟人化旁路**，并且必须如实承担三件事：

1. `official=False` 写进每条产出物，**不得**自称官方接口；
2. 它随时可能因风控（POI 检索接口已经撞上了阿里风控页）或改版失效，
   失败照实上报，不静默回退到另一个源冒充「还是拿到了」；
3. 官方 key 一旦注入（见 `AmapSource(key=...)`），立刻改走官方端点，
   且 `official` 变 True——拟人化只作为无可 key 时的过渡。

**它给出什么（实测口径）**

- 逆地理：`中国 / 北京市 / 北京市 / 东城区` + adcode 110101，
  并附 `road_list`（最近的几条真实道路 + 距离 + 方位）与 `pos`
  自然语言描述（「在333省道旁边，靠近乐凯北大街--惠民大街路口」）。
  **镇/乡一级该接口不返回**——只能靠官方 key，故如实置 None 并注明。
- 天气：以 adcode 查询，返回 `report_time`（高德服务器时间）+ `live`
  实测 + 逐时段预报。这就是「天气在该地图事实之上实时刷新」的接法：
  先定位拿到 adcode，再用 adcode 取天气，两步串成一条链。

**坐标基准**：高德用 GCJ-02。进来是 WGS-84 的（设备 GPS）要先过
`datum.wgs84_to_gcj02`，否则点位偏移数百米。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from ai_butler.verticals.tour.datum import wgs84_to_gcj02

__all__ = [
    "AMAP_REGEO_URL",
    "AMAP_WEATHER_URL",
    "AMAP_OFFICIAL_REGEO_URL",
    "AMAP_OFFICIAL_WEATHER_URL",
    "AmapError",
    "AmapForecastSlot",
    "AmapPlace",
    "AmapSource",
    "AmapWeather",
    "Road",
]

#: 拟人化端点（www.amap.com 自用）
AMAP_REGEO_URL = "https://www.amap.com/service/regeo"
AMAP_WEATHER_URL = "https://www.amap.com/service/weather"
#: 官方 Web 服务端点（需 key；有 key 时优先）
AMAP_OFFICIAL_REGEO_URL = "https://restapi.amap.com/v3/geocode/regeo"
AMAP_OFFICIAL_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.amap.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

#: 高德天气现象码 → 中文。未知的照实标注，不猜。
AMAP_WEATHER_CODE_TEXT: Mapping[str, str] = {
    "00": "晴", "01": "多云", "02": "阴", "03": "阵雨", "04": "雷阵雨",
    "05": "雷阵雨并伴有冰雹", "06": "雨夹雪", "07": "小雨", "08": "中雨",
    "09": "大雨", "10": "暴雨", "11": "大暴雨", "12": "特大暴雨",
    "13": "阵雪", "14": "小雪", "15": "中雪", "16": "大雪", "17": "暴雪",
    "18": "雾", "19": "冻雨", "20": "沙尘暴", "21": "小到中雨",
    "22": "中到大雨", "23": "大到暴雨", "24": "暴雨到大暴雨",
    "25": "大暴雨到特大暴雨", "26": "小到中雪", "27": "中到大雪",
    "28": "大到暴雪", "29": "浮尘", "30": "扬沙", "31": "强沙尘暴",
    "53": "霾", "99": "无",
}


class AmapError(RuntimeError):
    """高德源取数失败。调用方按表 12 处置：记 failures，**不静默回退**。"""


@dataclass(frozen=True)
class Road:
    """一条真实道路（逆地理返回的邻近道路）。"""

    name: str
    #: 到该道路的距离（米）
    distance_m: float
    #: 方位，如 "North" / "SouthWest"
    direction: str
    #: 道路等级（高德内部）
    level: str = ""
    #: 路宽（米）
    width_m: float = 0.0
    road_id: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {"name": self.name, "distance_m": self.distance_m,
                "direction": self.direction, "level": self.level,
                "width_m": self.width_m, "road_id": self.road_id}


@dataclass(frozen=True)
class AmapPlace:
    """一个坐标点对应的真实行政区划 + 所在道路。

    `township` 在拟人化接口下恒为 None（镇/乡需官方 key），
    `hierarchy()` 只列真正拿到的层级——不拿城市名补位。
    """

    longitude: float
    latitude: float
    country: str = ""
    province: str = ""
    city: str = ""
    district: str = ""
    adcode: str = ""
    #: 高德给的原始拼接串，如 "河北省,保定市,徐水区"
    desc: str = ""
    #: 邻近道路，按距离升序
    roads: tuple[Road, ...] = ()
    #: 门牌/地标（extensions=all 才有）
    landmark: str = ""
    #: 自然语言位置描述，如「在333省道旁边」
    pos_text: str = ""
    township: str | None = None
    provider: str = "amap-web"
    official: bool = False
    fetched_at: str = ""

    @property
    def nearest_road(self) -> Road | None:
        return self.roads[0] if self.roads else None

    def hierarchy(self) -> tuple[str, ...]:
        """真实拿到的行政层级，从大到小。拿不到的层级不出现，也不补位。"""
        levels = (self.country, self.province, self.city, self.district,
                  self.township or "")
        return tuple(x for x in levels if x)

    def as_payload(self) -> dict[str, Any]:
        return {
            "longitude": self.longitude, "latitude": self.latitude,
            "country": self.country, "province": self.province,
            "city": self.city, "district": self.district,
            "township": self.township,
            "township_note": ("" if self.township else
                              "镇/乡一级需高德官方 Web 服务 key，拟人化接口不返回"),
            "adcode": self.adcode, "desc": self.desc,
            "hierarchy": list(self.hierarchy()),
            "nearest_road": (self.nearest_road.as_payload()
                             if self.nearest_road else None),
            "roads": [r.as_payload() for r in self.roads],
            "landmark": self.landmark, "pos_text": self.pos_text,
            "provider": self.provider, "official": self.official,
            "datum": "GCJ-02",
            "fetched_at": self.fetched_at,
        }

    def one_line(self) -> str:
        """「现在在什么地方、什么路」的一句话答案。"""
        where = "/".join(self.hierarchy()) or "未知位置"
        road = self.nearest_road
        road_text = f"，最近道路 {road.name}（{road.distance_m:.0f}m）" if road else ""
        return f"{where}{road_text}"


@dataclass(frozen=True)
class AmapForecastSlot:
    """一个预报时段（白天/夜间）。"""

    daynight: int
    weather_name: str
    min_temp_c: float | None = None
    max_temp_c: float | None = None
    wind_direction: str = ""
    wind_power: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {"daynight": self.daynight,
                "part": "白天" if self.daynight == 0 else "夜间",
                "weather_name": self.weather_name,
                "min_temp_c": self.min_temp_c, "max_temp_c": self.max_temp_c,
                "wind_direction": self.wind_direction,
                "wind_power": self.wind_power}


@dataclass(frozen=True)
class AmapWeather:
    """某 adcode 的实时天气 + 预报。"""

    adcode: str
    #: 高德服务器报告的观测时间（本地时间串，UTC+8）
    report_time: str = ""
    weather_name: str = ""
    weather_code: str = ""
    temperature_c: float | None = None
    forecasts: tuple[AmapForecastSlot, ...] = ()
    provider: str = "amap-web"
    official: bool = False
    #: 本条事实的取得时刻（UTC，无歧义）——陈旧判定一律以它为准
    fetched_at: str = ""

    def age_seconds(self, *, now: float | None = None) -> float:
        """距取得时刻的秒数。解析不出 fetched_at 就当无限陈旧。"""
        try:
            stamp = datetime.fromisoformat(self.fetched_at).timestamp()
        except (TypeError, ValueError):
            return float("inf")
        return max(0.0, (now if now is not None else time.time()) - stamp)

    def is_stale(self, *, ttl_seconds: float, now: float | None = None) -> bool:
        return self.age_seconds(now=now) > ttl_seconds

    def as_payload(self) -> dict[str, Any]:
        return {"adcode": self.adcode, "report_time": self.report_time,
                "weather_name": self.weather_name, "weather_code": self.weather_code,
                "temperature_c": self.temperature_c,
                "forecasts": [f.as_payload() for f in self.forecasts],
                "provider": self.provider, "official": self.official,
                "fetched_at": self.fetched_at}

    def one_line(self) -> str:
        temp = (f"{self.temperature_c:.0f}℃" if self.temperature_c is not None
                else "温度未知")
        return f"{self.weather_name or '未知天气'} {temp}（观测 {self.report_time}）"


class AmapSource:
    """高德取数客户端。

    `key` 给了就走官方端点（official=True）；不给则走拟人化旁路，
    并在每条产出物上标明 `official=False`。
    """

    def __init__(self, *, key: str = "", timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None,
                 regeo_url: str | None = None,
                 weather_url: str | None = None) -> None:
        self._key = (key or "").strip()
        self._timeout = timeout
        self._transport = transport
        if regeo_url is None:
            regeo_url = (AMAP_OFFICIAL_REGEO_URL if self._key
                         else AMAP_REGEO_URL)
        if weather_url is None:
            weather_url = (AMAP_OFFICIAL_WEATHER_URL if self._key
                            else AMAP_WEATHER_URL)
        for url in (regeo_url, weather_url):
            if not url.startswith("https://"):
                raise AmapError(f"高德端点必须 https：{url!r}")
        self._regeo_url = regeo_url
        self._weather_url = weather_url

    @property
    def official(self) -> bool:
        return bool(self._key)

    def _headers(self) -> dict[str, str]:
        # 官方 API 不需要拟浏览器头（带了反而像在伪装浏览器）；
        # 拟人化旁路必须带，否则 www.amap.com 直接拒。
        return {} if self._key else dict(_BROWSER_HEADERS)

    # ------------------------------------------------------------------ #
    #  逆地理
    # ------------------------------------------------------------------ #
    async def regeo(self, *, longitude: float, latitude: float,
                    datum_of_input: str = "wgs84") -> AmapPlace:
        """坐标 → 行政区层级 + 当前道路。

        `datum_of_input`：设备 GPS 是 wgs84，高德要 gcj02，进来先转换。
        传 "gcj02" 表示调用方已确认坐标就在火星系上（如高德自己返回的）。
        """
        if datum_of_input not in ("wgs84", "gcj02"):
            raise AmapError(f"未知坐标基准：{datum_of_input!r}")
        lon, lat = ((wgs84_to_gcj02(longitude, latitude))
                    if datum_of_input == "wgs84" else (longitude, latitude))
        params: dict[str, Any] = {"longitude": f"{lon:.6f}",
                                  "latitude": f"{lat:.6f}"}
        if self._key:
            params["key"] = self._key
            params["extensions"] = "all"
        else:
            params["extensions"] = "all"
        data = await self._get(self._regeo_url, params)
        return self._parse_place(data, longitude=longitude, latitude=latitude,
                                 query_lon=lon, query_lat=lat)

    def _parse_place(self, data: Mapping[str, Any], *, longitude: float,
                     latitude: float, query_lon: float,
                     query_lat: float) -> AmapPlace:
        if self._key:
            # 官方形态：{status, regeocode:{addressComponent:{...}, roads:[...]}}
            if str(data.get("status")) != "1":
                raise AmapError(f"高德逆地理失败：{data.get('info') or data}")
            comp = (data.get("regeocode") or {}).get("addressComponent") or {}
            roads_raw = (data.get("regeocode") or {}).get("roads") or []
            body = {
                "country": comp.get("country") or "",
                "province": comp.get("province") or "",
                "city": comp.get("city") or comp.get("province") or "",
                "district": comp.get("district") or "",
                "adcode": str(comp.get("adcode") or ""),
                "desc": comp.get("desc") or "",
                "township": comp.get("township") or None,
            }
        else:
            if str(data.get("status")) != "1":
                raise AmapError(f"高德逆地理失败：{data.get('data') or data}")
            body_raw: Mapping[str, Any] = data.get("data") or {}
            body = {
                "country": body_raw.get("country") or "",
                "province": body_raw.get("province") or "",
                "city": body_raw.get("city") or "",
                "district": body_raw.get("district") or "",
                "adcode": str(body_raw.get("adcode") or ""),
                "desc": body_raw.get("desc") or "",
                "township": body_raw.get("township") or None,
            }
            roads_raw = body_raw.get("road_list") or []
            landmark = body_raw.get("hn") or ""
            pos_text = body_raw.get("pos") or ""
        roads: list[Road] = []
        for item in roads_raw:
            try:
                distance = float(item.get("distance") or 0.0)
            except (TypeError, ValueError):
                distance = 0.0
            roads.append(Road(
                name=str(item.get("name") or ""), distance_m=distance,
                direction=str(item.get("direction") or ""),
                level=str(item.get("level") or ""),
                width_m=_to_float(item.get("width")),
                road_id=str(item.get("roadid") or item.get("id") or "")))
        roads.sort(key=lambda r: r.distance_m)
        if self._key:
            landmark = str((data.get("regeocode") or {}).get("formatted_address") or "")
            pos_text = ""
        return AmapPlace(
            longitude=longitude, latitude=latitude,
            roads=tuple(roads), landmark=landmark, pos_text=pos_text,
            provider="amap-official" if self._key else "amap-web",
            official=self.official,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **body,
        )

    # ------------------------------------------------------------------ #
    #  天气
    # ------------------------------------------------------------------ #
    async def weather(self, adcode: str) -> AmapWeather:
        """adcode → 实时天气 + 预报。adcode 由 `regeo` 给出。"""
        code = str(adcode or "").strip()
        if not code:
            raise AmapError("adcode 为空：天气要挂在行政区划上，先做逆地理")
        params: dict[str, Any] = ({"key": self._key, "city": code,
                                   "extensions": "all"} if self._key
                                  else {"adcode": code})
        data = await self._get(self._weather_url, params)
        return self._parse_weather(data, adcode=code)

    def _parse_weather(self, data: Mapping[str, Any], *, adcode: str) -> AmapWeather:
        if self._key:
            if str(data.get("status")) != "1":
                raise AmapError(f"高德天气失败：{data.get('info') or data}")
            lives = data.get("lives") or []
            if not lives:
                raise AmapError(f"高德天气没有实况数据：adcode={adcode}")
            live = lives[0]
            forecasts = data.get("forecasts") or []
            slots: list[AmapForecastSlot] = []
            for day in forecasts:
                for cast in day.get("casts") or []:
                    slots.append(AmapForecastSlot(
                        daynight=0, weather_name=str(cast.get("weather") or ""),
                        min_temp_c=_to_float(cast.get("nighttemp")),
                        max_temp_c=_to_float(cast.get("daytemp")),
                        wind_direction=str(cast.get("daywind") or ""),
                        wind_power=str(cast.get("daypower") or "")))
            report_time = str(live.get("reporttime") or "")
            return AmapWeather(
                adcode=adcode, report_time=report_time,
                weather_name=str(live.get("weather") or ""),
                weather_code="",
                temperature_c=_to_float(live.get("temperature")),
                forecasts=tuple(slots), provider="amap-official",
                official=True,
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if str(data.get("status")) != "1":
            raise AmapError(f"高德天气失败：{data.get('data') or data}")
        rows = ((data.get("data") or {}).get("data")) or []
        if not rows:
            raise AmapError(f"高德天气没有数据：adcode={adcode}")
        day0 = rows[0]
        live = day0.get("live") or {}
        slots: list[AmapForecastSlot] = []
        for cast in day0.get("forecast_data") or []:
            slots.append(AmapForecastSlot(
                daynight=int(cast.get("daynight") or 0),
                weather_name=str(cast.get("weather_name") or
                                 AMAP_WEATHER_CODE_TEXT.get(
                                     str(cast.get("weather_code") or ""), "")),
                min_temp_c=_to_float(cast.get("min_temp")),
                max_temp_c=_to_float(cast.get("max_temp")),
                wind_direction=str(cast.get("wind_direction_desc") or ""),
                wind_power=str(cast.get("wind_power_desc") or "")))
        code = str(live.get("weather_code") or "")
        return AmapWeather(
            adcode=adcode,
            report_time=str(day0.get("report_time") or ""),
            weather_name=str(live.get("weather_name") or
                             AMAP_WEATHER_CODE_TEXT.get(code, "")),
            weather_code=code,
            temperature_c=_to_float(live.get("temperature")),
            forecasts=tuple(slots), provider="amap-web", official=False,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ #
    #  组合：先定位，再在其上取天气
    # ------------------------------------------------------------------ #
    async def where_and_weather(self, *, longitude: float, latitude: float,
                                datum_of_input: str = "wgs84"
                                ) -> tuple[AmapPlace, AmapWeather]:
        """「我在哪 + 那里现在什么天气」的一条链。

        两步串起来而不是各查各的：天气挂在 adcode 上，而 adcode 只能由
        逆地理给出。分开调就会有人拿别处的 adcode 配这里的坐标。
        """
        place = await self.regeo(longitude=longitude, latitude=latitude,
                                 datum_of_input=datum_of_input)
        if not place.adcode:
            raise AmapError("逆地理没给出 adcode：天气无处可挂")
        weather = await self.weather(place.adcode)
        return place, weather

    # ------------------------------------------------------------------ #
    async def _get(self, url: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout,
                                         transport=self._transport,
                                         headers=self._headers(),
                                         follow_redirects=True) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload: Any = response.json()
        except httpx.HTTPError as exc:
            raise AmapError(f"高德请求失败：{exc}") from exc
        except ValueError as exc:                    # JSON 解析失败
            raise AmapError(f"高德返回不是 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise AmapError(f"高德返回结构异常：{type(payload).__name__}")
        return payload


def _to_float(value: Any) -> float | None:
    """把高德那种「可能是空串」的数值转成 float；转不了就 None，不当 0。

    当 0 是危险的：0℃ 与「没测到」是两件事，混在一起天气会显示 0 度。
    """
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result
