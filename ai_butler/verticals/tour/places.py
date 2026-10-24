"""地点库：**示例数据** + 可选的外部覆盖（U71）。

必须说清楚的一件事
------------------
本仓库**没有接入任何 POI 数据源**（高德/百度/腾讯的 POI 检索都要 key，本机没有）。
所以这里的 `SEED_PLACES` 是**手写的示例数据**，只为把「按喜好 / 按所在地推荐」
这条链路走通、并让测试与界面上有东西可看。

因此 `load_places()` 的返回里永远带 `source`：

* `"seed-demo"` —— 示例数据，**不能当作真实地点使用**
* `"file"` —— 由部署方提供的 `places.json`（真实数据接入点）

界面据此显示「示例数据」角标。把示例数据当真实推荐展示，比没有推荐更糟。
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_butler.core.geo import GeoError, Place

#: 示例地点（北京城中心一小片，坐标 WGS84）。**仅供演示与测试**。
SEED_PLACES: tuple[Place, ...] = (
    Place("seed-1", "胡同里的独立书店", ("书店", "安静", "独处"),
          39.9142, 116.4040, "示例：二层有旧书与长桌"),
    Place("seed-2", "河边的手冲咖啡", ("咖啡", "安静"),
          39.9075, 116.3960, "示例：窗边能看到水面"),
    Place("seed-3", "夜里才开的面馆", ("夜宵", "面食"),
          39.9200, 116.4100, "示例：凌晨两点还在营业"),
    Place("seed-4", "城楼下的公园", ("散步", "户外", "亲子"),
          39.9180, 116.3990, "示例：傍晚人多，早上安静"),
    Place("seed-5", "老厂房改的展馆", ("看展", "摄影", "安静"),
          39.9350, 116.4200, "示例：常设展免费"),
    Place("seed-6", "菜市场二楼的小馆", ("吃饭", "本地", "性价比"),
          39.9030, 116.3900, "示例：只在午市开门"),
    Place("seed-7", "图书馆自习区", ("学习", "安静", "独处"),
          39.9260, 116.3880, "示例：需刷身份证进"),
    Place("seed-8", "能带狗的草坪", ("户外", "宠物", "散步"),
          39.9400, 116.3950, "示例：有围栏，可放开绳"),
)


def load_places(root: Path | None = None) -> tuple[list[Place], str]:
    """返回 `(地点列表, 数据来源)`。

    有 `places.json` 就用它（真实数据接入点）；否则用示例数据。
    解析失败**不静默退回示例** —— 那样部署方会以为自己的数据生效了。
    """
    if root is not None:
        path = Path(root) / "places.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise GeoError("places_unreadable", f"places.json 无法解析：{exc}") from exc
            items: list[Place] = []
            for entry in raw if isinstance(raw, list) else raw.get("places", []):
                try:
                    items.append(Place(
                        place_id=str(entry["place_id"]),
                        name=str(entry["name"]),
                        tags=tuple(str(t) for t in entry.get("tags", [])),
                        lat=float(entry["lat"]) if entry.get("lat") is not None else None,
                        lon=float(entry["lon"]) if entry.get("lon") is not None else None,
                        description=str(entry.get("description", "")),
                    ))
                except (KeyError, TypeError, ValueError) as exc:
                    raise GeoError(
                        "places_malformed", f"places.json 条目缺少必要字段：{exc}"
                    ) from exc
            return items, "file"
    return list(SEED_PLACES), "seed-demo"
