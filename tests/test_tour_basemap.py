"""世界底图双源裁定的守护测试（U102）。

守三件事：国内走高德、境外走 OSM、以及**两个源的坐标系不许混**。
"""

from __future__ import annotations

import pytest

from ai_butler.verticals.tour.basemap import (
    AMAP_COVERAGE_BBOX,
    AMAP_SATELLITE,
    AMAP_VECTOR,
    AMAP_VECTOR_LABELLED,
    CARTO_DARK,
    CARTO_LIGHT,
    TileSource,
    basemap_for,
    in_amap_coverage,
    tile_bbox,
    tile_url,
)

ALL_SOURCES = (AMAP_VECTOR, AMAP_VECTOR_LABELLED, AMAP_SATELLITE,
               CARTO_LIGHT, CARTO_DARK)

BEIJING = (116.3975, 39.9087)
SHENZHEN = (114.0579, 22.5431)
NEW_YORK = (-74.0060, 40.7128)
LONDON = (-0.1276, 51.5072)


def test_domestic_points_get_amap():
    for lon, lat in (BEIJING, SHENZHEN):
        source = basemap_for(lon, lat)
        assert source.key.startswith("amap")
        assert source.datum == "GCJ-02"


def test_overseas_points_get_osm():
    for lon, lat in (NEW_YORK, LONDON):
        source = basemap_for(lon, lat)
        assert source.key.startswith("carto")
        assert source.datum == "WGS-84"


def test_the_two_sources_disagree_on_datum_and_that_is_visible():
    """国内与境外底图的坐标系不同——这个差异必须显式带出，不能靠约定。

    混用的后果是点位偏几百米且看起来正常，所以判据是「两者 datum
    不同且都写出来了」，而不是「都是字符串」。
    """
    assert basemap_for(*BEIJING).datum != basemap_for(*NEW_YORK).datum
    assert basemap_for(*BEIJING).datum
    assert basemap_for(*NEW_YORK).datum


def test_every_source_carries_attribution():
    """新增底图源必须自带版权署名——不署名就是侵权，不是可选项。"""
    for source in ALL_SOURCES:
        assert source.attribution.strip(), f"{source.key} 没有署名"
        assert "©" in source.attribution, f"{source.key} 署名格式不对"


def test_every_source_url_is_https():
    for source in ALL_SOURCES:
        assert source.url_template.startswith("https://"), source.key


def test_tile_url_fills_all_placeholders():
    url = tile_url(AMAP_VECTOR, z=12, x=3370, y=1556)
    assert "{x}" not in url and "{y}" not in url and "{z}" not in url
    assert "x=3370" in url and "y=1556" in url and "z=12" in url


def test_tile_url_subdomain_is_deterministic():
    """同一块瓦片永远走同一个子域——否则端侧缓存被撕成四份。"""
    first = tile_url(AMAP_VECTOR, z=12, x=3370, y=1556)
    second = tile_url(AMAP_VECTOR, z=12, x=3370, y=1556)
    assert first == second
    assert "webrd0" in first
    # 不同瓦片可以落在不同子域，但必须是声明过的那几个之一
    for x in range(3370, 3375):
        url = tile_url(AMAP_VECTOR, z=12, x=x, y=1556)
        assert any(f"webrd0{s}." in url for s in AMAP_VECTOR.subdomains)


def test_tile_url_refuses_out_of_range_input():
    with pytest.raises(ValueError, match="非负整数"):
        tile_url(AMAP_VECTOR, z=12, x=-1, y=1556)
    with pytest.raises(ValueError, match="最大缩放级别"):
        tile_url(AMAP_VECTOR, z=25, x=1, y=1)


def test_coverage_bbox_is_a_rectangle_not_a_border():
    """覆盖判定是矩形近似——文档里写明了，测试把它钉住防止被当成国界。"""
    west, south, east, north = AMAP_COVERAGE_BBOX
    assert in_amap_coverage(west, south) is True
    assert in_amap_coverage(east, north) is True
    assert in_amap_coverage(west - 1.0, south) is False
    # 矩形内的境外邻国小块也会被判成「国内」——这是已知代价，不是 bug
    assert in_amap_coverage(130.0, 42.0) is True


def test_tile_bbox_matches_known_values():
    """z=0 的整块瓦片应当覆盖全球。"""
    west, south, east, north = tile_bbox(0, 0, 0)
    assert west == pytest.approx(-180.0)
    assert east == pytest.approx(180.0)
    assert north == pytest.approx(85.0511, abs=0.01)
    assert south == pytest.approx(-85.0511, abs=0.01)


def test_beijing_tile_bbox_is_inside_coverage():
    west, south, east, north = tile_bbox(12, 3370, 1556)
    center = ((west + east) / 2, (south + north) / 2)
    assert in_amap_coverage(*center)


def test_source_payload_is_serialisable():
    payload: dict = AMAP_VECTOR.as_payload()
    assert payload["datum"] == "GCJ-02"
    assert isinstance(payload["subdomains"], list)


def test_custom_style_can_be_injected():
    """国内想看卫星图时可以替换——底图不是写死一个。"""
    source = basemap_for(*BEIJING, domestic=AMAP_SATELLITE)
    assert source.key == "amap-satellite"
    assert "style=6" in tile_url(source, z=10, x=843, y=389)


def test_unknown_source_key_is_not_silently_defaulted():
    """源必须来自声明过的集合——拼错 key 不该悄悄拿到另一个底图。"""
    from ai_butler.verticals.tour import basemap as module
    assert set(module._BY_KEY) == {s.key for s in ALL_SOURCES}


def test_tilesource_is_frozen():
    """源是常量，不该被运行期改歪。"""
    with pytest.raises(Exception):
        AMAP_VECTOR.datum = "WGS-84"                   # type: ignore[misc]
