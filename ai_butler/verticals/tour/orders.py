"""费用汇总与待支付清单（表 11 步骤5）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ai_butler.verticals.tour.models import CostItem, TripPlan


class OrderError(ValueError):
    pass


def build_cost_items(plan: TripPlan, *, hotel_per_night: float = 0.0,
                     nights: int = 0) -> list[CostItem]:
    """按人数与票价汇总门票，加上住宿预估。"""
    people = plan.request.party_size
    items = [CostItem(name=f"{s.name}门票", amount=round(s.ticket_price * people, 2))
             for s in plan.stops if s.ticket_price > 0]
    if hotel_per_night > 0 and nights > 0:
        items.append(CostItem(name=f"住宿 {nights} 晚",
                              amount=round(hotel_per_night * nights, 2),
                              kind="hotel"))
    return items


def build_payment_list(plan: TripPlan, *, user_id: str,
                       merge: bool = True) -> dict[str, Any]:
    """产出**待支付清单**（描述），交管家中转给支付模块。

    **不执行付款**；~{merge}~ 对应「多笔账单合并一键支付」，
    但无论合并与否都必须经用户人工确认（表 11）。
    """
    if not user_id:
        raise OrderError("支付清单必须强绑定 user_id（P8 表注）")

    items = list(plan.costs)
    total = round(sum(i.amount for i in items), 2)
    payload: dict[str, Any] = {
        "user_id": user_id,
        "destination": plan.request.destination,
        "merge": merge,
        "total": total,
        "items": [{"name": i.name, "amount": i.amount, "kind": i.kind} for i in items],
        # ---- 三处必须显式声明的边界 ----
        "requires_manual_confirmation": True,
        "executed": False,
        "relay_via": "butler_agent",
        "note": "旅游 Agent 仅生成待付费清单，无权直接执行付款（表 11）",
    }
    if merge:
        payload["merged_count"] = len(items)
    return payload
