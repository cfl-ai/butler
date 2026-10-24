"""定位隐私、坐标系转换、导航链接与推荐（U71）。

重点守三件事：

* **精确坐标不出端侧**：粗化按位截断，且是幂等的
* **坐标系不能传错**：WGS84→GCJ-02 的偏移在国内是几百米级，
  这条测试证明"确实转了"，而不是把 WGS84 直接拼进高德链接
* **推荐可解释**：每条结果都有 `reason`，且关掉定位后是降级而不是报错
"""

from __future__ import annotations

import pytest

from ai_butler.core import geo

#: 天安门附近的 WGS84 坐标（示例点，用于坐标系偏移的量级校验）
BEIJING = (39.908722, 116.397499)


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("lat,lon", [(91, 116), (-91, 116), (39, 181), (39, -181)])
def test_out_of_range_coordinates_are_rejected(lat: float, lon: float) -> None:
    with pytest.raises(geo.GeoError) as err:
        geo.validate_coordinate(lat, lon)
    assert err.value.code == "invalid_coordinate"


def test_zero_zero_is_rejected_as_missing_fix() -> None:
    """(0,0) 几乎总是"没取到定位"的默认值 —— 不能当成有效坐标继续用。"""
    with pytest.raises(geo.GeoError) as err:
        geo.validate_coordinate(0.0, 0.0)
    assert err.value.code == "invalid_coordinate"
    assert "0,0" in err.value.message


def test_non_numeric_coordinates_are_rejected() -> None:
    with pytest.raises(geo.GeoError):
        geo.validate_coordinate("abc", 116.0)      # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 粗化
# --------------------------------------------------------------------------- #
def test_privacy_grid_truncates_instead_of_rounding() -> None:
    """截断：同格内的点都落在同一侧，不会出现"往格子角上挤"的假聚集。"""
    lat, lon = geo.privacy_grid(39.908722, 116.397499)
    assert lat == 39.908
    assert lon == 116.397


def test_privacy_grid_is_idempotent() -> None:
    once = geo.privacy_grid(*BEIJING)
    twice = geo.privacy_grid(*once)
    assert once == twice


def test_privacy_grid_error_is_bounded() -> None:
    """粗化误差必须小于 3 位小数对应的量级（约 110 m），否则等于没粗化。"""
    grid = geo.privacy_grid(*BEIJING, decimals=3)
    drift = geo.haversine_m(BEIJING[0], BEIJING[1], grid[0], grid[1])
    assert drift < 160, f"粗化后偏移 {drift:.0f} m，超出预期"


def test_privacy_grid_precision_is_configurable_and_bounded() -> None:
    coarse = geo.privacy_grid(*BEIJING, decimals=2)
    fine = geo.privacy_grid(*BEIJING, decimals=4)
    assert coarse == (39.90, 116.39)
    assert fine[0] == 39.9087
    with pytest.raises(geo.GeoError):
        geo.privacy_grid(*BEIJING, decimals=9)


# --------------------------------------------------------------------------- #
# 坐标系
# --------------------------------------------------------------------------- #
def test_wgs84_to_gcj02_shifts_hundreds_of_meters_in_china() -> None:
    """如果这里偏移接近 0，说明转换根本没生效 —— 导航会偏几百米。"""
    g_lat, g_lon = geo.wgs84_to_gcj02(*BEIJING)
    drift = geo.haversine_m(BEIJING[0], BEIJING[1], g_lat, g_lon)
    assert 50 < drift < 1000, f"偏移量 {drift:.1f} m 不在国内 GCJ-02 的合理量级"


def test_gcj02_round_trip_is_accurate() -> None:
    g_lat, g_lon = geo.wgs84_to_gcj02(*BEIJING)
    back_lat, back_lon = geo.gcj02_to_wgs84(g_lat, g_lon)
    assert geo.haversine_m(BEIJING[0], BEIJING[1], back_lat, back_lon) < 1.0


def test_bd09_round_trip_is_accurate() -> None:
    g_lat, g_lon = geo.wgs84_to_gcj02(*BEIJING)
    b_lat, b_lon = geo.gcj02_to_bd09(g_lat, g_lon)
    back_lat, back_lon = geo.bd09_to_gcj02(b_lat, b_lon)
    assert geo.haversine_m(g_lat, g_lon, back_lat, back_lon) < 1.0


def test_outside_china_coordinates_are_left_untouched() -> None:
    """偏移算法只在中国境内成立，境外硬套会把坐标改坏。"""
    lat, lon = 51.5074, -0.1278            # 伦敦
    assert geo.wgs84_to_gcj02(lat, lon) == (lat, lon)
    assert geo.convert(lat, lon, frm=geo.WGS84, to=geo.GCJ02) == (lat, lon)


def test_unknown_coordinate_system_is_rejected() -> None:
    with pytest.raises(geo.GeoError) as err:
        geo.convert(*BEIJING, frm="mars", to=geo.GCJ02)
    assert err.value.code == "unsupported_conversion"


# --------------------------------------------------------------------------- #
# 导航链接
# --------------------------------------------------------------------------- #
def test_navigation_links_cover_all_three_apps_plus_web_fallback() -> None:
    links = geo.navigation_links(dest_lat=BEIJING[0], dest_lon=BEIJING[1], dest_name="测试点")
    apps = [link.app for link in links]
    assert apps == ["amap", "baidu", "tencent", "web"]


def test_each_link_declares_its_expected_coordinate_system() -> None:
    """链接上的坐标系必须写清楚，否则调用方"顺手传 WGS84"就会偏几百米。"""
    links = {l.app: l for l in geo.navigation_links(dest_lat=BEIJING[0], dest_lon=BEIJING[1])}
    assert links["amap"].coordinate_system == geo.GCJ02
    assert links["tencent"].coordinate_system == geo.GCJ02
    assert links["baidu"].coordinate_system == geo.BD09


def test_links_carry_converted_coordinates_not_raw_wgs84() -> None:
    g_lat, g_lon = geo.wgs84_to_gcj02(*BEIJING)
    links = {l.app: l for l in geo.navigation_links(dest_lat=BEIJING[0], dest_lon=BEIJING[1])}
    # 高德链接里出现的是 GCJ-02 的经纬度（经度在前）
    assert f"{g_lon:.6f}" in links["amap"].url
    assert f"{g_lat:.6f}" in links["amap"].url
    # 原始 WGS84 不该原样出现（除非巧合相等）
    assert f"{BEIJING[1]:.6f}" != f"{g_lon:.6f}"


def test_origin_is_omitted_by_default_to_avoid_sharing_user_location() -> None:
    """默认不传起点 —— 让各家 App 用「我的位置」，我方坐标不外传。"""
    url = geo.navigation_links(dest_lat=BEIJING[0], dest_lon=BEIJING[1])[0].url
    assert "from=" not in url


def test_origin_is_converted_when_provided() -> None:
    origin = (39.900000, 116.400000)
    o_lat, o_lon = geo.wgs84_to_gcj02(*origin)
    url = geo.navigation_links(
        dest_lat=BEIJING[0], dest_lon=BEIJING[1], origin_lat=origin[0], origin_lon=origin[1]
    )[0].url
    assert f"from={o_lon:.6f},{o_lat:.6f}" in url


def test_link_names_are_url_encoded() -> None:
    url = geo.navigation_links(dest_lat=BEIJING[0], dest_lon=BEIJING[1], dest_name="故宫 太和殿")[0].url
    assert " " not in url
    assert "%20" in url or "故宫" in url      # 中文可保留但空格必须编码


def test_navigation_rejects_invalid_destination() -> None:
    with pytest.raises(geo.GeoError):
        geo.navigation_links(dest_lat=0.0, dest_lon=0.0)


# --------------------------------------------------------------------------- #
# 推荐
# --------------------------------------------------------------------------- #
def _places() -> list[geo.Place]:
    return [
        geo.Place("p1", "安静的咖啡馆", ("咖啡", "安静"), 39.9090, 116.3980),
        geo.Place("p2", "热闹的商场", ("购物", "热闹"), 39.9100, 116.4000),
        geo.Place("p3", "远处的咖啡馆", ("咖啡",), 40.1000, 116.6000),
        geo.Place("p4", "无坐标的小店", ("咖啡",)),
    ]


def test_recommendation_prefers_matching_prefs() -> None:
    results = geo.recommend(_places(), prefs=["咖啡"], located_at=BEIJING, limit=4)
    assert results[0].place.place_id == "p1"
    assert "喜欢咖啡" in results[0].reason


def test_recommendation_explains_every_item() -> None:
    for item in geo.recommend(_places(), prefs=["咖啡"], located_at=BEIJING, limit=4):
        assert item.reason, f"{item.place.name} 没有给出理由"


def test_recommendation_includes_distance_when_located() -> None:
    results = geo.recommend(_places(), prefs=["咖啡"], located_at=BEIJING, limit=4)
    nearest = results[0]
    assert nearest.distance_m is not None
    assert nearest.distance_m < 1000


def test_recommendation_degrades_without_location_instead_of_failing() -> None:
    """关掉定位 → 只按喜好排，并**说明为什么顺序变了**（`located=false` 的由来）。"""
    results = geo.recommend(_places(), prefs=["咖啡"], located_at=None, limit=4)
    assert results, "没有定位时不应返回空"
    assert any("未开启定位" in item.reason for item in results)
    assert all(item.distance_m is None for item in results)


def test_recommendation_handles_places_without_coordinates() -> None:
    results = geo.recommend(_places(), prefs=[], located_at=BEIJING, limit=4)
    nameless = [r for r in results if r.place.place_id == "p4"]
    assert nameless and nameless[0].distance_m is None


def test_recommendation_respects_limit_and_is_deterministic() -> None:
    first = geo.recommend(_places(), prefs=["咖啡"], located_at=BEIJING, limit=2)
    second = geo.recommend(_places(), prefs=["咖啡"], located_at=BEIJING, limit=2)
    assert len(first) == 2
    assert [r.place.place_id for r in first] == [r.place.place_id for r in second]


def test_recommendation_dict_payload_is_json_friendly() -> None:
    payload = geo.recommend(_places(), prefs=["咖啡"], located_at=BEIJING, limit=1)[0].as_dict()
    assert isinstance(payload["tags"], list)
    assert isinstance(payload["score"], float)
    assert "reason" in payload
