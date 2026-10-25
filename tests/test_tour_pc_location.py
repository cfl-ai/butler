"""电脑端定位的守护测试（U105）。

守的重点是「不许假装」：IP 定位是城市级，就必须标粗定位；
没有来源就必须报错，不许猜一个默认城市。
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from ai_butler.verticals.tour.pc_location import (
    IpWhoIsLocation,
    PcFix,
    PcLocationError,
    resolve_pc_location,
)

LIVE = os.environ.get("LIVE_PCLOC") == "1"

_PAYLOAD = {
    "ip": "117.176.242.101", "success": True, "type": "IPv4",
    "continent": "Asia", "country": "China", "country_code": "CN",
    "region": "Sichuan", "city": "Chengdu",
    "latitude": 30.6667, "longitude": 104.0667,
}


class _Port:
    def __init__(self, payload=None, fail: bool = False) -> None:
        self.payload = payload or _PAYLOAD
        self.fail = fail
        self.calls = 0

    async def locate(self) -> PcFix:
        self.calls += 1
        if self.fail:
            raise PcLocationError("模拟定位失败")
        return _fix_from(self.payload)


def _fix_from(payload) -> PcFix:
    return PcFix(latitude=payload["latitude"], longitude=payload["longitude"],
                 city=payload.get("city", ""), region=payload.get("region", ""),
                 country=payload.get("country", ""))


def _source(handler, **kw) -> IpWhoIsLocation:
    return IpWhoIsLocation(transport=httpx.MockTransport(handler), **kw)


# --------------------------------------------------------------------------- #
def test_ipwhois_parses_city_and_coords():
    fix = asyncio.run(_source(lambda r: httpx.Response(200, json=_PAYLOAD)).locate())
    assert fix.city == "Chengdu"
    assert fix.latitude == pytest.approx(30.6667)
    assert fix.longitude == pytest.approx(104.0667)
    assert fix.provider == "ipwhois"


def test_pc_fix_is_always_coarse():
    """IP 定位恒为粗定位——把城市级当 GPS 用是这类系统的经典事故。"""
    fix = asyncio.run(_source(lambda r: httpx.Response(200, json=_PAYLOAD)).locate())
    assert fix.coarse is True
    assert "不可当 GPS" in fix.as_payload()["note"]
    assert "粗定位" in fix.one_line()


def test_unsuccessful_response_is_an_error():
    async def go():
        return await _source(lambda r: httpx.Response(
            200, json={"success": False, "message": "Reserved range"})).locate()
    with pytest.raises(PcLocationError, match="未成功"):
        asyncio.run(go())


def test_missing_coords_is_an_error():
    async def go():
        return await _source(
            lambda r: httpx.Response(200, json={"success": True, "city": "X"})).locate()
    with pytest.raises(PcLocationError, match="缺少坐标"):
        asyncio.run(go())


def test_http_failure_is_an_error():
    async def go():
        return await _source(lambda r: httpx.Response(503, json={})).locate()
    with pytest.raises(PcLocationError, match="请求失败"):
        asyncio.run(go())


def test_plain_http_endpoint_is_refused():
    with pytest.raises(PcLocationError, match="https"):
        IpWhoIsLocation(url="http://ipwho.is/")


def test_override_wins_over_network():
    port = _Port()
    fix = asyncio.run(resolve_pc_location(override=(104.0667, 30.6667), port=port))
    assert port.calls == 0
    assert fix.provider == "override"
    # 显式坐标仍然标粗：它是「这台电脑在哪」的城市级事实，不是 GPS 观测
    assert fix.coarse is True


def test_no_source_raises_instead_of_guessing():
    """没有来源就报错——静默返回「北京」会让整条规划建在假位置上。"""
    async def go():
        return await resolve_pc_location()
    with pytest.raises(PcLocationError, match="没有定位来源"):
        asyncio.run(go())


def test_port_failure_propagates():
    async def go():
        return await resolve_pc_location(port=_Port(fail=True))
    with pytest.raises(PcLocationError, match="模拟定位失败"):
        asyncio.run(go())


def test_payload_round_trips():
    fix = PcFix(latitude=30.6667, longitude=104.0667, city="Chengdu")
    payload = fix.as_payload()
    assert payload["city"] == "Chengdu"
    assert payload["coarse"] is True
    assert payload["accuracy_km"] >= 0


# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not LIVE, reason="未设置 LIVE_PCLOC=1（真实网络冒烟）")
def test_live_pc_location():
    """真实取一次：能拿到城市级坐标，且确实是粗定位。"""
    fix = asyncio.run(IpWhoIsLocation().locate())
    assert -90.0 <= fix.latitude <= 90.0
    assert -180.0 <= fix.longitude <= 180.0
    assert fix.coarse is True
    assert fix.city or fix.country
