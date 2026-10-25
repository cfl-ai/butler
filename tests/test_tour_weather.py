"""实时天气采集的守护测试（U96/U97）。

分两层：
- **MockTransport 层**（CI 常绿）：解析、WMO 码表、TTL 缓存、单飞、
  陈旧判定、歧义标注、隐私字段、采集桥接；
- **真实网络层**（`LIVE_WEATHER=1` 才跑）：对 api.open-meteo.com 与
  geocoding-api.open-meteo.com 发真实请求——「实时」这件事
  mock 永远证明不了。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
import httpx
import pytest

from ai_butler.verticals.tour.agent import TourAgent
from ai_butler.verticals.tour.sources import CollectError
from ai_butler.verticals.tour.weather import (
    WMO_CODE_TEXT,
    GeoPoint,
    OpenMeteoWeather,
    WeatherCache,
    WeatherCollector,
    WeatherError,
    WeatherFact,
)

LIVE = os.environ.get("LIVE_WEATHER") == "1"

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: 全模块共用一个「现在」：写死过去的时间戳会让每条事实一出生就过期
#: （本测试第一版就踩了），而各处各取一次 now 又会让相等断言随机失败。
_NOW_UTC = _utc_now_iso()


def _fact(**kw) -> WeatherFact:
    base = dict(latitude=39.9075, longitude=116.39723, temperature_c=10.8,
                weather_code=61, weather_text="小雨", wind_kmh=12.0,
                fetched_at=_NOW_UTC, provider="open-meteo")
    base.update(kw)
    return WeatherFact(**base)


def _forecast_response(**kw) -> httpx.Response:
    payload = {
        "latitude": 39.9075, "longitude": 116.39723,
        "current": {"time": _NOW_UTC, "temperature_2m": 10.84,
                    "weather_code": 61, "wind_speed_10m": 12.0},
    }
    payload.update(kw)
    return httpx.Response(200, json=payload)


def _geocode_response(count: int = 1) -> httpx.Response:
    results = [{
        "id": 1816670, "name": "北京", "latitude": 39.9075,
        "longitude": 116.39723, "country": "中国", "admin1": "北京市",
        "country_code": "CN",
    }]
    # 歧义样本：重庆还有一个「北京」（真实 API 的返回就是这样）
    if count > 1:
        results.append({"id": 8404324, "name": "北京", "latitude": 30.72608,
                        "longitude": 108.67483, "country": "中国",
                        "admin1": "重庆市", "country_code": "CN"})
    return httpx.Response(200, json={"results": results})


class _Router:
    """按 URL 分派的 MockTransport handler。"""

    def __init__(self) -> None:
        self.forecast_calls = 0
        self.geocode_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            self.geocode_calls += 1
            return _geocode_response(count=2)
        self.forecast_calls += 1
        return _forecast_response()


def _weather(handler) -> OpenMeteoWeather:
    return OpenMeteoWeather(transport=httpx.MockTransport(handler))


class _FakePort:
    """可计数、可定点抛假的天气端口。"""

    def __init__(self, *, fail: bool = False, fact: WeatherFact | None = None,
                 geo: GeoPoint | None | Exception = None) -> None:
        self.calls = 0
        self.geo_calls = 0
        self.fail = fail
        self.fact = fact or _fact()
        self.geo = geo

    async def current(self, *, latitude: float, longitude: float) -> WeatherFact:
        self.calls += 1
        if self.fail:
            raise WeatherError("模拟采集失败")
        return self.fact

    async def geocode(self, name: str) -> GeoPoint | None:
        self.geo_calls += 1
        if isinstance(self.geo, Exception):
            raise self.geo
        return self.geo


# --------------------------------------------------------------------------- #
#  WMO 码表与事实模型
# --------------------------------------------------------------------------- #
def test_wmo_codes_cover_common_weather():
    for code in (0, 1, 2, 3, 45, 61, 63, 65, 71, 80, 95):
        assert code in WMO_CODE_TEXT, f"WMO {code} 没有中文译名"


def test_unknown_wmo_code_is_labelled_not_guessed():
    """未知码译成「未知天气码 N」——不猜一个看起来合理的词。"""
    fact = _fact(weather_code=42, weather_text=WMO_CODE_TEXT.get(42, f"未知天气码 42"))
    assert "未知" in fact.weather_text


def test_age_and_stale_use_fetched_at():
    fact = _fact()
    now = datetime.fromisoformat(_NOW_UTC).replace(tzinfo=timezone.utc).timestamp()
    assert fact.age_seconds(now=now + 60) == pytest.approx(60, abs=1)
    assert fact.is_stale(ttl_seconds=600, now=now + 601) is True
    assert fact.is_stale(ttl_seconds=600, now=now + 599) is False


def test_age_of_unparsable_fetched_at_is_infinite():
    """解析不了时间戳 = 无限陈旧——不是 0 秒（把坏数据当最新比标陈旧更糟）。"""
    fact = _fact(fetched_at="not-a-time")
    assert fact.age_seconds() == float("inf")
    assert fact.is_stale(ttl_seconds=600) is True


def test_payload_keeps_raw_code_and_translation():
    """source 页存原始码 + 译文两份——翻译可以查，原始码不会被译文污染。"""
    payload = _fact().as_payload()
    assert payload["weather_code"] == 61
    assert payload["weather_text"] == "小雨"
    assert payload["fetched_at"] == _NOW_UTC


# --------------------------------------------------------------------------- #
#  OpenMeteoWeather：解析与失败
# --------------------------------------------------------------------------- #
def test_current_parses_forecast_response():
    async def go():
        return await _weather(_Router()).current(latitude=39.9075,
                                                 longitude=116.39723)
    fact = asyncio.run(go())
    assert fact.temperature_c == 10.8          # 10.84 → 一位小数
    assert fact.weather_text == "小雨"
    assert fact.weather_code == 61
    assert fact.wind_kmh == 12.0
    assert fact.fetched_at == _NOW_UTC
    assert fact.provider == "open-meteo"
    assert fact.from_cache is False


def test_current_requests_utc_timezone():
    """fetched_at 必须是无歧义的 UTC——时区偏移会让「距现在多久」整体失真。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _forecast_response()

    asyncio.run(_weather(handler).current(latitude=39.9, longitude=116.4))
    assert "timezone=UTC" in seen[0]


def test_current_http_failure_raises_weather_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": True})

    async def go():
        return await _weather(handler).current(latitude=39.9, longitude=116.4)

    with pytest.raises(WeatherError):
        asyncio.run(go())


def test_current_missing_fields_raises_weather_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"current": {"time": _NOW_UTC}})

    async def go():
        return await _weather(handler).current(latitude=39.9, longitude=116.4)

    with pytest.raises(WeatherError, match="weather_code"):
        asyncio.run(go())


def test_geocode_marks_ambiguity():
    """「北京」在重庆也有一个——歧义必须标出来，不能悄悄取第一条当唯一答案。"""

    async def go():
        return await _weather(_Router()).geocode("北京")

    point = asyncio.run(go())
    assert point is not None
    assert point.name == "北京"
    assert point.latitude == pytest.approx(39.9075)
    assert point.ambiguous is True


def test_geocode_single_result_is_unambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        return _geocode_response(count=1)

    async def go():
        return await _weather(handler).geocode("上海")

    point = asyncio.run(go())
    assert point is not None
    assert point.ambiguous is False


def test_geocode_no_results_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    async def go():
        return await _weather(handler).geocode("不存在的地名xyz")

    assert asyncio.run(go()) is None


def test_geocode_http_failure_raises_weather_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": True})

    async def go():
        return await _weather(handler).geocode("北京")

    with pytest.raises(WeatherError):
        asyncio.run(go())


def test_plain_http_url_is_refused():
    with pytest.raises(WeatherError, match="https"):
        OpenMeteoWeather(forecast_url="http://api.open-meteo.com/v1/forecast")


# --------------------------------------------------------------------------- #
#  WeatherCache：TTL 与单飞（U97）
# --------------------------------------------------------------------------- #
def test_cache_serves_fresh_fact_with_from_cache_marker():
    port = _FakePort()
    cache = WeatherCache(port, ttl_seconds=600)

    async def go():
        first = await cache.current(latitude=39.9, longitude=116.4)
        second = await cache.current(latitude=39.9, longitude=116.4)
        return first, second

    first, second = asyncio.run(go())
    assert port.calls == 2 - 1                  # 第二次走缓存
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.fetched_at == first.fetched_at


def test_cache_refetches_after_ttl():
    port = _FakePort()
    cache = WeatherCache(port, ttl_seconds=600)
    old = _fact(fetched_at="2020-01-01T00:00:00")
    cache._facts[(round(39.9, 3), round(116.4, 3))] = old

    async def go():
        return await cache.current(latitude=39.9, longitude=116.4)

    fact = asyncio.run(go())
    assert port.calls == 1                      # 过期 → 重新采集
    assert fact.fetched_at == _NOW_UTC


def test_cache_coarsens_key_like_u71():
    """坐标差 1e-7 的两次查询命中同一条缓存——不为免费 API 添无谓的请求。

    基准坐标刻意避开 0.0005 这种四舍五入边界（39.9075 与 39.90751
    会落进不同的三位小数格子，那时「没命中」是正确的，不是缺陷）。
    """
    port = _FakePort()
    cache = WeatherCache(port, ttl_seconds=600)

    async def go():
        await cache.current(latitude=39.9072, longitude=116.3972)
        await cache.current(latitude=39.9072001, longitude=116.3972002)
        return port.calls

    assert asyncio.run(go()) == 1


def test_cache_does_not_coarsen_across_cell_boundary():
    """跨格就是跨格：粗化到 110m，不是把相距几百米的点当同一个。

    这条是上一条的反面——只测「会命中」会漏掉「该分不分」的缺陷。
    """
    port = _FakePort()
    cache = WeatherCache(port, ttl_seconds=600)

    async def go():
        await cache.current(latitude=39.9072, longitude=116.3972)
        await cache.current(latitude=39.9089, longitude=116.3991)
        return port.calls

    assert asyncio.run(go()) == 2


def test_cache_single_flight_under_concurrency():
    """并发 8 次同一坐标，只打一次源（单飞）——免费 API 有限流。"""
    port = _FakePort()
    cache = WeatherCache(port, ttl_seconds=600)

    async def go():
        results = await asyncio.gather(*[
            cache.current(latitude=39.9, longitude=116.4) for _ in range(8)])
        return results

    results = asyncio.run(go())
    assert port.calls == 1
    assert {r.fetched_at for r in results} == {_NOW_UTC}


def test_cache_failure_propagates_without_serving_stale():
    """采集失败照实报错——不吐陈旧数据冒充新鲜（表 12 禁回退）。"""
    port = _FakePort(fail=True)
    cache = WeatherCache(port, ttl_seconds=600)

    async def go():
        return await cache.current(latitude=39.9, longitude=116.4)

    with pytest.raises(WeatherError):
        asyncio.run(go())


def test_cache_rejects_non_positive_ttl():
    with pytest.raises(WeatherError, match="TTL"):
        WeatherCache(_FakePort(), ttl_seconds=0)


# --------------------------------------------------------------------------- #
#  WeatherCollector：统一采集桥接（U37）
# --------------------------------------------------------------------------- #
def test_collector_accepts_lat_lon_subject_without_geocoding():
    port = _FakePort()
    collector = WeatherCollector(port)

    async def go():
        return await collector.fetch("weather", subject="39.9075,116.39723")

    fact = asyncio.run(go())
    assert port.geo_calls == 0
    assert fact.kind == "weather"
    assert fact.provider == "open-meteo"
    assert fact.payload["temperature_c"] == 10.8
    assert fact.payload["weather_text"] == "小雨"
    assert fact.evidence_id.startswith("open-meteo:")


def test_collector_geocodes_place_name():
    point = GeoPoint(name="北京", latitude=39.9075, longitude=116.39723,
                     country="中国", admin1="北京市", ambiguous=True)
    port = _FakePort(geo=point)
    collector = WeatherCollector(port)

    async def go():
        return await collector.fetch("weather", subject="北京")

    fact = asyncio.run(go())
    assert port.geo_calls == 1
    assert fact.payload["place_name"] == "北京"
    # 歧义地名 → 低置信标注（表 11 步骤3）
    assert fact.low_confidence is True


def test_collector_unresolvable_name_is_a_collect_failure():
    port = _FakePort(geo=None)
    collector = WeatherCollector(port)

    async def go():
        return await collector.fetch("weather", subject="不存在的地名xyz")

    with pytest.raises(CollectError, match="解析不到坐标"):
        asyncio.run(go())


def test_collector_refuses_non_weather_kinds_honestly():
    """没有免费可达的真实源就不服务——不假装有（U96）。"""
    collector = WeatherCollector(_FakePort())

    async def go():
        return await collector.fetch("traffic", subject="北京")

    with pytest.raises(CollectError, match="traffic"):
        asyncio.run(go())


def test_collector_empty_subject_is_refused():
    collector = WeatherCollector(_FakePort())

    async def go():
        return await collector.fetch("weather", subject="  ")

    with pytest.raises(CollectError):
        asyncio.run(go())


# --------------------------------------------------------------------------- #
#  Agent 集成：天气事实落 source 页 + claim 绑证据（表 11 步骤2）
# --------------------------------------------------------------------------- #
def _trip_state(**extra) -> dict:
    state = {
        "user_id": "u1",
        "task_kind": "trip_plan",
        "task_params": {"destination": "北京", "start_date": "2026-11-01",
                        "days": 1, "party_size": 2},
        "collect_kinds": ["weather"],
        "candidate_stops": [
            {"stop_id": "s1", "name": "第一站", "dwell_minutes": 60,
             "latitude": 39.909, "longitude": 116.404},
        ],
    }
    state.update(extra)
    return state


def test_agent_lands_weather_fact_as_source_page():
    point = GeoPoint(name="北京", latitude=39.9075, longitude=116.39723)
    collector = WeatherCollector(_FakePort(geo=point))
    agent = TourAgent(collector=collector)
    result = asyncio.run(agent.run(_trip_state()))
    pages = result["tour_result"]["source_pages"]
    assert len(pages) == 1
    page = pages[0]
    assert page["kind"] == "weather"
    assert page["provider"] == "open-meteo"
    # claim 必须绑 evidence（表 11 步骤2）
    assert page["claim"]["evidence_ids"]
    assert page["claim"]["subject_id"] == "北京"


def test_agent_collect_failure_blocks_plan_without_rollback():
    """天气采集失败 → 计划不出、原因可见、且**不回退**（表 12）。

    地名解析必须给一个真结果：否则先撞上「解析不到坐标」，
    测的是另一条失败路径，根本没走到天气采集。
    """
    point = GeoPoint(name="北京", latitude=39.9075, longitude=116.39723)
    collector = WeatherCollector(_FakePort(fail=True, geo=point))
    agent = TourAgent(collector=collector)
    result = asyncio.run(agent.run(_trip_state()))
    body = result["tour_result"]
    assert body["handled"] is False
    assert body["fallback"]["allowed"] is False
    assert any("模拟采集失败" in f for f in body["failures"])


# --------------------------------------------------------------------------- #
#  真实网络层（LIVE_WEATHER=1 才跑）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_WEATHER=1（真实网络冒烟）")
def test_live_beijing_weather_is_real():
    """真实取一次北京当前天气：温度在合理区间、译文非空、fetched_at 可解析。"""

    async def go():
        return await OpenMeteoWeather().current(latitude=39.9075,
                                                longitude=116.39723)

    fact = asyncio.run(go())
    assert -60.0 < fact.temperature_c < 60.0
    assert fact.weather_text
    assert fact.provider == "open-meteo"
    # fetched_at 必须能被 age_seconds 解析（UTC 无歧义）
    assert fact.age_seconds() < 3600
    assert fact.is_stale(ttl_seconds=600) is False


@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_WEATHER=1（真实网络冒烟）")
def test_live_geocode_beijing_is_ambiguous():
    """真实 API 对「北京」返回多个结果（重庆也有一个）——歧义标注不是理论。"""

    async def go():
        return await OpenMeteoWeather().geocode("北京")

    point = asyncio.run(go())
    assert point is not None
    assert point.ambiguous is True
    assert 39.0 < point.latitude < 40.0
