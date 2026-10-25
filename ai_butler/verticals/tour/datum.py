"""坐标基准转换（WGS-84 ↔ GCJ-02）。

**为什么必须有这一层**：设备 GPS 给的是 WGS-84，高德底图（以及高德的
逆地理结果）用的是 GCJ-02（俗称火星坐标）。两种基准在中国境内相差
**数百米**——不做转换就把 GPS 点直接画到高德底图上，点会落在隔壁街区，
而且**看起来完全正常**：没有人会怀疑一个「就在附近」的蓝点偏了 400 米。
这是典型的「错得很像对的」缺陷，所以单列一个模块并配守护。

算法为公开的 GCJ-02 加偏/纠偏变换（克拉索夫斯基椭球参数），
逆变换用迭代逼近而非网上常见的「减一次偏移」近似式（后者在北京
有数米残差，迭代三轮可收敛到厘米级）。

境外（`_out_of_china`）不加偏：高德自身对境外也不做偏移，
两边都返回原值，转换是恒等的——这是刻意的，不是漏判。

BD-09（百度）不在本项目范围内：计划书与 U71 的三大导航 URI 只涉及
高德/百度/腾讯的跳转链接，不落地百度坐标。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "GCJ_BBOX",
    "LatLon",
    "gcj02_to_wgs84",
    "in_gcj_zone",
    "offset_metres",
    "wgs84_to_gcj02",
]

_PI = math.pi
#: 克拉索夫斯基椭球长半轴（GCJ-02 采用的椭球）
_A = 6378245.0
#: 第一偏心率平方
_EE = 0.00669342162296594323

#: GCJ-02 加偏的适用矩形（经纬度）。境外不加偏。
GCJ_BBOX = (72.004, 0.8293, 137.8347, 55.8271)


@dataclass(frozen=True)
class LatLon:
    """一个经纬度点。字段顺序是 (经度, 纬度)，与高德/GeoJSON 一致。"""

    longitude: float
    latitude: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.longitude, self.latitude)


def in_gcj_zone(longitude: float, latitude: float) -> bool:
    """坐标是否落在 GCJ-02 加偏适用区域内。"""
    west, south, east, north = GCJ_BBOX
    return west <= longitude <= east and south <= latitude <= north


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y
           + 0.1 * x * y + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def _delta(longitude: float, latitude: float) -> tuple[float, float]:
    """返回 (经度偏移, 纬度偏移)，单位为度。"""
    dlat = _transform_lat(longitude - 105.0, latitude - 35.0)
    dlon = _transform_lon(longitude - 105.0, latitude - 35.0)
    radlat = latitude / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return dlon, dlat


def wgs84_to_gcj02(longitude: float, latitude: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02。境外原样返回。"""
    if not in_gcj_zone(longitude, latitude):
        return longitude, latitude
    dlon, dlat = _delta(longitude, latitude)
    return longitude + dlon, latitude + dlat


def gcj02_to_wgs84(longitude: float, latitude: float,
                   *, iterations: int = 3) -> tuple[float, float]:
    """GCJ-02 → WGS-84。

    用迭代逼近而不是「减一次偏移」的近似式：近似式把加偏当作常数，
    而它其实随位置变化，在北京会留数米残差。迭代三轮收敛到厘米级。
    """
    if not in_gcj_zone(longitude, latitude):
        return longitude, latitude
    if iterations < 1:
        raise ValueError("iterations 至少为 1")
    wlon, wlat = longitude, latitude
    for _ in range(iterations):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon += longitude - glon
        wlat += latitude - glat
    return wlon, wlat


def offset_metres(longitude: float, latitude: float) -> float:
    """该点两种基准的平面距离（米）——用于「偏移量是否可忽略」的判断。"""
    wlon, wlat = gcj02_to_wgs84(longitude, latitude)
    # 局部平面近似：1 度纬度 ≈ 111.32 km，经度随纬度收缩
    dy = (latitude - wlat) * 111320.0
    dx = (longitude - wlon) * 111320.0 * math.cos(math.radians(latitude))
    return math.hypot(dx, dy)
