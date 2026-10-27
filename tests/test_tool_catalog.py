"""真实工具清单（`scheduler/tool_catalog.py`）的守护测试。

守三件事：
1. **清单能落到模型手里**：schema + 能力归属两道闸都过（否则接通层永远空转）；
2. **user_id 由调度器注入，模型伪造无效** —— 这是数据隔离的底线，不是风格问题；
3. **越界入参不炸链路**：空 query 得到失败结果、top_k 被夹到上限、长文截断留痕。

真实 SearchService 的端到端命中由 `scripts/smoke_p1_p4.py` 的第 9 步覆盖
（E-99：mock 看不见端点/参数错误，所以真实路径必须在非测试脚本里也走一遍）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_butler.agents.capabilities import CAP_EXTERNAL_READ
from ai_butler.scheduler.tool_catalog import (
    HIT_TEXT_MAX_CHARS,
    MAX_HITS,
    build_tool_registry,
)
from ai_butler.scheduler.tools import ToolCall, ToolDispatcher, ToolRegistry

_SQL = "西湖景区春季六点半开放，门票可在线预约。"


class StubSearchService:
    """记录实际收到的 user_id / top_k —— 用来看注入与夹取是否生效。"""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int | None]] = []

    def search(self, query: str, *, user_id: str, top_k: int | None = None):
        self.seen.append((query, user_id, top_k))
        hits = [SimpleNamespace(doc_id=f"doc-{i}", text=_SQL * 20, score=0.9 - i / 100,
                                evidence_ids=[f"ev-{i}"]) for i in range(top_k or 5)]
        return SimpleNamespace(query=query, user_id=user_id, hits=hits,
                               searched_scopes=["public", f"vault:{user_id}"])


def _dispatcher(registry: ToolRegistry) -> ToolDispatcher:
    from ai_butler.core.permissions import (
        AgentIdentity, CapabilityLevel, ToolPermission, UserRole)
    d = ToolDispatcher(registry)
    d.bind_permission(ToolPermission(
        agent=AgentIdentity(key="catalog_test", level=CapabilityLevel.L1_WRITE_OWN),
        role=UserRole.MEMBER))
    return d


# --------------------------------------------------------------------------- #
# 1. 清单可见性
# --------------------------------------------------------------------------- #
def test_catalog_tools_are_exposed_with_schema_and_capability():
    registry = build_tool_registry(search_service=StubSearchService(),
                                   wiki_store=None, external_tools=False)
    exposed = registry.as_openai_tools()
    assert [t["function"]["name"] for t in exposed] == ["search_materials"]
    assert exposed[0]["function"]["parameters"]["required"] == ["query"]
    spec = registry.get("search_materials")
    assert spec.capability, "没有能力归属的工具不会被下发"


def test_missing_dependency_registers_nothing_instead_of_a_broken_shell():
    """缺依赖就少注册，不是注册一个调用必炸的空壳。"""
    assert build_tool_registry(external_tools=False).names() == ()


# --------------------------------------------------------------------------- #
# 1b. 外部只读工具（U118）
# --------------------------------------------------------------------------- #
class StubWeatherPort:
    """假天气端口：不发网络请求，但走真实的 WeatherCollector 解析路径。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def geocode(self, name):
        self.seen.append(name)
        from ai_butler.verticals.tour.weather import GeoPoint
        return GeoPoint(name=name, latitude=30.25, longitude=120.16,
                        country="中国", admin1="浙江", ambiguous=False)

    async def current(self, *, latitude, longitude):
        from ai_butler.verticals.tour.weather import WeatherFact
        return WeatherFact(latitude=latitude, longitude=longitude,
                           temperature_c=21.5, weather_code=0, weather_text="晴",
                           wind_kmh=8.0, fetched_at="2026-10-27T00:00:00+00:00",
                           provider="stub-weather")


def test_weather_tool_is_registered_by_default_and_readable():
    """天气是「默认打开就有用」的那个工具：无 key、只读、能落到模型手里。"""
    port = StubWeatherPort()
    registry = build_tool_registry(search_service=StubSearchService(),
                                   weather_port=port, settings=None)
    assert "get_weather" in registry.names()
    spec = registry.get("get_weather")
    assert spec.capability == CAP_EXTERNAL_READ
    assert spec.exposed_to_model is True
    assert spec.params_schema["required"] == ["city"]

    result = _dispatcher(registry).dispatch(
        ToolCall(name="get_weather", payload={"city": "杭州"}), user_id="user-A")
    assert result.ok, result.note
    assert result.output["weather"] == "晴"
    assert result.output["temperature_c"] == 21.5
    assert port.seen == ["杭州"], "地名要真的交给端口解析"


def test_weather_tool_carries_forecast_when_the_port_supports_it():
    """问「明天会下雨吗」要有预报（U119）：只有实况时模型会判定查不到天气。

    这条守的是「能力有就要给到模型」：`get_weather` 的返回里必须带
    未来几天的日期/天气/温度/降水概率，而不是只给一个当前气温。
    """
    from ai_butler.verticals.tour.weather import DailyForecast

    class ForecastPort(StubWeatherPort):
        async def forecast(self, *, latitude, longitude, days=3):
            return tuple(
                DailyForecast(date=f"2026-10-{27 + i}", weather_code=61,
                              weather_text="小雨", temp_max_c=21.0 + i,
                              temp_min_c=12.0 + i, precip_prob_max=60, precip_mm=3.2)
                for i in range(days))

    registry = build_tool_registry(weather_port=ForecastPort())
    result = _dispatcher(registry).dispatch(
        ToolCall(name="get_weather", payload={"city": "杭州", "days": 2}),
        user_id="user-A")
    assert result.ok, result.note
    forecast = result.output["forecast"]
    assert len(forecast) == 2, "days 要真的传下去"
    assert forecast[0]["weather_text"] == "小雨"
    assert "%" in forecast[0]["text"], "现成的一句话要带单位，免得模型念成「降水概率60」"
    assert result.output["forecast_error"] == ""


def test_weather_tool_still_answers_when_forecast_is_unavailable():
    """源不支持预报时：实况照给，预报缺席的原因如实写出来（不整体失败）。"""
    registry = build_tool_registry(weather_port=StubWeatherPort())
    result = _dispatcher(registry).dispatch(
        ToolCall(name="get_weather", payload={"city": "杭州"}), user_id="user-A")
    assert result.ok, result.note
    assert result.output["temperature_c"] == 21.5
    assert result.output["forecast"] == []
    assert "预报不可用" in result.output["forecast_error"]


def test_weather_tool_fails_honestly_when_place_cannot_be_resolved():
    class Broken(StubWeatherPort):
        async def geocode(self, name):
            return None

    registry = build_tool_registry(weather_port=Broken())
    result = _dispatcher(registry).dispatch(
        ToolCall(name="get_weather", payload={"city": "不存在的地方"}), user_id="user-A")
    assert result.ok is False, "解析不到地名必须失败，不能编一个坐标"
    assert "解析" in (result.note or "") or "取不到" in (result.note or "")


def test_poi_tool_is_registered_only_when_a_key_exists():
    """没有高德 key 就不注册 search_poi：模型不该看见必然失败的工具。"""
    assert "search_poi" not in build_tool_registry().names()

    class NoKeySettings:
        amap_key = ""

    assert "search_poi" not in build_tool_registry(settings=NoKeySettings()).names()

    class WithKeySettings:
        amap_key = "fake-key"

    registry = build_tool_registry(settings=WithKeySettings())
    assert "search_poi" in registry.names()
    assert registry.get("search_poi").capability == CAP_EXTERNAL_READ


def test_external_tools_can_be_switched_off_for_offline_assembly():
    """离线装配/测试不该被外部网络绑架：`external_tools=False` 一个都不注册。"""
    assert build_tool_registry(external_tools=False).names() == ()


# --------------------------------------------------------------------------- #
# 2. user_id 注入
# --------------------------------------------------------------------------- #
def test_user_id_is_injected_by_dispatcher_not_supplied_by_model():
    service = StubSearchService()
    dispatcher = _dispatcher(build_tool_registry(search_service=service))
    result = dispatcher.dispatch(
        # 模型若尝试自己塞 user_id，必须被 State 里的值覆盖
        ToolCall(name="search_materials",
                 payload={"query": "西湖", "user_id": "user-EVIL"}),
        user_id="user-A")
    assert result.ok, result.note
    assert service.seen[0][1] == "user-A", "模型传入的 user_id 不得生效"


def test_user_id_missing_is_a_refusal_not_an_anonymous_read():
    service = StubSearchService()
    dispatcher = _dispatcher(build_tool_registry(search_service=service))
    result = dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "西湖"}), user_id="")
    assert result.ok is False
    assert service.seen == [], "没绑定账号就不该真的去检索"


# --------------------------------------------------------------------------- #
# 3. 入参与输出边界
# --------------------------------------------------------------------------- #
def test_empty_query_fails_with_a_result_not_an_exception():
    dispatcher = _dispatcher(build_tool_registry(search_service=StubSearchService()))
    result = dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "   "}), user_id="user-A")
    assert result.ok is False


def test_top_k_is_clamped_to_the_hard_ceiling():
    service = StubSearchService()
    dispatcher = _dispatcher(build_tool_registry(search_service=service))
    dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "西湖", "top_k": 999}),
        user_id="user-A")
    assert service.seen[0][2] == MAX_HITS, "模型要 999 条也不能真给 999 条"


def test_long_hit_text_is_truncated_and_says_so():
    dispatcher = _dispatcher(build_tool_registry(search_service=StubSearchService()))
    result = dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "西湖", "top_k": 1}),
        user_id="user-A")
    text = result.output["hits"][0]["text"]
    assert len(text) <= HIT_TEXT_MAX_CHARS + len("…（已截断）")
    assert text.endswith("…（已截断）")
    assert result.output["truncated_text"] is True


def test_missing_wiki_page_is_a_conclusion_not_an_error():
    """「没找到」要让模型据此换路径，而不是当成故障反复重试同一个 id。"""
    class EmptyWiki:
        def read_page(self, page_id, *, user_id=None):
            return None

    dispatcher = _dispatcher(build_tool_registry(wiki_store=EmptyWiki()))
    result = dispatcher.dispatch(
        ToolCall(name="read_wiki_page", payload={"page_id": "p-missing"}),
        user_id="user-A")
    assert result.ok is True
    assert result.output == {"page_id": "p-missing", "found": False}
