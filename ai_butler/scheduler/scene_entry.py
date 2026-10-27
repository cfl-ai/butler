"""P3 主状态机的生产入口（U111）。

问题
----
`SceneScheduler` 此前全项目只在 `scripts/smoke_p1_p4.py` 与 `tests/` 里被构造，
`app.py` 只装配了意图路由给聊天链路 —— 「L4 命中 → 垂类执行」这条链**不是线上链路**：
可运行、有守护、端到端验证过，但没有入口触发它（E-86 的完整形态）。

本模块把「对话入口的一句话」接到主状态机上，并把垂类结果翻成一句人能读的答案。
它只做三件事，不做第四件：

1. **入口闸**：只有出现旅游核心词（`CORE_SIGNALS`）才交给垂类。
   为什么必须有一道闸：L4 的关键词打分里「酒店」「门票」权重也高，
   「帮我订杭州的酒店」会命中旅游垂类并被拉去生成行程 —— 那是交易意图，
   不是行程规划。闸门放在**入口**而不是改 L4 的打分表：打分表是 U110 的既有结论，
   改它会让既有的路由守护全部重算基线。
2. **参数抽取**：`extract_trip_params` 把「杭州两日游」里的目的地与天数抽出来，
   抽不到的留给垂类按表 12 询问用户。
3. **结果呈现**：把 `tour_result` 翻成纯文本（无 Markdown）+ 来源清单。

失败口径
--------
- 非垂类命中 / 无垂类执行者 → 返回 None，**安静退回**既有链路（不抢答）。
- 垂类命中但执行失败 → 返回带 `note` 的空文本，由调用方写进 notes 后继续既有链路。
  不把「垂类没跑起来」伪装成「已由垂类回答」，也不静默吞掉原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ai_butler.verticals.tour.utterance import extract_trip_params

#: 旅游核心词：出现这些才认为用户是在说「行程/游玩」，而不是在订酒店或买门票。
CORE_SIGNALS: tuple[str, ...] = (
    "旅游", "旅行", "行程", "攻略", "景点", "游玩", "出游", "度假",
    "自驾", "跟团", "自由行", "一日游", "两日游", "三日游", "几日游",
    "怎么玩", "去哪玩", "玩几天", "路线推荐", "行程规划", "规划一下",
)

#: 垂类结果里的采集类别 → 展示用来源名（后端给，前端不自己拼）
SOURCE_LABELS: dict[str, str] = {
    "weather": "Open-Meteo 气象数据",
    "scenic": "高德地图 POI",
    "traffic": "交通路况（本机无免费源，已降级）",
}

#: 行程必填参数：缺一个垂类就会问，不会硬跑（表 11 步骤 1）
REQUIRED_PARAMS: tuple[str, ...] = ("destination", "start_date", "days", "party_size")


@dataclass
class SceneAnswer:
    """主状态机给出的回答（或一句如实说明）。"""

    text: str
    sources: list[dict[str, str]] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    #: 非空表示「这次没答出来，原因是它」—— 调用方要把它写进 notes
    note: str = ""

    @property
    def has_answer(self) -> bool:
        return bool(self.text.strip())


def has_core_signal(utterance: str) -> bool:
    """这句话像不像在说「帮我规划一趟行程」。"""
    text = utterance or ""
    return any(signal in text for signal in CORE_SIGNALS)


def _route_basis_text(basis: Any) -> str:
    value = str(basis or "")
    if value.startswith("osrm"):
        mode = value.split(":", 1)[1] if ":" in value else ""
        name = {"car": "驾车", "bike": "骑行", "foot": "步行", "transit": "公交"}.get(mode, mode)
        return f"OSRM 真实路网（{name or '默认方式'}）"
    return "直线距离估算（这一段没有走真实路网，时间仅供参考）"


def _issue_text(issue: Any) -> str:
    if isinstance(issue, Mapping):
        return str(issue.get("note") or issue.get("text") or issue)
    return str(issue)


def render_tour_answer(result: Mapping[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """`tour_result` → （纯文本答案, 来源清单）。

    只复述结构化字段，**不补任何原结果里没有的内容**：行程点、钟点、门票都来自
    采集与规划结果，缺了就写「未取到」，不拿常识替它补一个景点。
    """
    if not result.get("handled"):
        ask = str(result.get("ask") or "").strip()
        if ask:
            # 表 12：参数缺失属本职范围内的失败 → 主动问用户补齐
            return ask, []
        note = str(result.get("note") or "").strip() or "这次没能生成行程方案"
        lines = [note]
        for failure in result.get("failures") or ():
            lines.append(f"未取到：{_issue_text(failure)}")
        return "\n".join(lines), []

    plan = dict(result.get("plan") or {})
    clock = dict(result.get("time") or {})
    route = dict(result.get("route") or {})
    lines: list[str] = []

    head = str(plan.get("destination") or "行程方案")
    if plan.get("days"):
        head += f" {plan['days']} 天行程"
    if plan.get("start_date") or plan.get("party_size"):
        head += "（" + "、".join(part for part in (
            f"{plan.get('start_date')} 起" if plan.get("start_date") else "",
            f"{plan.get('party_size')} 人" if plan.get("party_size") else "") if part) + "）"
    lines.append(head)

    depart = clock.get("depart") or plan.get("depart")
    end = clock.get("end") or plan.get("end")
    assumed = clock.get("depart_assumed", plan.get("depart_assumed", False))
    when = f"时间：{depart} 出发"
    when += "（你没给出发时刻，这是假定值）" if assumed else ""
    if end:
        when += f"，预计 {end} 结束"
    lines.append(when)

    timeline = list(clock.get("timeline") or plan.get("timeline") or [])
    if timeline:
        for index, row in enumerate(timeline, 1):
            item = dict(row or {})
            seg = f"{index}. {item.get('arrive') or '--:--'} 到 {item.get('name') or '未命名地点'}"
            if item.get("dwell_minutes"):
                seg += f"，停留 {item['dwell_minutes']} 分钟"
            if item.get("travel_minutes"):
                seg += f"（上一段路上 {item['travel_minutes']} 分钟"
                seg += "，估计值）" if item.get("travel_estimated") else "）"
            lines.append(seg)
    else:
        lines.append("逐站钟点未取到（候选景点为空或都缺坐标）")

    total = plan.get("total_cost")
    if total:
        lines.append(f"门票合计约 {float(total):.0f} 元")
    lines.append(f"路线依据：{_route_basis_text(route.get('basis'))}")
    degraded_from = str(route.get("degraded_from") or "").strip()
    if degraded_from:
        # 公交查不到就按驾车估：必须说出来，否则用户以为这是公交方案
        lines.append(f"注意：{degraded_from} 路线查询不可用，以上通行时间按替代方式估算")

    issues = list(result.get("issues") or plan.get("issues") or [])
    for issue in issues[:5]:
        lines.append(f"注意：{_issue_text(issue)}")

    sources: list[dict[str, str]] = []
    for page in result.get("source_pages") or ():
        kind = str((page or {}).get("kind") or "")
        label = SOURCE_LABELS.get(kind)
        if label and all(item["label"] != label for item in sources):
            sources.append({"label": label, "kind": "vertical", "source": kind})
    if not timeline:
        sources.append({"label": "旅游垂类（P3 主状态机）", "kind": "vertical",
                        "source": "tour"})
    return "\n".join(lines), sources


def _detail(*, utterance: str, intent: str, result: Any, params: Mapping[str, Any],
            sources: Sequence[Mapping[str, str]], ok: bool, summary: str) -> dict[str, Any]:
    """过程面板数据：与 function calling 的 `units` 同形，前端只认一种结构。"""
    return {
        "scene": {
            "intent": intent,
            "layer": getattr(result, "layer", ""),
            "confidence": getattr(result, "confidence", 0.0),
            "params": dict(params),
            "dispatched": list(getattr(result, "dispatched", []) or []),
            "degradation": list(getattr(result, "degradation", []) or []),
        },
        "units": [{
            "text": utterance,
            "agent": f"vertical:{intent}",
            "slot_kind": "scene",
            "tools": [f"P3 主状态机 → {intent}"],
            "tool_calls": 1,
            "rounds": 1,
            "calls": [{
                "tool": f"vertical:{intent}",
                "args": dict(params),
                "ok": ok,
                "empty": not ok,
                "note": "" if ok else "垂类未产出可用行程",
                "result": summary,
            }],
            "degradation": list(getattr(result, "degradation", []) or []),
            "assignment_fallback": False,
        }],
        "sources": [dict(item) for item in sources],
    }


async def answer_via_scene(*, scheduler: Any, user_id: str, session_id: str,
                           utterance: str, labels: Sequence[str] = (),
                           poi_search: Callable[[str], list[dict[str, Any]]] | None = None,
                           ) -> SceneAnswer | None:
    """走一次主状态机。返回 None = 这一轴不该接管，调用方继续既有链路。"""
    if scheduler is None:
        return None
    verticals = getattr(scheduler, "verticals", None)
    if verticals is None:                       # 没装垂类派发器 = 这一轴不存在
        return None
    if not has_core_signal(utterance):
        return None

    params = extract_trip_params(utterance)
    extra_state: dict[str, Any] = {}
    if params:
        extra_state["task_params"] = dict(params)

    # 参数齐了才需要候选景点：缺参数时垂类先问，问完再取点（省一次外部调用）
    if poi_search is not None and all(params.get(key) for key in REQUIRED_PARAMS):
        try:
            stops = poi_search(str(params["destination"])) or []
        except Exception as exc:                # noqa: BLE001 —— 取点失败不阻断「先问参数」
            stops = []
            extra_state.setdefault("scene_notes", [])
            extra_state["scene_notes"].append(
                f"景点候选检索失败（{type(exc).__name__}）：{exc}")
        if stops:
            extra_state["candidate_stops"] = list(stops)

    result = await scheduler.run(
        user_id=user_id, session_id=session_id or "scene",
        utterance=utterance, labels=tuple(labels) or verticals.keys(),
        extra_state=extra_state or None)

    intent = str(result.intent or "")
    if not intent or not verticals.has(intent):
        return None                             # 不是垂类命中：不抢答

    payload = (result.agent_results or {}).get(f"vertical:{intent}")
    if isinstance(payload, Mapping):
        # 子 Agent 返回的是**状态更新字典**（`{"tour_result": {...}}`），不是结果本身。
        # 直接读 `handled` 会永远读不到（实测：一切正常却渲染成「没能生成行程」）。
        payload = payload.get("tour_result", payload)
    if not isinstance(payload, Mapping):
        return SceneAnswer(
            text="",
            note=(f"主状态机命中 {intent} 但没有产出结果："
                  + "；".join(result.degradation[:3]) or "无降级明细"),
            detail=_detail(utterance=utterance, intent=intent, result=result,
                           params=params, sources=(), ok=False, summary="无结果"))

    text, sources = render_tour_answer(payload)
    ok = bool(payload.get("handled"))
    summary = (f"{payload.get('plan', {}).get('days', '?')} 天 / "
               f"{len(payload.get('plan', {}).get('stops') or [])} 个停留点")
    detail = _detail(utterance=utterance, intent=intent, result=result,
                     params=params, sources=sources, ok=ok, summary=summary)
    notes = list(extra_state.get("scene_notes") or ())
    return SceneAnswer(text=text, sources=sources, detail=detail,
                       note="；".join(notes))


__all__ = [
    "CORE_SIGNALS",
    "REQUIRED_PARAMS",
    "SOURCE_LABELS",
    "SceneAnswer",
    "answer_via_scene",
    "has_core_signal",
    "render_tour_answer",
]
