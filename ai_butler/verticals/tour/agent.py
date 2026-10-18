"""旅游 Agent —— 第一个垂类样板（裁定 U39）。

计划书依据
----------
- 五、第二阶段表 11：模块定位、六项✅职责、三项❌职责、回退规则
- 表 11 步骤1–6：任务解析 → 采集 → 方案生成 → 动态更新 → 费用汇总 → 结果输出与归档
- 表 11 模块边界：**无权修改路由规则、无权分发任务给其他 Agent**；
  跨模块请求必须经管家中转；回退必须原路返回路由中枢

边界是靠**结构**保证的，不是靠自觉：本 Agent 不持有任何路由/分发能力，
跨模块只能产出 ~{out_of_scope_request}~ 描述。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.capabilities import CAP_DATA_READ, CAP_JEV, CAP_WIKI_READ, CAP_WIKI_WRITE
from ai_butler.verticals.base import (
    FAILURE_COLLECT_FAILED,
    FAILURE_MISSING_PARAM,
    FAILURE_OUT_OF_SCOPE,
    FAILURE_PLAN_FAILED,
    DutyBoundary,
    decide_fallback,
    parse_params,
)
from ai_butler.verticals.tour.models import (
    TRIP_PARAM_SPECS,
    TripPlan,
    TripRequest,
    Stop,
)
from ai_butler.verticals.tour.orders import build_cost_items, build_payment_list
from ai_butler.verticals.tour.replan import ReplanSignals, evaluate_preconditions
from ai_butler.verticals.tour.routing import ISSUE_KINDS, plan_route
from ai_butler.verticals.tour.sources import (
    SOURCE_KINDS,
    CollectedFact,
    collect_all,
    to_claim_fact,
    to_source_page,
)
from ai_butler.verticals.base import VerticalAgent

#: 表 11 的 ✅ 清单
IN_SCOPE: tuple[str, ...] = (
    "trip_plan",          # 用户行程规划
    "realtime_query",     # 实时信息查询
    "trip_update",        # 行程动态更新
    "plan_output",        # 行程方案输出
    "order_assist",       # 行程订单辅助
    "archive",            # 行程资料汇总
)

#: 表 11 的 ❌ 清单
OUT_OF_SCOPE: tuple[str, ...] = (
    "refund",             # 退款
    "payment_confirm",    # 支付确认
    "after_sales",        # 订单售后
    "food_recommend",     # 点餐推荐
    "food_order",         # 餐饮下单
    "system_command",     # 通用系统指令（唤醒、清空对话等）
)

#: ❌ 清单的去向提示（**仅提示**；垂类不得自行转交）
HANDOFF: Mapping[str, str] = {
    "refund": "p8_payment", "payment_confirm": "p8_payment", "after_sales": "p8_payment",
    "food_recommend": "food_agent", "food_order": "food_agent",
    "system_command": "p3_scene_scheduler",
}

TOUR_BOUNDARY = DutyBoundary(vertical="tour", in_scope=IN_SCOPE,
                             out_of_scope=OUT_OF_SCOPE, handoff_hint=HANDOFF)


@dataclass
class TourAgent(VerticalAgent):
    """旅游垂类业务执行单元。"""

    name: ClassVar[str] = "tour_agent"
    writes: ClassVar[tuple[str, ...]] = ("tour_result",)
    capabilities: ClassVar[tuple[str, ...]] = (
        CAP_DATA_READ, CAP_WIKI_READ, CAP_WIKI_WRITE, CAP_JEV)
    boundary: ClassVar[DutyBoundary] = TOUR_BOUNDARY

    #: 采集端口（U37；由运行时注入，不在本模块内置网络调用）
    collector: Any = None
    #: 路线优化器（U38；不注入则用内置贪心最近邻）
    optimizer: Any = None

    def _stops(self, state: Mapping[str, Any]) -> list[Stop]:
        out: list[Stop] = []
        for row in (state.get("candidate_stops") or []):
            row = row or {}
            out.append(Stop(
                stop_id=str(row.get("stop_id") or ""),
                name=str(row.get("name") or ""),
                open_minute=row.get("open_minute"),
                close_minute=row.get("close_minute"),
                dwell_minutes=int(row.get("dwell_minutes") or 60),
                ticket_price=float(row.get("ticket_price") or 0.0),
                latitude=row.get("latitude"),
                longitude=row.get("longitude"),
                evidence_ids=tuple(row.get("evidence_ids") or ()),
                low_confidence=bool(row.get("low_confidence")),
                closed=bool(row.get("closed"))))
        return out

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        task_kind = str(state.get("task_kind") or "trip_plan")

        # ---- 职责边界自检（表 11：接收任务后自检业务域匹配度）----
        scope = self.declare_scope(task_kind)
        if scope == FAILURE_OUT_OF_SCOPE:
            decision = decide_fallback(
                failure=FAILURE_OUT_OF_SCOPE,
                rollbacks_used=int(state.get("rollbacks_used") or 0))
            return {"tour_result": {
                "user_id": uid, "task_kind": task_kind, "handled": False,
                "fallback": decision.as_dict(),
                "handoff_hint": self.boundary.handoff_hint.get(task_kind, ""),
                "note": "超出旅游业务域，**原路返回路由中枢**，不自行转交（表 11 模块边界）"}}

        # ---- 步骤1：任务解析与参数提取（缺失即询问，**禁止回退**）----
        params = parse_params(dict(state.get("task_params") or {}), TRIP_PARAM_SPECS)
        if not params.complete:
            decision = decide_fallback(failure=FAILURE_MISSING_PARAM)
            return {"tour_result": {
                "user_id": uid, "task_kind": task_kind, "handled": False,
                "fallback": decision.as_dict(),
                "ask": params.ask_text(),
                "note": "参数缺失属本职范围内失败，询问用户补齐而非回退（表 12）"}}

        request = TripRequest(
            destination=params.values["destination"],
            start_date=str(params.values["start_date"]),
            days=int(params.values["days"]),
            party_size=int(params.values["party_size"]),
            budget=(float(params.values["budget"]) if params.values.get("budget") else None),
            transport=str(params.values.get("transport") or "transit"),
            preferences=tuple(params.values.get("preferences") or ()),
            taboos=tuple(params.values.get("taboos") or ()))

        # ---- 步骤2：调用统一采集底座（失败即重试，**禁止回退**）----
        facts: list[CollectedFact] = []
        if self.collector is not None:
            collected = collect_all(self.collector, subject=request.destination,
                                    kinds=tuple(state.get("collect_kinds") or SOURCE_KINDS))
            if not collected.ok:
                decision = decide_fallback(failure=FAILURE_COLLECT_FAILED)
                return {"tour_result": {
                    "user_id": uid, "task_kind": task_kind, "handled": False,
                    "fallback": decision.as_dict(),
                    "failures": collected.failures,
                    "note": "采集失败属本职范围内失败，重试而非回退（表 12）"}}
            facts = collected.facts

        # ---- 步骤3：行程方案生成 + 内置校验 ----
        stops = self._stops(state)
        if not stops:
            decision = decide_fallback(failure=FAILURE_PLAN_FAILED)
            return {"tour_result": {
                "user_id": uid, "task_kind": task_kind, "handled": False,
                "fallback": decision.as_dict(),
                "note": "方案生成失败属本职范围内失败，降级提示而非回退（表 12）"}}

        ordered, issues = plan_route(stops, optimizer=self.optimizer)
        plan = TripPlan(request=request, stops=ordered, issues=issues)

        # ---- 步骤4：动态监测与重规划条件判定（U40）----
        signals = ReplanSignals(**dict(state.get("replan_signals") or {}))
        replan = evaluate_preconditions(signals)

        # ---- 步骤5：订单 & 费用汇总（**不执行付款**）----
        plan.costs = build_cost_items(
            plan, hotel_per_night=float(state.get("hotel_per_night") or 0.0),
            nights=int(state.get("nights") or 0))
        payment_list = build_payment_list(
            plan, user_id=uid, merge=bool(state.get("merge_bills", True)))

        # ---- 采集结果落公共 Workspace（source 页 + claim 绑证据）----
        source_pages: list[dict[str, Any]] = []
        for fact in facts:
            page = to_source_page(fact, now=str(state.get("now") or ""))
            source_pages.append({
                "page_id": page.id, "kind": fact.kind, "provider": fact.provider,
                "claim": to_claim_fact(fact, subject_id=request.destination,
                                       claim_page_id=f"claim-{page.id}")})

        return {"tour_result": {
            "user_id": uid, "task_kind": task_kind, "handled": True,
            "plan": plan.as_summary(),
            "issues": issues,
            "low_confidence": [s.name for s in ordered if s.low_confidence],
            "replan": replan.as_dict(),
            "payment_list": payment_list,
            "source_pages": source_pages,
            "synthesis": {
                "scope": "vault", "page_type": "synthesis",
                "user_id": uid,
                "note": "行程方案写入**本人私有 Vault** 的 synthesis 综合摘要页面（表 11）"},
            "boundary": {"in_scope": list(IN_SCOPE), "out_of_scope": list(OUT_OF_SCOPE)},
        }}
