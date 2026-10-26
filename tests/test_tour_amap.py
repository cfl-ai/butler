"""高德真实源的守护测试（U101）。

分两层：
- **MockTransport 层**（CI 常绿）：解析、行政层级、道路排序、坐标基准转换、
  两步串联、失败纪律、拟人化/官方两种形态；
- **真实网络层**（`LIVE_AMAP=1` 才跑）：真打 www.amap.com。
  拟人化接口随时可能失效，mock 证明不了「现在还活着」。
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from ai_butler.verticals.tour.amap import (
    AMAP_OFFICIAL_REGEO_URL,
    AMAP_REGEO_URL,
    AMAP_WEATHER_URL,
    AmapError,
    AmapSource,
    make_amap_source,
)
from ai_butler.verticals.tour.datum import wgs84_to_gcj02

LIVE = os.environ.get("LIVE_AMAP") == "1"

#: 天安门（WGS-84）
BEIJING = (116.3975, 39.9087)

_REGEO_PAYLOAD = {
    "status": "1",
    "data": {
        "code": "1", "message": "Successful.", "result": "true",
        "timestamp": "1792947682181", "version": "1.0",
        "country": "中国", "countrycode": "CN",
        "province": "北京市", "provinceadcode": "110000",
        "city": "北京市", "cityadcode": "110000", "tel": "010", "areacode": "010",
        "district": "东城区", "districtadcode": "110101", "adcode": "110101",
        "desc": "北京市,北京市,东城区",
        "road_list": [
            {"direction": "North", "distance": "111", "latitude": "39.9077",
             "level": "4", "longitude": "116.398", "name": "西长安街",
             "roadid": "010J50F0020192946", "width": "28"},
            {"direction": "East", "distance": "159", "latitude": "39.9089",
             "level": "7", "longitude": "116.396", "name": "广场西路",
             "roadid": "010J50F00202065684", "width": "4"},
        ],
        "cross_list": [], "hn": "天安门", "sea_area": "",
        "pos": "在西长安街旁边",
    },
}

_WEATHER_PAYLOAD = {
    "status": "1",
    "data": {
        "code": "1", "timestamp": "1792947699.38", "version": "2.0",
        "result": "true", "message": "Successful.",
        "data": [{
            "report_time": "2026-10-26 00:55:00",
            "live": {"weather_name": "阴", "weather_code": "02",
                     "temperature": "9"},
            "forecast_date": "2026-10-26", "weekday": 0,
            "forecast_data": [
                {"wind_direction_code": "7", "weather_code": "01",
                 "wind_power_code": "2", "min_temp": "9", "weather_name": "多云",
                 "wind_power_desc": "4-5", "daynight": 0,
                 "wind_direction_desc": "西北风", "max_temp": "18"},
                {"wind_direction_code": "7", "weather_code": "01",
                 "wind_power_code": "1", "min_temp": "6", "weather_name": "晴",
                 "wind_power_desc": "3", "daynight": 1,
                 "wind_direction_desc": "西北风", "max_temp": "12"},
            ],
        }],
    },
}


class _Router:
    """按 URL 分派，并记录每次请求（用于验证串联与坐标转换）。"""

    def __init__(self, *, regeo=None, weather=None) -> None:
        self.regeo_payload = regeo or _REGEO_PAYLOAD
        self.weather_payload = weather or _WEATHER_PAYLOAD
        self.urls: list[str] = []
        self.weather_params: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.urls.append(url)
        if "regeo" in url:
            return httpx.Response(200, json=self.regeo_payload)
        self.weather_params.append(dict(request.url.params))
        return httpx.Response(200, json=self.weather_payload)


def _source(router, **kw) -> AmapSource:
    return AmapSource(transport=httpx.MockTransport(router), **kw)


# --------------------------------------------------------------------------- #
#  逆地理：行政层级与所在道路
# --------------------------------------------------------------------------- #
def test_regeo_parses_full_admin_hierarchy():
    place = asyncio.run(_source(_Router()).regeo(longitude=BEIJING[0],
                                                 latitude=BEIJING[1]))
    assert place.country == "中国"
    assert place.province == "北京市"
    assert place.district == "东城区"
    assert place.adcode == "110101"
    assert place.hierarchy()[:3] == ("中国", "北京市", "北京市")


def test_regeo_exposes_the_road_you_are_on():
    """「现在在什么路」——最近的那条路必须是距离最小的那条。"""
    place = asyncio.run(_source(_Router()).regeo(longitude=BEIJING[0],
                                                 latitude=BEIJING[1]))
    road = place.nearest_road
    assert road is not None
    assert road.name == "西长安街"
    assert road.distance_m == pytest.approx(111.0)
    # 排序保证：距离升序，不是 API 给的原始顺序
    dists = [r.distance_m for r in place.roads]
    assert dists == sorted(dists)


def test_regeo_one_line_answers_where_and_which_road():
    place = asyncio.run(_source(_Router()).regeo(longitude=BEIJING[0],
                                                 latitude=BEIJING[1]))
    text = place.one_line()
    assert "东城区" in text
    assert "西长安街" in text


def test_regeo_converts_wgs84_to_gcj02_before_asking():
    """设备 GPS 是 WGS-84，高德要 GCJ-02——查询前必须转换。

    不转换的后果是逆地理结果整体偏移数百米，而它「看起来」仍然是一
    个合理的行政区，正是最难发现的那种错。
    """
    router = _Router()
    asyncio.run(_source(router).regeo(longitude=BEIJING[0], latitude=BEIJING[1]))
    asked_lon = float(router.urls[0].split("longitude=")[1].split("&")[0])
    asked_lat = float(router.urls[0].split("latitude=")[1].split("&")[0])
    expect_lon, expect_lat = wgs84_to_gcj02(*BEIJING)
    assert asked_lon == pytest.approx(expect_lon, abs=1e-5)
    assert asked_lat == pytest.approx(expect_lat, abs=1e-5)
    # 且确实动了（不是恒等转换）
    assert asked_lon != pytest.approx(BEIJING[0], abs=1e-5)


def test_regeo_honours_already_gcj02_input():
    """调用方明确说坐标已是 GCJ-02 时，不再二次转换（否则会偏两次）。"""
    glon, glat = wgs84_to_gcj02(*BEIJING)
    router = _Router()
    asyncio.run(_source(router).regeo(longitude=glon, latitude=glat,
                                      datum_of_input="gcj02"))
    asked_lon = float(router.urls[0].split("longitude=")[1].split("&")[0])
    assert asked_lon == pytest.approx(glon, abs=1e-6)


def test_regeo_rejects_unknown_datum():
    async def go():
        return await _source(_Router()).regeo(longitude=BEIJING[0],
                                              latitude=BEIJING[1],
                                              datum_of_input="bd09")
    with pytest.raises(AmapError, match="坐标基准"):
        asyncio.run(go())


def test_township_is_absent_and_marked_as_such():
    """镇/乡拟人化接口不返回——如实置 None 并注明，不用区名补位。"""
    place = asyncio.run(_source(_Router()).regeo(longitude=BEIJING[0],
                                                 latitude=BEIJING[1]))
    assert place.township is None
    assert "镇/乡" in place.as_payload()["township_note"]
    # 层级里不能出现空的乡镇项
    assert all(level for level in place.hierarchy())


def test_regeo_marks_itself_unofficial():
    place = asyncio.run(_source(_Router()).regeo(longitude=BEIJING[0],
                                                 latitude=BEIJING[1]))
    assert place.official is False
    assert place.provider == "amap-web"


# --------------------------------------------------------------------------- #
#  天气
# --------------------------------------------------------------------------- #
def test_weather_parses_live_and_forecast():
    weather = asyncio.run(_source(_Router()).weather("110101"))
    assert weather.temperature_c == pytest.approx(9.0)
    assert weather.weather_name == "阴"
    assert weather.weather_code == "02"
    assert weather.report_time == "2026-10-26 00:55:00"
    assert len(weather.forecasts) == 2
    assert weather.forecasts[0].weather_name == "多云"
    assert weather.forecasts[0].max_temp_c == pytest.approx(18.0)
    assert weather.forecasts[1].daynight == 1


def test_weather_temperature_missing_is_none_not_zero():
    """空温度是「没测到」，不是 0℃——混起来会显示零下都不如的假数据。"""
    payload = {"status": "1", "data": {"data": [{
        "report_time": "2026-10-26 00:55:00",
        "live": {"weather_name": "阴", "weather_code": "02", "temperature": ""},
        "forecast_data": []}]}}
    router = _Router(weather=payload)
    weather = asyncio.run(_source(router).weather("110101"))
    assert weather.temperature_c is None
    assert "℃" not in weather.one_line()


def test_weather_needs_an_adcode():
    """天气挂在行政区划上——没有 adcode 就查不了，不拿城市名去猜。"""
    async def go():
        return await _source(_Router()).weather("  ")
    with pytest.raises(AmapError, match="adcode"):
        asyncio.run(go())


def test_weather_status_failure_is_an_error():
    async def go():
        return await _source(_Router(weather={"status": "0", "data": "参数错误"})
                             ).weather("110101")
    with pytest.raises(AmapError, match="失败"):
        asyncio.run(go())


def test_weather_empty_rows_is_an_error():
    async def go():
        return await _source(_Router(weather={"status": "1", "data": {"data": []}})
                             ).weather("110101")
    with pytest.raises(AmapError, match="没有数据"):
        asyncio.run(go())


def test_weather_freshness_uses_fetched_at():
    weather = asyncio.run(_source(_Router()).weather("110101"))
    assert weather.age_seconds() < 60
    assert weather.is_stale(ttl_seconds=600) is False


# --------------------------------------------------------------------------- #
#  两步串联：先定位，再在其上取天气
# --------------------------------------------------------------------------- #
def test_where_and_weather_uses_the_adcode_from_regeo():
    """天气必须挂在这次逆地理给出的 adcode 上，不能各查各的。"""
    router = _Router()
    place, weather = asyncio.run(
        _source(router).where_and_weather(longitude=BEIJING[0],
                                           latitude=BEIJING[1]))
    assert place.adcode == "110101"
    assert weather.adcode == place.adcode
    assert router.weather_params[0]["adcode"] == "110101"


def test_where_and_weather_fails_when_place_has_no_adcode():
    payload = {"status": "1", "data": {**_REGEO_PAYLOAD["data"], "adcode": ""}}
    router = _Router(regeo=payload)

    async def go():
        return await _source(router).where_and_weather(longitude=BEIJING[0],
                                                       latitude=BEIJING[1])
    with pytest.raises(AmapError, match="adcode"):
        asyncio.run(go())


# --------------------------------------------------------------------------- #
#  失败纪律与形态
# --------------------------------------------------------------------------- #
def test_http_failure_raises_amap_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": True})

    async def go():
        return await _source(handler).regeo(longitude=BEIJING[0],
                                             latitude=BEIJING[1])
    with pytest.raises(AmapError, match="请求失败"):
        asyncio.run(go())


def test_non_json_response_raises_amap_error():
    """风控页（HTML）会被当成 JSON 解析失败——报出来，不要静默回退。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>风控</html>")

    async def go():
        return await _source(handler).regeo(longitude=BEIJING[0],
                                             latitude=BEIJING[1])
    with pytest.raises(AmapError, match="不是 JSON"):
        asyncio.run(go())


def test_plain_http_endpoint_is_refused():
    with pytest.raises(AmapError, match="https"):
        AmapSource(regeo_url="http://www.amap.com/service/regeo")


def test_official_key_switches_endpoint_and_flag():
    """官方 key 一到位就改走官方端点，并如实标注 official=True。"""
    source = AmapSource(key="test-key")
    assert source.official is True
    assert source._regeo_url == AMAP_OFFICIAL_REGEO_URL


def test_simulated_endpoint_is_the_default():
    source = AmapSource()
    assert source.official is False
    assert source._regeo_url == AMAP_REGEO_URL
    assert source._weather_url == AMAP_WEATHER_URL


def test_browser_headers_are_only_sent_on_the_simulated_path():
    """官方 API 不带拟浏览器头（那是伪装）；拟人化旁路必须带。"""
    assert AmapSource()._headers().get("Referer")
    assert AmapSource(key="k")._headers() == {}


def test_official_regeo_parses_address_component():
    """官方形态的响应结构不同（regeocode.addressComponent）——也要能解析，
    否则 key 一到手反而解析不出东西。"""
    payload = {"status": "1", "regeocode": {
        "formatted_address": "北京市东城区西长安街",
        "addressComponent": {"country": "中国", "province": "北京市",
                             "city": "北京市", "district": "东城区",
                             "adcode": "110101", "township": "东华门街道"},
        "roads": [{"name": "西长安街", "distance": "50", "direction": "北"}]}}
    source = AmapSource(key="k", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    place = asyncio.run(source.regeo(longitude=BEIJING[0], latitude=BEIJING[1]))
    assert place.district == "东城区"
    assert place.township == "东华门街道"          # 官方才有乡镇
    assert place.official is True
    assert place.nearest_road.name == "西长安街"


def test_official_weather_parses_lives_and_forecasts():
    payload = {"status": "1",
               "lives": [{"weather": "晴", "temperature": "12",
                          "reporttime": "2026-10-26 09:00:00"}],
               "forecasts": [{"casts": [
                   {"weather": "多云", "daytemp": "20", "nighttemp": "8",
                    "daywind": "西北", "daypower": "4"}]}]}
    source = AmapSource(key="k", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    weather = asyncio.run(source.weather("110101"))
    assert weather.temperature_c == pytest.approx(12.0)
    assert weather.weather_name == "晴"
    assert weather.official is True
    assert weather.forecasts[0].max_temp_c == pytest.approx(20.0)


def test_official_regeo_sends_location_and_coordsys():
    """官方端点用 location=经度,纬度（经度在前）+ coordsys=gps，不用 longitude/。

    这是真 key 实测前必须锁定的参数形态——mock 测试无视参数，曾让官方路径
    长期「声明可用、实则点错参数名」（U108 修）。
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "1", "regeocode": {
            "addressComponent": {"country": "中国", "province": "北京市",
                                 "city": "北京市", "district": "东城区",
                                 "adcode": "110101", "township": "东华门街道"},
            "roads": []}})

    async def go():
        return await AmapSource(key="k", transport=httpx.MockTransport(handler)
                               ).regeo(longitude=116.3975, latitude=39.9087)
    place = asyncio.run(go())
    url = captured["url"]
    assert "location=116.3975" in url and "39.9087" in url
    assert "coordsys=gps" in url
    assert "longitude=" not in url
    assert place.township == "东华门街道"          # 官方才有乡镇


def test_official_regeo_gcj02_input_skips_coordsys():
    """调用方已确认坐标是 GCJ-02 时，不传 coordsys（避免高德二次偏移）。"""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "1", "regeocode": {
            "addressComponent": {"country": "中国", "province": "北京市",
                                 "city": "北京市", "district": "东城区",
                                 "adcode": "110101"}, "roads": []}})

    async def go():
        return await AmapSource(key="k", transport=httpx.MockTransport(handler)
                               ).regeo(longitude=116.4036, latitude=39.9100,
                                       datum_of_input="gcj02")
    asyncio.run(go())
    assert "location=" in captured["url"]
    assert "coordsys" not in captured["url"]


def test_official_weather_base_and_all_split():
    """官方天气：base 回实况、all 只回预报（无 lives）。两路合并，不得因 all 缺 lives 报错。

    真 key 实测前必须锁定：extensions=all 不回 lives 是真实行为，曾让官方天气解析器
    直接 raise「没有实况数据」（U108 修为分两次取）。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("extensions") == "base":
            return httpx.Response(200, json={"status": "1", "lives": [
                {"weather": "多云", "temperature": "18",
                 "reporttime": "2026-10-26 15:34:06"}]})
        return httpx.Response(200, json={"status": "1", "forecasts": [{
            "city": "东城区", "adcode": "110101",
            "casts": [{"date": "2026-10-26", "dayweather": "晴", "nightweather": "晴",
                       "daytemp": "18", "nighttemp": "6", "daywind": "西",
                       "daypower": "1-3"}]}]})

    async def go():
        return await AmapSource(key="k", transport=httpx.MockTransport(handler)
                               ).weather("110101")
    weather = asyncio.run(go())
    assert weather.official is True
    assert weather.weather_name == "多云"
    assert weather.temperature_c == pytest.approx(18.0)
    assert len(weather.forecasts) == 1
    assert weather.forecasts[0].max_temp_c == pytest.approx(18.0)
    assert weather.forecasts[0].min_temp_c == pytest.approx(6.0)


def test_make_amap_source_reads_config_key():
    """工厂从 Settings.amap_key 读 key；有 key 即 official=True（U108 装配点）。"""
    src = make_amap_source(key="k")
    assert src.official is True


# --------------------------------------------------------------------------- #
#  真实网络层（LIVE_AMAP=1 才跑）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_AMAP=1（真实网络冒烟）")
def test_live_regeo_beijing():
    async def go():
        return await AmapSource().regeo(longitude=BEIJING[0], latitude=BEIJING[1])
    place = asyncio.run(go())
    assert place.province == "北京市"
    assert place.adcode.startswith("1101")
    assert place.nearest_road is not None


@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_AMAP=1（真实网络冒烟）")
def test_live_where_and_weather_chain():
    """真实跑一遍「我在哪 + 那里现在什么天气」——这是本轮的核心链路。"""
    place, weather = asyncio.run(
        AmapSource().where_and_weather(longitude=BEIJING[0], latitude=BEIJING[1]))
    assert place.adcode
    assert weather.adcode == place.adcode
    assert -60.0 < (weather.temperature_c or 0.0) < 60.0
    assert weather.weather_name


# 官方真实链路：必须同时具备 LIVE_AMAP=1 与真实 AMAP_KEY（避免误打官方 API）
LIVE_OFFICIAL = LIVE and bool(os.environ.get("AMAP_KEY"))


@pytest.mark.skipif(not LIVE_OFFICIAL,
                    reason="未设置 LIVE_AMAP=1 与 AMAP_KEY（官方真实链路冒烟）")
def test_live_official_where_and_weather():
    """真 key 官方链路：`make_amap_source()` 读配置拿到 key，逆地理应有镇/乡，
    天气应有实况 + 预报。这是 U108「key 真正接通」的可重复冒烟。"""
    place, weather = asyncio.run(
        make_amap_source().where_and_weather(longitude=BEIJING[0], latitude=BEIJING[1]))
    assert place.official is True
    assert place.adcode.startswith("1101")
    assert place.township                       # 官方才有镇/乡
    assert weather.official is True
    assert weather.temperature_c is not None
    assert weather.weather_name
    assert len(weather.forecasts) >= 1


# --------------------------------------------------------------------------- #
#  POI 检索（U109）：官方 Web 服务专属，与 regeo 顺带周边 POI 不重叠
# --------------------------------------------------------------------------- #
_POI_TEXT_PAYLOAD = {
    "status": "1", "count": "2",
    "pois": [
        {"name": "故宫博物院", "address": "景山前街4号", "type": "风景名胜;公园广场;",
         "location": "116.397128,39.918058", "adcode": "110101",
         "tel": "010-85007421", "distance": ""},
        {"name": "天安门广场", "address": "长安街", "type": "风景名胜;广场;",
         "location": "116.397499,39.903179", "adcode": "110101",
         "tel": "", "distance": ""},
    ],
}

_POI_AROUND_PAYLOAD = {
    "status": "1", "count": "1",
    "pois": [
        {"name": "王府井百货", "address": "王府井大街255号", "type": "购物;百货商场;",
         "location": "116.410886,39.914889", "adcode": "110101",
         "tel": "010-65228888", "distance": "320"},
    ],
}


class _PoiRouter:
    """记录每次 POI 请求的参数与 URL（用于验证端点与参数形态）。"""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.params: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.params.append(dict(request.url.params))
        return httpx.Response(200, json=self.payload)


def test_search_places_sends_keywords_city_types_and_hits_text_endpoint():
    """v3/place/text：keywords/city/types/key 都要上参数，且打的是 text 端点。"""
    router = _PoiRouter(_POI_TEXT_PAYLOAD)
    src = AmapSource(key="k", transport=httpx.MockTransport(router))
    pois = asyncio.run(src.search_places(keywords="故宫", city="北京",
                                        types="风景名胜", offset=10, page=2))
    assert len(pois) == 2
    p = router.params[0]
    assert p["keywords"] == "故宫"
    assert p["city"] == "北京"
    assert p["types"] == "风景名胜"
    assert p["key"] == "k"
    assert p["offset"] == "10"
    assert p["page"] == "2"
    assert "place/text" in router.urls[0]


def test_search_places_parses_all_fields_and_marks_official():
    """解析出 name/address/poi_type/location/adcode/tel，并如实 official=True。"""
    src = AmapSource(key="k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=_POI_TEXT_PAYLOAD)))
    pois = asyncio.run(src.search_places(keywords="故宫"))
    first = pois[0]
    assert first.name == "故宫博物院"
    assert first.address == "景山前街4号"
    assert first.poi_type == "风景名胜;公园广场;"
    assert first.location == pytest.approx((116.397128, 39.918058))
    assert first.adcode == "110101"
    assert first.tel == "010-85007421"
    assert first.distance_m is None          # text 检索不给距离
    assert first.official is True
    assert first.provider == "amap-official"
    assert "故宫博物院" in first.one_line()


def test_search_places_requires_key():
    """无 key 如实抛错——不降级、不伪装（POI 端点必须 key）。"""
    async def go():
        return await AmapSource().search_places(keywords="故宫")
    with pytest.raises(AmapError, match="key"):
        asyncio.run(go())


def test_search_places_rejects_empty_keywords():
    async def go():
        return await AmapSource(key="k").search_places(keywords="   ")
    with pytest.raises(AmapError, match="keywords"):
        asyncio.run(go())


def test_search_places_status_failure_surfaces_info():
    async def go():
        return await AmapSource(key="k", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"status": "0",
                                                "info": "INVALID_USER_KEY"})
        )).search_places(keywords="故宫")
    with pytest.raises(AmapError, match="INVALID_USER_KEY"):
        asyncio.run(go())


def test_search_places_empty_result_is_empty_tuple():
    """搜不到返回空元组，不是 None 也不是异常（如实反映「没有」）。"""
    pois = asyncio.run(AmapSource(key="k", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"status": "1", "pois": []})
    )).search_places(keywords="不存在的地名xyz"))
    assert pois == ()


def test_search_places_around_sends_location_radius_and_parses_distance():
    """v3/place/around：location=经度,纬度 + radius，并能解析周边距离。"""
    router = _PoiRouter(_POI_AROUND_PAYLOAD)
    src = AmapSource(key="k", transport=httpx.MockTransport(router))
    pois = asyncio.run(src.search_places_around(longitude=116.4, latitude=39.91,
                                               radius=500, keywords="商场"))
    assert len(pois) == 1
    poi = pois[0]
    assert poi.name == "王府井百货"
    assert poi.distance_m == pytest.approx(320.0)
    assert poi.location == pytest.approx((116.410886, 39.914889))
    p = router.params[0]
    assert p["location"] == "116.400000,39.910000"
    assert p["radius"] == "500"
    assert p["keywords"] == "商场"
    assert "place/around" in router.urls[0]


def test_search_places_around_requires_key():
    async def go():
        return await AmapSource().search_places_around(longitude=116.4,
                                                      latitude=39.91)
    with pytest.raises(AmapError, match="key"):
        asyncio.run(go())


@pytest.mark.skipif(not LIVE_OFFICIAL,
                    reason="未设置 LIVE_AMAP=1 与 AMAP_KEY（官方 POI 冒烟）")
def test_live_search_places_text():
    """真 key 官方 POI 冒烟：关键字搜「故宫」应至少命中一条。"""
    pois = asyncio.run(make_amap_source().search_places(keywords="故宫",
                                                        city="北京"))
    assert len(pois) >= 1
    assert any("故宫" in p.name for p in pois)
    assert all(p.official is True for p in pois)
