"""定位隐私、坐标系转换、三大导航链接与「按所在地/喜好」推荐（U71）。

三条边界先写清楚，因为它们决定了这个模块"不做什么"
--------------------------------------------------
1. **精确坐标不出端侧**：服务端只收 `privacy_grid()` 粗化后的点（默认 3 位小数，约 110 m）。
   既然是隐私字段，就不能"先收下来再想办法保护"。
2. **没有地图 key，就不画地图**：本仓库没有任何地图服务凭据，
   所以这里不做底图、不做地址反查。界面显示「坐标 + 精度 + 时间」并注明未配置，
   不拿一张静态图冒充"实时地图"。
3. **导航链接本地生成**：按三家公开的 URI scheme 拼，不需要 key；
   同时给 web 兜底，未装 App 时点了也有反应。

坐标系：这不是可以省略的细节
----------------------------
端侧 GPS 给的是 **WGS84**；高德/腾讯用 **GCJ-02**（俗称火星坐标）；
百度用 **BD-09**。把 WGS84 直接拼进高德链接，标记点会偏 **几十到几百米**——
在"导航到店门口"这种场景里就是错的。

所以这里实现标准的三系互转（公开的偏移算法，不是抄某家的代码），
并在每个链接上**注明它期望的坐标系**，让调用方不可能"顺手传错"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

#: 默认粗化精度：3 位小数 ≈ 110 m（纬度）；经度随纬度收紧，国内约 96 m
DEFAULT_GRID_DECIMALS = 3

WGS84 = "wgs84"
GCJ02 = "gcj02"
BD09 = "bd09"

# --------------------------------------------------------------------------- #
# 校验与粗化
# --------------------------------------------------------------------------- #
class GeoError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_coordinate(lat: float, lon: float) -> tuple[float, float]:
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError) as exc:
        raise GeoError("invalid_coordinate", "经纬度必须是数字") from exc
    if not (-90.0 <= latitude <= 90.0):
        raise GeoError("invalid_coordinate", f"纬度越界：{latitude}")
    if not (-180.0 <= longitude <= 180.0):
        raise GeoError("invalid_coordinate", f"经度越界：{longitude}")
    if latitude == 0.0 and longitude == 0.0:
        # (0,0) 是几内亚湾海面，几乎总是"没取到定位"的默认值冒充成功
        raise GeoError("invalid_coordinate", "经纬度为 (0,0)，通常表示定位未取到")
    return latitude, longitude


def _truncate(value: float, decimals: int) -> float:
    """按位**截断**而不是四舍五入。

    四舍五入会让相邻两个格子共享同一条边界点，统计上出现"人都往格子角上挤"的假聚集；
    截断保证同格内所有点落在同一侧，粗化结果更稳定、也更容易解释。
    """
    factor = 10 ** decimals
    return math.trunc(value * factor) / factor


def privacy_grid(lat: float, lon: float, *, decimals: int = DEFAULT_GRID_DECIMALS) -> float:
    """把精确坐标粗化到网格。返回值仍是 (lat, lon)，但已是格点。"""
    latitude, longitude = validate_coordinate(lat, lon)
    if not (0 <= decimals <= 5):
        raise GeoError("invalid_precision", "粗化精度需在 0–5 位小数之间")
    return _truncate(latitude, decimals), _truncate(longitude, decimals)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（米）。用于「就近」排序，不追求大地测量级精度。"""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------------------- #
# 坐标系转换（WGS84 / GCJ-02 / BD-09）
# --------------------------------------------------------------------------- #
_A = 6378245.0                 # 克拉索夫斯基椭球长半轴
_EE = 0.00669342162296594323   # 第一偏心率平方
_X_PI = math.pi * 3000.0 / 180.0


def _out_of_china(lat: float, lon: float) -> bool:
    """偏移算法只在中国境内有效；境外原样返回，不要硬套。"""
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    latitude, longitude = validate_coordinate(lat, lon)
    if _out_of_china(latitude, longitude):
        return latitude, longitude
    d_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    d_lon = _transform_lon(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return latitude + d_lat, longitude + d_lon


def gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    """反解：偏移量随位置变化，没有闭式逆，用两次迭代逼近（误差 < 1 m）。"""
    latitude, longitude = validate_coordinate(lat, lon)
    if _out_of_china(latitude, longitude):
        return latitude, longitude
    # 先用一次正向偏移做初值，再迭代修正
    wgs_lat, wgs_lon = latitude, longitude
    for _ in range(2):
        g_lat, g_lon = wgs84_to_gcj02(wgs_lat, wgs_lon)
        wgs_lat += latitude - g_lat
        wgs_lon += longitude - g_lon
    return wgs_lat, wgs_lon


def gcj02_to_bd09(lat: float, lon: float) -> tuple[float, float]:
    latitude, longitude = validate_coordinate(lat, lon)
    z = math.sqrt(latitude * latitude + longitude * longitude) + 0.00002 * math.sin(
        latitude * _X_PI
    )
    theta = math.atan2(latitude, longitude) + 0.000003 * math.cos(longitude * _X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def bd09_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    latitude, longitude = validate_coordinate(lat, lon)
    x = longitude - 0.0065
    y = latitude - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.sin(theta), z * math.cos(theta)


def wgs84_to_bd09(lat: float, lon: float) -> tuple[float, float]:
    g_lat, g_lon = wgs84_to_gcj02(lat, lon)
    return gcj02_to_bd09(g_lat, g_lon)


def convert(lat: float, lon: float, *, frm: str, to: str) -> tuple[float, float]:
    """统一入口。显式给源与目标，避免"以为传的是 WGS84"这类静默错误。"""
    if frm == to:
        return validate_coordinate(lat, lon)
    if frm == WGS84 and to == GCJ02:
        return wgs84_to_gcj02(lat, lon)
    if frm == GCJ02 and to == WGS84:
        return gcj02_to_wgs84(lat, lon)
    if frm == WGS84 and to == BD09:
        return wgs84_to_bd09(lat, lon)
    if frm == GCJ02 and to == BD09:
        return gcj02_to_bd09(lat, lon)
    if frm == BD09 and to == GCJ02:
        return bd09_to_gcj02(lat, lon)
    if frm == BD09 and to == WGS84:
        g_lat, g_lon = bd09_to_gcj02(lat, lon)
        return gcj02_to_wgs84(g_lat, g_lon)
    raise GeoError("unsupported_conversion", f"不支持的坐标系转换：{frm} → {to}")


# --------------------------------------------------------------------------- #
# 导航链接
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NavLink:
    app: str                # amap / baidu / tencent / web
    label: str
    url: str
    coordinate_system: str  # 该链接期望的坐标系，便于核对
    note: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "app": self.app,
            "label": self.label,
            "url": self.url,
            "coordinate_system": self.coordinate_system,
            "note": self.note,
        }


def navigation_links(
    *,
    dest_lat: float,
    dest_lon: float,
    dest_name: str = "目的地",
    origin_lat: float | None = None,
    origin_lon: float | None = None,
    source_coord: str = WGS84,
) -> list[NavLink]:
    """生成三家导航 App 的跳转链接 + web 兜底。

    起点**默认不传**：让各家 App 用「我的位置」。这样我方不需要把用户坐标
    交给第三方，第三方取到的是它自己拿的定位 —— 少的这一次传递就是少的泄漏面。
    只有调用方明确给了起点时才带上（并做坐标系转换）。
    """
    latitude, longitude = validate_coordinate(dest_lat, dest_lon)
    name = (dest_name or "目的地").strip()[:30]

    g_lat, g_lon = convert(latitude, longitude, frm=source_coord, to=GCJ02)
    b_lat, b_lon = convert(latitude, longitude, frm=source_coord, to=BD09)

    from urllib.parse import quote

    links: list[NavLink] = []

    # 高德：uri.amap.com 同时兼容 App 与浏览器，无需 key
    if origin_lat is not None and origin_lon is not None:
        o_lat, o_lon = convert(float(origin_lat), float(origin_lon), frm=source_coord, to=GCJ02)
        amap_url = (
            "https://uri.amap.com/navigation"
            f"?from={o_lon:.6f},{o_lat:.6f},我的位置"
            f"&to={g_lon:.6f},{g_lat:.6f},{quote(name)}"
            "&mode=car&policy=1&src=aibutler&coordinate=gaode&callnative=1"
        )
    else:
        amap_url = (
            "https://uri.amap.com/navigation"
            f"?to={g_lon:.6f},{g_lat:.6f},{quote(name)}"
            "&mode=car&policy=1&src=aibutler&coordinate=gaode&callnative=1"
        )
    links.append(NavLink("amap", "高德地图", amap_url, GCJ02, "未装 App 时自动走网页"))

    # 百度：bd09ll 明确声明坐标系，避免百度按 gcj02 解释
    baidu_url = (
        f"baidumap://map/direction?destination=latlng:{b_lat:.6f},{b_lon:.6f}"
        f"|name:{quote(name)}&coord_type=bd09ll&mode=driving&src=webapp.aibutler"
    )
    links.append(NavLink("baidu", "百度地图", baidu_url, BD09, "需已安装百度地图 App"))

    # 腾讯：routeplan 的 tocoord 用 gcj02
    tencent_url = (
        f"https://apis.map.qq.com/uri/v1/routeplan?type=drive"
        f"&to={quote(name)}&tocoord={g_lat:.6f},{g_lon:.6f}&policy=0"
    )
    links.append(NavLink("tencent", "腾讯地图", tencent_url, GCJ02, "未装 App 时自动走网页"))

    # web 兜底：坐标 + 名称的通用搜索页，不依赖任何 key
    web_url = (
        "https://uri.amap.com/marker"
        f"?position={g_lon:.6f},{g_lat:.6f}&name={quote(name)}&coordinate=gaode&callnative=0"
    )
    links.append(NavLink("web", "网页查看位置", web_url, GCJ02, "没有装任何导航 App 时用这个"))

    return links


# --------------------------------------------------------------------------- #
# 推荐
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Place:
    place_id: str
    name: str
    tags: tuple[str, ...] = ()
    lat: float | None = None
    lon: float | None = None
    description: str = ""


@dataclass(frozen=True)
class Recommendation:
    place: Place
    score: float
    reason: str
    distance_m: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "place_id": self.place.place_id,
            "name": self.place.name,
            "tags": list(self.place.tags),
            "lat": self.place.lat,
            "lon": self.place.lon,
            "description": self.place.description,
            "score": round(self.score, 2),
            "reason": self.reason,
            "distance_m": round(self.distance_m) if self.distance_m is not None else None,
        }


#: 每命中一个喜好标签的加分
_TAG_WEIGHT = 2.0
#: 距离衰减半径：超过这个距离，距离项贡献趋近 0
_DISTANCE_SCALE = 5000.0


def recommend(
    places: Sequence[Place],
    *,
    prefs: Iterable[str] = (),
    located_at: tuple[float, float] | None = None,
    limit: int = 5,
) -> list[Recommendation]:
    """按「喜好命中 + 就近」排序，**每条都带理由**。

    权重刻意做得简单可解释：命中一个标签 +2；距离项最多 +2，按 5 km 缓降。
    不做"神秘排序"——用户问"为什么先给我看这个"，必须能答上来。
    """
    likes = {str(p).strip().lower() for p in prefs if str(p).strip()}
    results: list[Recommendation] = []

    for place in places:
        reasons: list[str] = []
        score = 0.0

        hits = [t for t in (tag.lower() for tag in place.tags) if t in likes]
        if hits:
            score += _TAG_WEIGHT * len(hits)
            reasons.append("喜欢" + "、".join(hits))

        distance: float | None = None
        if located_at is not None and place.lat is not None and place.lon is not None:
            distance = haversine_m(located_at[0], located_at[1], place.lat, place.lon)
            proximity = max(0.0, 1.0 - distance / _DISTANCE_SCALE) * 2.0
            score += proximity
            if distance < 1000:
                reasons.append(f"距你 {distance:.0f} 米")
            elif distance < _DISTANCE_SCALE:
                reasons.append(f"距你约 {distance / 1000:.1f} 公里")
        elif place.lat is not None:
            reasons.append("（未开启定位，仅按喜好排序）")

        if not reasons:
            reasons.append("暂无匹配的喜好标签")
        results.append(Recommendation(place, score, "；".join(reasons), distance))

    results.sort(key=lambda r: (-r.score, r.distance_m if r.distance_m is not None else 1e18))
    return results[:max(1, limit)]
