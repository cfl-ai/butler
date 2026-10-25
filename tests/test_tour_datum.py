"""坐标基准转换的守护测试（WGS-84 ↔ GCJ-02）。

这批测试守的是一件**看起来不会错**的事：GPS 点画到高德底图上会偏。
偏移量几百米，肉眼只觉得「点好像在附近」，没人会去怀疑基准。
"""

from __future__ import annotations

import math

import pytest

from ai_butler.verticals.tour.datum import (
    GCJ_BBOX,
    gcj02_to_wgs84,
    in_gcj_zone,
    offset_metres,
    wgs84_to_gcj02,
)

#: 天安门（WGS-84 近似值）
BEIJING = (116.3975, 39.9087)
#: 纽约（境外，不加偏）
NEW_YORK = (-74.0060, 40.7128)


def test_beijing_offset_is_hundreds_of_metres():
    """北京两种基准相差数百米——这正是「点画错位置」的量级。"""
    offset = offset_metres(*BEIJING)
    assert 100.0 < offset < 1000.0, f"偏移量 {offset:.0f}m 不合常理"


def test_offset_is_zero_outside_gcj_zone():
    """境外不加偏：转换是恒等的。这不是漏判，是高德自己也不加。"""
    assert wgs84_to_gcj02(*NEW_YORK) == NEW_YORK
    assert gcj02_to_wgs84(*NEW_YORK) == NEW_YORK
    assert offset_metres(*NEW_YORK) == pytest.approx(0.0, abs=1e-6)


def test_round_trip_is_accurate_to_centimetres():
    """wgs84 → gcj02 → wgs84 回到原处。

    用「减一次偏移」的近似式会留数米残差，迭代式应收敛到厘米级
    （1e-7 度 ≈ 1.1 cm）。
    """
    for lon, lat in (BEIJING, (121.4737, 31.2304), (113.2644, 23.1291)):
        glon, glat = wgs84_to_gcj02(lon, lat)
        wlon, wlat = gcj02_to_wgs84(glon, glat)
        assert abs(wlon - lon) < 1e-7, f"经度残差过大：{abs(wlon - lon):.3e}"
        assert abs(wlat - lat) < 1e-7, f"纬度残差过大：{abs(wlat - lat):.3e}"


def test_gcj02_is_northeast_of_wgs84_in_china():
    """GCJ-02 相对 WGS-84 在中国境内是往东北偏（该算法的已知特征）。"""
    glon, glat = wgs84_to_gcj02(*BEIJING)
    assert glon > BEIJING[0]
    assert glat > BEIJING[1]


def test_zone_boundaries_are_inclusive():
    west, south, east, north = GCJ_BBOX
    assert in_gcj_zone(west, south) is True
    assert in_gcj_zone(east, north) is True
    # 边界外一步就是境外
    assert in_gcj_zone(west - 0.001, south) is False
    assert in_gcj_zone(east + 0.001, north) is False


def test_offset_varies_by_location():
    """偏移量随位置变化——所以逆变换不能写成「减固定值」。"""
    offsets = [offset_metres(*p) for p in
               (BEIJING, (121.4737, 31.2304), (113.2644, 23.1291))]
    assert len(set(round(o, 1) for o in offsets)) == len(offsets)
    assert all(50.0 < o < 1500.0 for o in offsets)


def test_iterations_must_be_positive():
    with pytest.raises(ValueError, match="iterations"):
        gcj02_to_wgs84(*BEIJING, iterations=0)


def test_conversion_does_not_produce_nan():
    """极点/原点这类极端输入不能算出 NaN——NaN 会静默穿过所有校验。"""
    for lon, lat in ((0.0, 0.0), (180.0, 90.0), (-180.0, -90.0)):
        glon, glat = wgs84_to_gcj02(lon, lat)
        assert not math.isnan(glon) and not math.isnan(glat)
