"""wttr.in 主源 + FailoverWeather 故障转移的守护测试（2026-11-18 补充裁定）。

MockTransport 层：j1 解析、码表隔离（不许复用 WMO 表）、3 天上限的
如实报错、failover 换源、全部失败列原因、能力探测跳过、装配点主备顺序。
"""

from __future__ import annotations

import asyncio
import json
import httpx
import pytest

from ai_butler.scheduler.external_tools import default_weather_port
from ai_butler.verticals.tour.weather import (
    FailoverWeather,
    OpenMeteoWeather,
    WTTR_CODE_TEXT,
    WttrWeather,
    WeatherError,
)

# --------------------------------------------------------------------------- #
#  j1 样本（wttr.in 真实结构的最小还原）
# --------------------------------------------------------------------------- #
def _j1_payload() -> dict:
    return {
        "current_condition": [{
            "temp_C": "13.6", "weatherCode": "113",
            "windspeedKmph": "14.3", "localObsDateTime": "2026-11-18 05:23 PM",
        }],
        "weather": [
            {"date": "2026-11-18", "maxtempC": "17", "mintempC": "9",
             "totalprecipMM": "2.1",
             "hourly": [
                 {"time": "900", "weatherCode": "176",
                  "chanceofrain": "70", "chanceofsnow": "0"},
                 {"time": "1200", "weatherCode": "296",
                  "chanceofrain": "97", "chanceofsnow": "0"},
             ]},
            {"date": "2026-11-19", "maxtempC": "15", "mintempC": "8",
             "totalprecipMM": "0.0",
             "hourly": [
                 {"time": "1200", "weatherCode": "119",
                  "chanceofrain": "6", "chanceofsnow": "0"},
             ]},
        ],
        "nearest_area": [{
            "areaName": [{"value": "Hangzhou"}],
            "country": [{"value": "China"}],
            "region": [{"value": "Zhejiang"}],
            "latitude": "30.25", "longitude": "120.17",
        }],
    }


def _j1_handler(payload: dict | None = None, *, status: int = 200):
    body = payload if payload is not None else _j1_payload()
    if isinstance(body, Exception):
        def raise_handler(request: httpx.Request) -> httpx.Response:
            raise body
        return raise_handler
    return lambda request: httpx.Response(status, json=body)


def _wttr(payload: dict | None = None) -> WttrWeather:
    return WttrWeather(transport=httpx.MockTransport(_j1_handler(payload)))


# --------------------------------------------------------------------------- #
#  WttrWeather：解析
# --------------------------------------------------------------------------- #
def test_current_parses_j1_and_uses_wttr_code_table():
    fact = asyncio.run(_wttr().current(latitude=30.25, longitude=120.17))
    assert fact.provider == "wttr.in"
    assert fact.temperature_c == 13.6
    assert fact.weather_code == 113
    # 113 在 WMO 表里根本不存在——读出「晴」只能来自 wttr 专用码表
    assert fact.weather_text == "晴"
    assert fact.wind_kmh == 14.3


def test_wttr_code_table_is_independent_from_wmo():
    """两套码表不许混用：wttr 的 116（多云）在 WMO 表里是别的意思。"""
    assert 116 in WTTR_CODE_TEXT
    assert WTTR_CODE_TEXT[116] == "多云"


def test_unknown_wttr_code_is_labelled_not_guessed():
    payload = _j1_payload()
    payload["current_condition"][0]["weatherCode"] = "999"
    fact = asyncio.run(_wttr(payload).current(latitude=30.25, longitude=120.17))
    assert "未知" in fact.weather_text


def test_current_missing_fields_reports_which():
    payload = _j1_payload()
    del payload["current_condition"][0]["temp_C"]
    with pytest.raises(WeatherError, match="temp_C"):
        asyncio.run(_wttr(payload).current(latitude=30.25, longitude=120.17))


def test_forecast_parses_midday_code_and_max_prob():
    rows = asyncio.run(_wttr().forecast(latitude=30.25, longitude=120.17, days=2))
    assert len(rows) == 2
    assert rows[0].date == "2026-11-18"
    assert rows[0].weather_code == 296          # 正午时段的码，不是 0900 的阵雨
    assert rows[0].weather_text == "小雨"
    assert rows[0].temp_max_c == 17.0 and rows[0].temp_min_c == 9.0
    assert rows[0].precip_prob_max == 97        # 全天最大降水概率
    assert rows[0].precip_mm == 2.1


def test_forecast_more_than_three_days_fails_honestly():
    """>3 天在本源如实报错（触发 failover），不静默只回 3 天。"""
    with pytest.raises(WeatherError, match="最多 3 天"):
        asyncio.run(_wttr().forecast(latitude=30.25, longitude=120.17, days=5))


def test_forecast_without_hourly_fails_for_that_day():
    payload = _j1_payload()
    payload["weather"][0]["hourly"] = []
    with pytest.raises(WeatherError, match="hourly"):
        asyncio.run(_wttr(payload).forecast(latitude=30.25,
                                            longitude=120.17, days=1))


def test_geocode_parses_nearest_area():
    point = asyncio.run(_wttr().geocode("杭州"))
    assert point is not None
    assert point.latitude == 30.25 and point.longitude == 120.17
    assert point.name == "Hangzhou"
    assert point.ambiguous is False


def test_geocode_empty_name_returns_none():
    assert asyncio.run(_wttr().geocode("  ")) is None


def test_http_error_carries_location():
    handler = _j1_handler(httpx.ConnectError("boom"))
    port = WttrWeather(transport=httpx.MockTransport(handler))
    with pytest.raises(WeatherError, match="杭州"):
        asyncio.run(port.geocode("杭州"))


# --------------------------------------------------------------------------- #
#  FailoverWeather：换源语义
# --------------------------------------------------------------------------- #
def test_failover_switches_to_next_source_on_primary_failure():
    class _Broken:
        name = "broken"

        async def current(self, *, latitude, longitude):
            raise WeatherError("主源挂了")

    fact = asyncio.run(FailoverWeather([_Broken(), _wttr()]).current(
        latitude=30.25, longitude=120.17))
    assert fact.provider == "wttr.in"           # 备源接住，且来源标记可追溯


def test_failover_reports_every_hop_when_all_fail():
    class _Bad:
        name = "bad-src"

        async def current(self, *, latitude, longitude):
            raise WeatherError("原因甲")

    with pytest.raises(WeatherError) as excinfo:
        asyncio.run(FailoverWeather([_Bad(), _Bad()]).current(
            latitude=30.25, longitude=120.17))
    message = str(excinfo.value)
    assert "原因甲" in message
    assert message.count("bad-src") == 2        # 每一跳都留名，不吞细节


def test_failover_skips_sources_without_method():
    class _NoGeocode:
        name = "no-geo"

        async def current(self, *, latitude, longitude):
            raise WeatherError("我只管实况")

    point = asyncio.run(FailoverWeather([_NoGeocode(), _wttr()]).geocode("杭州"))
    assert point is not None                    # 跳过没能力的源，不把"没能力"当失败


def test_failover_forecast_falls_through_on_day_limit():
    """主源只给 3 天、请求 5 天 → 主源如实报错 → 备源承接。"""
    class _ThreeDayOnly:
        name = "three-day"

        async def forecast(self, *, latitude, longitude, days=3):
            raise WeatherError(f"最多 3 天（请求 {days} 天）")

    class _SevenDay:
        name = "seven-day"

        async def forecast(self, *, latitude, longitude, days=3):
            from ai_butler.verticals.tour.weather import DailyForecast
            return tuple(DailyForecast(
                date=f"2026-11-{18 + i}", weather_code=2, weather_text="多云",
                temp_max_c=20.0, temp_min_c=10.0,
                precip_prob_max=0, precip_mm=0.0) for i in range(days))

    rows = asyncio.run(FailoverWeather([_ThreeDayOnly(), _SevenDay()]).forecast(
        latitude=30.25, longitude=120.17, days=5))
    assert len(rows) == 5                       # 备源给出完整天数，不静默变短


def test_failover_requires_at_least_one_port():
    with pytest.raises(WeatherError, match="至少"):
        FailoverWeather([])


# --------------------------------------------------------------------------- #
#  装配点：主备顺序
# --------------------------------------------------------------------------- #
def test_default_weather_port_wires_wttr_primary():
    port = default_weather_port()
    inner = port._port
    assert isinstance(inner, FailoverWeather)
    assert inner.name == "wttr.in+open-meteo"   # wttr 在前，open-meteo 在后备位
    assert isinstance(inner._ports[0], WttrWeather)
    assert isinstance(inner._ports[1], OpenMeteoWeather)
