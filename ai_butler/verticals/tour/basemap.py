"""世界底图源裁定（U102）：国内高德，境外 OSM。

**为什么是两个源而不是一个**：高德只覆盖中国大陆——实测北京瓦片
8568 字节（真实路网），纽约与伦敦同为 179 字节（空白底图）。
「完整世界地图」要成立，境外必须有另一个源；OSM 系（CARTO 瓦片）
全球覆盖且免费无 key。

**因此裁定是「按位置切源」，不是「选一个凑合全部」**：

- 落在高德覆盖矩形内 → 高德瓦片（GCJ-02）；
- 其外 → CARTO/OSM 瓦片（WGS-84）。

两条必须随结果一起交出去，否则使用者无从判断自己看的是哪一套：

1. `datum`：底图坐标系。混用会让点位偏移数百米且**看起来正常**；
2. `attribution`：版权署名。OSM 与 CARTO 要求署名，高德亦然，
   不署就是侵权——这一条不是可选项。

**覆盖判定是矩形近似**，不是精确国界：矩形内的邻国小块会被判成
「国内」而拿到空白瓦片。精确判定要官方 API（需 key）。矩形近似
的代价是「边缘地区底图空白」，精确判定的代价是「多一次网络请求」；
空白是可见的、会被修的，而把境外错当境内并画出一张错图是不可见的。

实测坑：`a.basemaps.cartocdn.com` 这类带子域的主机名在本机解析不了，
**不带子域的 `basemaps.cartocdn.com` 才通**——故 subdomains 留空。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "AMAP_COVERAGE_BBOX",
    "AMAP_SATELLITE",
    "AMAP_VECTOR",
    "AMAP_VECTOR_LABELLED",
    "CARTO_DARK",
    "CARTO_LIGHT",
    "TileSource",
    "basemap_for",
    "in_amap_coverage",
    "tile_bbox",
    "tile_url",
]

#: 高德栅格覆盖的近似矩形（西, 南, 东, 北）。**不是国界**。
AMAP_COVERAGE_BBOX = (73.5, 3.8, 135.1, 53.6)


@dataclass(frozen=True)
class TileSource:
    """一个瓦片底图源。"""

    key: str
    name: str
    url_template: str
    attribution: str
    #: 坐标基准——决定打点要不要先转换
    datum: str
    #: 覆盖范围描述
    coverage: str
    max_zoom: int = 18
    subdomains: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, str | int]:
        return {"key": self.key, "name": self.name,
                "url_template": self.url_template,
                "attribution": self.attribution, "datum": self.datum,
                "coverage": self.coverage, "max_zoom": self.max_zoom,
                "subdomains": list(self.subdomains)}


AMAP_VECTOR = TileSource(
    key="amap-vector",
    name="高德矢量底图",
    url_template=("https://webrd0{s}.is.autonavi.com/appmaptile"
                  "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"),
    attribution="© 高德地图",
    datum="GCJ-02",
    coverage="中国大陆（bbox 近似）",
    max_zoom=18,
    subdomains=("1", "2", "3", "4"),
)

AMAP_VECTOR_LABELLED = TileSource(
    key="amap-vector-labelled",
    name="高德矢量底图（含路名）",
    url_template=("https://webrd0{s}.is.autonavi.com/appmaptile"
                  "?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}"),
    attribution="© 高德地图",
    datum="GCJ-02",
    coverage="中国大陆（bbox 近似）",
    max_zoom=18,
    subdomains=("1", "2", "3", "4"),
)

AMAP_SATELLITE = TileSource(
    key="amap-satellite",
    name="高德卫星影像",
    url_template=("https://webst0{s}.is.autonavi.com/appmaptile"
                  "?style=6&x={x}&y={y}&z={z}"),
    attribution="© 高德地图",
    datum="GCJ-02",
    coverage="中国大陆（bbox 近似）",
    max_zoom=18,
    subdomains=("1", "2", "3", "4"),
)

CARTO_LIGHT = TileSource(
    key="carto-light",
    name="CARTO 浅色底图（OSM 数据）",
    url_template="https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    attribution="© OpenStreetMap contributors © CARTO",
    datum="WGS-84",
    coverage="全球",
    max_zoom=20,
)

CARTO_DARK = TileSource(
    key="carto-dark",
    name="CARTO 深色底图（OSM 数据）",
    url_template="https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    attribution="© OpenStreetMap contributors © CARTO",
    datum="WGS-84",
    coverage="全球",
    max_zoom=20,
)

_BY_KEY: Mapping[str, TileSource] = {
    src.key: src for src in (AMAP_VECTOR, AMAP_VECTOR_LABELLED,
                              AMAP_SATELLITE, CARTO_LIGHT, CARTO_DARK)
}

#: 国内/境外各自的默认风格
_DEFAULT_DOMESTIC = AMAP_VECTOR_LABELLED
_DEFAULT_OVERSEAS = CARTO_LIGHT


def in_amap_coverage(longitude: float, latitude: float) -> bool:
    """坐标是否落在高德瓦片覆盖矩形内（bbox 近似，不是精确国界）。"""
    west, south, east, north = AMAP_COVERAGE_BBOX
    return west <= longitude <= east and south <= latitude <= north


def basemap_for(longitude: float, latitude: float, *,
                domestic: TileSource | None = None,
                overseas: TileSource | None = None) -> TileSource:
    """按位置裁定底图源：国内高德，境外 OSM。"""
    return (domestic or _DEFAULT_DOMESTIC) if in_amap_coverage(longitude, latitude) \
        else (overseas or _DEFAULT_OVERSEAS)


def tile_url(source: TileSource, *, z: int, x: int, y: int) -> str:
    """拼出瓦片 URL。子域按瓦片号确定性选取——同一块瓦片永远走同一台，
    否则浏览器/端侧缓存会被打成四份。"""
    for name, value in (("z", z), ("x", x), ("y", y)):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"瓦片 {name} 必须是非负整数：{value!r}")
    if z > source.max_zoom:
        raise ValueError(f"{source.name} 最大缩放级别是 {source.max_zoom}，收到 {z}")
    sub = ""
    if source.subdomains:
        sub = source.subdomains[(x + y) % len(source.subdomains)]
    return source.url_template.format(z=z, x=x, y=y, s=sub)


def tile_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """瓦片号 → (西, 南, 东, 北)。用于按瓦片而不是按点判断该用哪个源。"""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north
