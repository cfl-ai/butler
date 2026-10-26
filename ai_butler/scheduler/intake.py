"""用户输入离散化（有状态 → 无状态）与 Agent / 工具调配（U113）。

要解决的问题
------------
function calling 拿到的输入常常是**有状态的**：

- 「那里天气怎么样」——「那里」是哪儿？得看上一轮。
- 「顺便也订个酒店」——这一句含两件事，且第二件事的地儿还是上一轮的。

把这种带隐含状态的原句原样丢给模型，模型只能猜；猜错的代价是调错工具、传错参数。
所以本模块在模型之前加一层：**先把输入变成一组自包含的离散单元**，
再判断每个单元是什么类型的信息，最后按类型调配 Agent 与工具。

四步管线
--------
```text
原句（有状态）
  → ① 去状态化：指代消解（"那里" → 杭州），并留痕说明它原本是个指代
  → ② 离散化：按并列 / 转折连接词拆成多个 InformationUnit（多意图拆分）
  → ③ 类型判定：信息类型（槽位种类）+ 任务类型（**复用已有 QueryCategory**）
  → ④ 调配：(任务类型, 信息类型) → (Agent, 工具能力)，查表；表上没有就不派发
```

关于「无状态」的精确含义
------------------------
离散化后的每个单元**脱离会话也能被独立理解** —— 槽位值已经补全进单元里，
下游（模型、Agent、工具）不需要再去翻会话历史。这不是「丢弃上下文」，
而是**把上下文消费在这一层，之后不再依赖它**。

两个刻意不做的事
----------------
1. **不假装能做实体识别**：本模块只做可靠的指代消解与规则型类型判定，
   不猜测「杭州西湖」属于哪个实体 —— 那是 NER 的活，没有就如实留空槽位。
2. **不凭空造工具名**：调配结果给的是**工具能力**（`CAP_*`，真值源是
   `agents.capabilities`），具体工具由 ToolRegistry 按能力装配。
   在调配表里写死工具名，等于编造一层不存在的事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any, Mapping, Sequence

from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_JEV,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CAP_WIKI_WRITE,
)
from ai_butler.core.config import get_settings
from ai_butler.infra.llm_gateway import ChatMessage
from ai_butler.retrieval.planner import QueryCategory, classify


#: 合法的任务类型取值（`QueryCategory`）。**不要把非本集合的标签当分类用** ——
#: 调度器的意图标签是分派名，硬塞进来会让 `ASSIGNMENTS` 全部查空。
_CATEGORY_VALUES = frozenset(c.value for c in QueryCategory)


# --------------------------------------------------------------------------- #
# 信息类型：一个单元携带的是**哪一种信息**
# --------------------------------------------------------------------------- #
class SlotKind(str, Enum):
    """信息类型（槽位种类）。

    与 `QueryCategory` 正交：那一轴是「查询形态」（决定工具序列），
    这一轴是「信息种类」（决定由谁处理、要不要碰私有数据）。
    """

    LOCATION = "location"        # 地点
    TIME = "time"                # 时间 / 日期
    ENTITY_ID = "entity_id"      # 精确标识：订单号 / 编号 / 状态码
    QUANTITY = "quantity"        # 数量 / 金额 / 天数
    PREFERENCE = "preference"    # 偏好 / 禁忌
    TOPIC = "topic"              # 普通主题对象
    PRIVATE = "private"          # 涉及用户私有数据（我上次 / 我的 / 之前说的）
    NONE = "none"                # 判不出来


#: 指代 → 需要的槽位。**消解必须靠上下文值，不许猜**。
_PRONOUN_SLOTS: dict[str, tuple[SlotKind, str]] = {
    "那里": (SlotKind.LOCATION, "location"), "那边": (SlotKind.LOCATION, "location"),
    "那儿": (SlotKind.LOCATION, "location"), "当地": (SlotKind.LOCATION, "location"),
    "那个地方": (SlotKind.LOCATION, "location"), "目的地": (SlotKind.LOCATION, "location"),
    "这个": (SlotKind.TOPIC, "topic"), "那个": (SlotKind.TOPIC, "topic"),
    "它": (SlotKind.TOPIC, "topic"), "这款": (SlotKind.TOPIC, "topic"),
    "上次": (SlotKind.PRIVATE, "prior"), "上一次": (SlotKind.PRIVATE, "prior"),
    "之前": (SlotKind.PRIVATE, "prior"), "我的": (SlotKind.PRIVATE, "prior"),
    "我之前": (SlotKind.PRIVATE, "prior"),
}

#: 相对日期 → 偏移天数。补全需要 `IntakeContext.today`；**没有 today 就不补全**，
#: 宁可留着「明天」两个字，也不按服务器当前时间偷偷代填（那会静默改变语义）。
_RELATIVE_DAYS: dict[str, int] = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3, "昨天": -1}

#: 离散化的切分信号（并列 / 转折 / 追加）。**短句不切**：先验是「切成有意义的几件事」，
#: 而不是「见到连接词就切」—— 过切会把一句话拆成无法独立理解的碎片。
_SPLIT_MARKERS: tuple[str, ...] = (
    "顺便", "另外", "还有", "并且", "同时", "以及", "然后", "再者", "除此之外", "再",
)
#: 低于该长度的片段不参与拆分（避免切出「再」这种单字噪音）
_MIN_UNIT_CHARS = 4


@dataclass(frozen=True)
class IntakeContext:
    """消解指代所依赖的会话上下文。

    **只在这一层被消费**：补全完成后，下游拿到的是自包含单元，不再需要它。
    全空 = 没有上下文可用，此时含指代的单元如实标 `unresolved`。
    """

    location: str = ""
    topic: str = ""
    prior: str = ""
    entity_id: str = ""
    #: 当天日期（ISO）。给了才敢把「明天」翻成具体日期。
    today: str = ""


@dataclass(frozen=True)
class InformationUnit:
    """一个离散的信息单元（**自包含**）。"""

    text: str                                  # 去状态化后的文本
    origin: str                                # 原始片段（便于追溯切了什么）
    slot_kind: SlotKind                        # 信息类型
    category: str                              # 任务类型（QueryCategory 的值）
    slots: Mapping[str, Any] = field(default_factory=dict)   # 补全出的槽位值
    resolved: tuple[str, ...] = ()             # 消解痕迹："那里→杭州"
    unresolved: tuple[str, ...] = ()           # 想消解但没上下文的指代
    confidence: float = 1.0

    @property
    def is_private(self) -> bool:
        return self.slot_kind == SlotKind.PRIVATE

    @property
    def self_contained(self) -> bool:
        """是否真正做到自包含（没有悬空指代）。"""
        return not self.unresolved


@dataclass(frozen=True)
class Assignment:
    """调配结论：这个单元交给谁、给它什么工具能力。"""

    agent: str                        # 阶段 Agent 名，或 `vertical:<key>` 形态的垂类
    capabilities: tuple[str, ...]     # 工具能力（真值源：agents.capabilities）
    note: str = ""

    @property
    def is_vertical(self) -> bool:
        return self.agent.startswith("vertical:")


# --------------------------------------------------------------------------- #
# 调配表：单一真值源。**表上没有的组合一律不派发**
# --------------------------------------------------------------------------- #
#: (任务类型, 信息类型) → (Agent, 工具能力)
#: 不给「大概归它管」留默认值：查不到就是查不到，宁可走兜底也不要猜着派。
ASSIGNMENTS: dict[tuple[str, str], Assignment] = {
    # 事实问答：按信息种类分派给不同的感知/规划单元
    (QueryCategory.FACT_QUESTION.value, SlotKind.LOCATION.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL, CAP_WIKI_READ), "地点类事实：检索权威素材"),
    (QueryCategory.FACT_QUESTION.value, SlotKind.TIME.value): Assignment(
        "time_progress_agent", (CAP_DATA_READ,), "时间类事实：读会话时间上下文"),
    (QueryCategory.FACT_QUESTION.value, SlotKind.ENTITY_ID.value): Assignment(
        "planner_agent", (CAP_DATA_READ, CAP_RETRIEVAL), "标识类查询：数据中台 + 检索"),
    (QueryCategory.FACT_QUESTION.value, SlotKind.QUANTITY.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL,), "数量/金额类：检索核实"),
    (QueryCategory.FACT_QUESTION.value, SlotKind.TOPIC.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL, CAP_WIKI_READ), "通用事实问答"),

    # 私有回忆：必须走能读本人数据的单元，且不写公共知识库
    (QueryCategory.PERSONAL_RECALL.value, SlotKind.PRIVATE.value): Assignment(
        "planner_agent", (CAP_DATA_READ, CAP_WIKI_READ),
        "私有回忆：只读本人数据，禁止写入公共 Workspace"),

    # 推荐：走规划单元（需要素材支撑）
    (QueryCategory.RECOMMENDATION.value, SlotKind.TOPIC.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL, CAP_WIKI_READ), "推荐方案：先检索再生成"),
    (QueryCategory.RECOMMENDATION.value, SlotKind.LOCATION.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL, CAP_WIKI_READ), "地点类推荐"),

    # 操作步骤 / 规则政策
    (QueryCategory.PROCEDURE.value, SlotKind.TOPIC.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL,), "操作步骤：检索规程"),
    (QueryCategory.POLICY_RULE.value, SlotKind.TOPIC.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL,), "规则政策：检索条款"),

    # 对比
    (QueryCategory.COMPARISON.value, SlotKind.TOPIC.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL,), "对比：检索双方素材"),

    # 标识查询：精确匹配，走数据中台而不是语义检索
    (QueryCategory.IDENTITY_LOOKUP.value, SlotKind.ENTITY_ID.value): Assignment(
        "planner_agent", (CAP_DATA_READ, CAP_RETRIEVAL), "标识查询：精确匹配优先"),
    (QueryCategory.IDENTITY_LOOKUP.value, SlotKind.TOPIC.value): Assignment(
        "planner_agent", (CAP_DATA_READ,), "标识查询：读本人数据"),

    # 感知类：交给 Mops 集群，且允许写回事实
    (QueryCategory.FACT_QUESTION.value, SlotKind.PREFERENCE.value): Assignment(
        "incremental_extraction_agent", (CAP_RETRIEVAL, CAP_WIKI_WRITE),
        "偏好：转成绑定证据的事实写入本人 Vault"),

    # 判不出来：按 QueryCategory.UNKNOWN 的既有约定「走最稳的一条路」——
    # 只给检索，不给写权限。**只兜这一种组合**：未知任务 + 未知信息类型，
    # 其余组合查不到就是查不到。
    (QueryCategory.UNKNOWN.value, SlotKind.NONE.value): Assignment(
        "planner_agent", (CAP_RETRIEVAL,), "判不出：检索兜底，不授予任何写能力"),
}


def assign(unit: InformationUnit) -> Assignment | None:
    """查调配表。**查不到返回 None**（= 不派发），不提供兜底猜测。"""
    return ASSIGNMENTS.get((unit.category, unit.slot_kind.value))


# --------------------------------------------------------------------------- #
# ① 去状态化
# --------------------------------------------------------------------------- #
def resolve_references(text: str, context: IntakeContext | None = None
                       ) -> tuple[str, tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    """把指代补成自包含值，返回 (补全后文本, 消解痕迹, 未解决指代, 槽位)。

    消解**留痕**：单元里带 `resolved`，谁被替换过一目了然 ——
    悄悄替换等于让下游无法判断这个值有多可信。
    """
    ctx = context or IntakeContext()
    resolved: list[str] = []
    unresolved: list[str] = []
    slots: dict[str, Any] = {}
    out = text

    for pronoun, (kind, slot_name) in _PRONOUN_SLOTS.items():
        if pronoun not in out:
            continue
        value = {"location": ctx.location, "topic": ctx.topic,
                 "prior": ctx.prior}.get(slot_name, "")
        if value:
            out = out.replace(pronoun, value)
            slots[slot_name] = value
            resolved.append(f"{pronoun}→{value}")
        else:
            # 上下文没有值：如实标未解决，绝不拿一个默认值顶上
            unresolved.append(pronoun)

    # 相对日期：只有给了 today 才翻成绝对日期
    for word, offset in _RELATIVE_DAYS.items():
        if word not in out:
            continue
        if not ctx.today:
            unresolved.append(word)
            continue
        try:
            base = date.fromisoformat(ctx.today)
        except ValueError:
            unresolved.append(word)
            continue
        concrete = (base + timedelta(days=offset)).isoformat()
        out = out.replace(word, concrete)
        slots["date"] = concrete
        resolved.append(f"{word}→{concrete}")

    return out, tuple(resolved), tuple(unresolved), slots


# --------------------------------------------------------------------------- #
# ② 离散化
# --------------------------------------------------------------------------- #
def split_units(text: str) -> tuple[str, ...]:
    """按并列/追加信号把一句拆成多件事。

    **先验是「切成有意义的几件事」，不是「见到连接词就切」**：
    过切会产生无法独立理解的碎片，那比不切更糟。所以太短的片段会被丢弃，
    且切完只剩一段时直接返回原句。
    """
    pieces: list[str] = [text]
    for marker in _SPLIT_MARKERS:
        nxt: list[str] = []
        for piece in pieces:
            parts = [p.strip() for p in piece.split(marker) if p.strip()]
            # 切出来的后续片段要有连接词才完整：补回去保持可读
            if len(parts) > 1:
                nxt.append(parts[0])
                nxt.extend(p for p in parts[1:])
            else:
                nxt.append(piece)
        pieces = nxt

    # 切完要清掉残留的连接标点：带着「天气怎么样，」这种尾巴去跑分类器，
    # 分类器认不出是正常的 —— 脏的是我们切出来的文本，不是分类器不行。
    kept = [p.strip(" ，,。;；、") for p in pieces]
    kept = [p for p in kept if len(p) >= _MIN_UNIT_CHARS]
    if len(kept) <= 1:
        return (text.strip(),)
    return tuple(kept)


# --------------------------------------------------------------------------- #
# ③ 类型判定
# --------------------------------------------------------------------------- #
#: 数量提示**不含单字**：「天」会把「天气」判成数量，「人」会把「人少」判成数量 ——
#: 单字提示在中文里几乎必然误伤，宁可漏判也不误判（误判会派错工具）。
_QUANTITY_HINTS = ("多少钱", "价格", "预算", "多少钱", "几天", "多少天", "多少人",
                   "几人", "元", "人均", "总价")
_ENTITY_HINTS = ("订单号", "编号", "单号", "工单", "流水号", "状态码", "错误码", "版本号")
_PRIVATE_HINTS = ("我上次", "我之前", "我说过", "我的", "之前让你")
_PREFERENCE_HINTS = ("喜欢", "偏好", "不吃", "忌", "想", "希望")
_LOCATION_HINTS = ("景区", "景点", "城市", "哪里", "哪儿", "天气", "路线", "附近")
_TIME_HINTS = ("几点", "什么时候", "日程", "行程", "时刻", "开放时间")


def detect_slot_kind(text: str) -> SlotKind:
    """信息类型判定（规则型，可解释；判不出来返回 NONE，不猜）。"""
    # 顺序是试出来的：先判「专属信号强」的（私有 / 标识），再判时间、地点，
    # 数量与偏好放最后 —— 它们的提示词最容易被别的类型顺带命中。
    if any(h in text for h in _PRIVATE_HINTS):
        return SlotKind.PRIVATE
    if any(h in text for h in _ENTITY_HINTS):
        return SlotKind.ENTITY_ID
    if any(h in text for h in _TIME_HINTS):
        return SlotKind.TIME
    if any(h in text for h in _LOCATION_HINTS):
        return SlotKind.LOCATION
    if any(h in text for h in _PREFERENCE_HINTS):
        return SlotKind.PREFERENCE
    if any(h in text for h in _QUANTITY_HINTS):
        return SlotKind.QUANTITY
    return SlotKind.NONE


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def discretize(utterance: str, *, context: IntakeContext | None = None,
               intent_label: str | None = None) -> tuple[InformationUnit, ...]:
    """把一次用户输入变成一组**自包含的离散单元**。

    `intent_label` 只在规则型分类落空时作为兜底（与 `planner.classify` 同口径：
    先确定性后概率性）。

    **但不是任何标签都能当兜底**：调度器给的意图标签是 `DispatchPlan` 的键
    （如 `"weather_q"`），那是「谁执行」的分派名，不是 `QueryCategory`（「怎么处理」）。
    把分派名当查询分类塞进来，`ASSIGNMENTS` 永远查不到 —— 表面「接通了」，
    实际每个单元都被记成 unassigned。所以只接受**合法的 QueryCategory 值**，
    其余一律不理，让分类器自己判。
    """
    text = (utterance or "").strip()
    if not text:
        return ()

    hint = intent_label if intent_label in _CATEGORY_VALUES else None
    units: list[InformationUnit] = []
    for piece in split_units(text):
        resolved_text, resolved, unresolved, slots = resolve_references(piece, context)
        kind = detect_slot_kind(resolved_text)
        # 任务类型**复用既有分类器**（E-44：另造一套必漂移）
        category, _reason, _signals = classify(resolved_text, intent_label=hint)
        if category is QueryCategory.UNKNOWN and hint:
            category_value = hint
        else:
            category_value = category.value
        # 判不出信息类型但判出了私有回忆 → 按私有处理（保守侧）
        if kind is SlotKind.NONE and category is QueryCategory.PERSONAL_RECALL:
            kind = SlotKind.PRIVATE
        units.append(InformationUnit(
            text=resolved_text, origin=piece, slot_kind=kind,
            category=category_value, slots=slots, resolved=resolved,
            unresolved=unresolved,
            confidence=0.6 if unresolved else 1.0))
    return tuple(units)


def plan_assignments(units: Sequence[InformationUnit]
                     ) -> tuple[tuple[InformationUnit, Assignment | None], ...]:
    """给每个单元配 Agent 与工具能力；配不上的单元给 None（调用方去走兜底）。"""
    return tuple((u, assign(u)) for u in units)


# --------------------------------------------------------------------------- #
# ⑤ 接通：离散单元 → 裁剪后的工具清单 → function calling 回路（U114）
# --------------------------------------------------------------------------- #
def build_tools_for(assignment: Assignment, registry: Any) -> list[dict[str, Any]]:
    """把调配结论落成**实际下发给模型**的工具清单。

    两道闸都必须过：
    1. 工具声明的能力 ∈ 调配给的能力集合（计划书三、P4：按职责授予，不是全量下发）；
    2. 工具写了参数契约（`exposed_to_model`）。

    **只下发该 Agent 职责内的工具** —— 若让模型看见全部工具，权限边界就从
    「按职责授予」退化成「模型自觉」，那是把护栏交给了概率。
    """
    from ai_butler.scheduler.tools import spec_as_openai_tool

    allowed = set(assignment.capabilities)
    return [spec_as_openai_tool(s) for s in registry.specs()
            if s.exposed_to_model and s.capability in allowed]


@dataclass
class UnitOutcome:
    """一个离散单元的处置结果。"""

    unit: InformationUnit
    assignment: Assignment | None = None
    tools: tuple[dict[str, Any], ...] = ()
    result: Any = None                 # ToolLoopResult（未走回路时为 None）
    #: 为什么没走回路（空 = 正常走到了回路）
    skipped: str = ""

    @property
    def ran(self) -> bool:
        return self.result is not None


@dataclass
class IntakeReport:
    outcomes: tuple[UnitOutcome, ...] = ()
    #: 整条接通路径未启用（开关关闭）。**如实标记**，不伪装成"处理了但没结果"。
    disabled: bool = False

    @property
    def ran_count(self) -> int:
        return sum(1 for o in self.outcomes if o.ran)

    @property
    def unassigned(self) -> tuple[InformationUnit, ...]:
        return tuple(o.unit for o in self.outcomes if o.skipped == "unassigned")

    def as_dict(self) -> dict[str, Any]:
        """落成可进 State 的普通字典。

        **只带结论，不带活对象**：进 State 的东西会被下游 Agent、聚合、账本看到，
        把 `ToolResult` / dataclass 原样塞进去等于让下游去猜内部结构。
        """
        units = []
        for o in self.outcomes:
            res = o.result
            units.append({
                "text": o.unit.text,
                "origin": o.unit.origin,
                "slot_kind": o.unit.slot_kind.value,
                "category": o.unit.category,
                "resolved": list(o.unit.resolved),
                "unresolved": list(o.unit.unresolved),
                "self_contained": o.unit.self_contained,
                "agent": o.assignment.agent if o.assignment else "",
                "tools": [t["function"]["name"] for t in o.tools],
                "skipped": o.skipped,
                "ran": o.ran,
                "answer": res.text if res else "",
                "rounds": res.rounds if res else 0,
                "truncated": bool(res.truncated) if res else False,
                "tool_calls": len(res.tool_results) if res else 0,
                "degradation": list(res.degradation) if res else [],
            })
        return {"disabled": self.disabled, "ran_count": self.ran_count,
                "units": units,
                "unassigned": [u.text for u in self.unassigned]}


async def run_units(*, gateway: Any, dispatcher: Any, registry: Any,
                    user_id: str, utterance: str,
                    context: IntakeContext | None = None,
                    settings: Any = None,
                    intent_label: str | None = None,
                    max_rounds: int | None = None) -> IntakeReport:
    """接通：输入 → 离散单元 → 裁剪工具 → 每个单元一条 function calling 回路。

    `intent_label` 是**五层意图路由（U74）已经给出的结论**。本层不取代路由：
    路由负责「这是什么任务」，离散化负责「拆开、补全指代、按类型调配」。
    两者协作才对 —— 让离散化去重复猜一次意图，等于把概率结论覆盖确定性结论。

    **每个单元单独起一条回路**：单元之间是并列关系（「查天气」+「订酒店」），
    不该共用同一轮对话上下文 —— 否则前一个单元的中间结论会污染下一个单元的判断（E-88）。

    **开关关闭时返回 disabled=True 且不消费任何模型调用**：
    行为与接通前逐字一致，调用方据此走既有路径。
    """
    from ai_butler.scheduler.tool_loop import FunctionCallLoop, mention_tools_available

    cfg = settings or get_settings()
    if not getattr(cfg, "function_calling_enable", False):
        return IntakeReport(disabled=True)

    units = discretize(utterance, context=context, intent_label=intent_label)
    outcomes: list[UnitOutcome] = []
    for unit in units:
        assignment = assign(unit)
        if assignment is None:
            # 调配表里没有：如实记下，交给既有兜底 —— 不硬塞给模型瞎调
            outcomes.append(UnitOutcome(unit=unit, skipped="unassigned"))
            continue
        tools = build_tools_for(assignment, registry)
        if not tools:
            outcomes.append(UnitOutcome(unit=unit, assignment=assignment,
                                        skipped="no_tools_for_capabilities"))
            continue
        loop = FunctionCallLoop(gateway, dispatcher, settings=cfg, max_rounds=max_rounds)
        hint = mention_tools_available(tools)
        messages = [
            ChatMessage(role="system", content=hint) if hint else None,
            ChatMessage(role="user", content=unit.text),
        ]
        result = await loop.run(
            user_id=user_id,
            messages=[m for m in messages if m is not None],
            tools=tools)
        outcomes.append(UnitOutcome(unit=unit, assignment=assignment,
                                    tools=tuple(tools), result=result))
    return IntakeReport(outcomes=tuple(outcomes))


__all__ = [
    "ASSIGNMENTS",
    "Assignment",
    "InformationUnit",
    "IntakeContext",
    "IntakeReport",
    "SlotKind",
    "UnitOutcome",
    "assign",
    "build_tools_for",
    "detect_slot_kind",
    "discretize",
    "plan_assignments",
    "resolve_references",
    "run_units",
    "split_units",
]
