"""检索侧查询路由器 —— 决定一次查询走单路线还是多路线。

计划书依据
----------
- 三、P2「搭建 BM25 + 向量检索 + Rerank 三重混合检索服务」
- 一、项目概述 / 四、1「五层意图路由：规则匹配 → Jev → 向量 → 垂类决策模型 → 大模型兜底」
- 三、P6「质检 Agent 校验纠错（调用 Wiki 做事实溯源）」

裁定
----
计划书规定了**有哪些检索手段**，未规定**何时用哪一条**；
本路由器的三档与判据为裁定 **U46**（来源与复用边界见 `core/decisions.py`）。

与五层意图路由的关系（用户选定「两者结合」）
------------------------------------------
**五层意图路由定大方向，本路由器在检索侧做细粒度决策**：

- 意图路由是判「这句话想干什么」，本路由器是判「这句话该怎么查」。
- 五层链路已由计划书【八】**冻结**，本模块**不新增第六层**，只消费它的结论：
  `intent_layer="llm_fallback"` 意味着连意图都不确定，检索自然要走最重的一档。
- 反过来，意图层命中 hard_rule 时，检索也倾向于直查 —— 两者方向一致时互相印证。

三档路线
--------
| 档 | 什么时候走 | 干什么 |
|---|---|---|
| `RULE_DIRECT` | 查询含**精确标识**（编号 / 日期 / 版本 / 枚举值） | 单路线：直接查结构化库与 BM25，**完全不经过大模型** |
| `HYBRID` | 无精确标识但语义可判 | 多路线：BM25 与向量并行 → 融合 → topN 候选 → Rerank → topK |
| `LLM_ANALYSIS` | 多意图冲突 / 证据互相矛盾 / 置信不足 | 交给大模型，并把该次歧义样本落盘（U50 学习回流） |

判据**先看证据不看直觉**：每条结论都带 `matched_signals`，
说得出是哪一条命中了 —— 否则「为什么走这条路」无从追溯（E-53）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

#: 五层意图路由的层名（取自 scheduler.router，此处只引用不重新定义层级）
LAYER_HARD_RULE = "hard_rule"
LAYER_JEV = "jev"
LAYER_VECTOR = "vector"
LAYER_VERTICAL = "vertical_model"
LAYER_LLM_FALLBACK = "llm_fallback"


class QueryRoute(str, Enum):
    """检索路线。"""

    RULE_DIRECT = "rule_direct"      # 单路线：规则直查，不过大模型
    HYBRID = "hybrid"                # 多路线：BM25 + 向量 → topN → Rerank → topK
    LLM_ANALYSIS = "llm_analysis"    # 大模型分析（歧义 / 冲突 / 置信不足）


#: 精确标识的判据：(正则, 信号名)。命中即倾向 RULE_DIRECT。
#: 这些是「向量检索的固有盲区」—— 语义相近但字面必须精确匹配的场景。
EXACT_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"\d{5,}", "长数字编号"),                       # 订单号、流水号、SKU 数字段
    (r"[A-Za-z]{2,}[-_/]?\d{2,}", "字母数字混合编号"),  # SKU-12345、ERR404、v2.3
    (r"\d{4}[-/年]\d{1,2}([-/月]\d{1,2})?", "日期"),
    (r"\bv?\d+(\.\d+){1,3}\b", "版本号"),
    (r"[「『\"'][^」』\"']{2,20}[」』\"']", "引号精确短语"),
    (r"(状态码|错误码|编号|订单号|版本号|型号)", "标识类字段词"),
)

#: 歧义 / 冲突的判据。命中即倾向 LLM_ANALYSIS。
AMBIGUITY_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"(还是|或者|或是|二者|哪个更)", "选择困难"),
    (r"(对比|区别|差异|优缺点|哪个好)", "比较诉求"),
    (r"(不确定|不一定|可能吧|大概|也许是)", "自我不确定"),
    (r"(矛盾|冲突|不一致|说法不一)", "证据矛盾"),
    (r"(听说|网传|有人说|据传)", "来源存疑"),
)

#: 多个意图并存的粗判据（更精确的多意图判定由 Jev 层负责，此处只做检索侧兜底）
MULTI_INTENT_PATTERN = re.compile(r"[?？；;]|以及|并且|另外|同时")


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingDecision:
    route: QueryRoute
    #: **必须写明理由** —— 否则「为什么没走直查」无从追溯
    reason: str
    #: 命中的判据信号名。三档都可能为空（例如纯语义查询走 HYBRID 是默认档）
    matched_signals: tuple[str, ...] = ()
    #: 是否**完全不经过大模型**
    bypasses_llm: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"route": self.route.value, "reason": self.reason,
                "matched_signals": list(self.matched_signals),
                "bypasses_llm": self.bypasses_llm}


def _match_all(patterns: Sequence[tuple[str, str]], query: str
               ) -> tuple[str, ...]:
    found: list[str] = []
    for pattern, name in patterns:
        if re.search(pattern, query):
            found.append(name)
    return tuple(found)


def route_query(query: str, *, intent_layer: str | None = None,
                min_confidence: float | None = None,
                confidence_threshold: float = 0.6) -> RoutingDecision:
    """决定一次查询走哪条检索路线。

    参数
    ----
    query:
        用户查询原文。
    intent_layer:
        五层意图路由给出的结论层（`scheduler.router` 的层名）。
        **只消费不改写**：五层链路已冻结，本函数绝不新增层。
    min_confidence:
        已有证据里的最低置信度（来自检索结果的 `confidence`）。
        低于阈值说明证据本身就软，该上大模型。
    confidence_threshold:
        置信不足的判定阈值（实现参数）。
    """
    if not query or not query.strip():
        raise RoutingError("查询不得为空 —— 空查询无从判定路线")

    text = query.strip()
    exact = _match_all(EXACT_SIGNALS, text)
    ambiguous = _match_all(AMBIGUITY_SIGNALS, text)

    # ---- 第一优先：意图层已经走到大模型兜底 ----
    # 连意图都判不出来，检索再走捷径只会快而错。
    if intent_layer == LAYER_LLM_FALLBACK:
        return RoutingDecision(
            route=QueryRoute.LLM_ANALYSIS,
            reason="五层意图路由已降级到 llm_fallback：意图本身不确定，"
                   "检索必须交给大模型而不是走捷径",
            matched_signals=("intent_layer=llm_fallback",),
        )

    # ---- 第二优先：证据置信不足 ----
    if min_confidence is not None and min_confidence < confidence_threshold:
        return RoutingDecision(
            route=QueryRoute.LLM_ANALYSIS,
            reason=f"证据最低置信度 {min_confidence:.2f} 低于阈值 {confidence_threshold:.2f}，"
                   f"证据本身就软，需大模型介入分析",
            matched_signals=(f"low_confidence={min_confidence:.2f}",),
        )

    # ---- 第三优先：明确的歧义 / 冲突信号 ----
    if ambiguous:
        return RoutingDecision(
            route=QueryRoute.LLM_ANALYSIS,
            reason=f"查询含歧义或冲突信号（{'、'.join(ambiguous)}），"
                   f"规则与向量都无法判别，交给大模型并落盘为歧义样本（U50）",
            matched_signals=ambiguous,
        )

    # ---- 第四优先：精确标识 → 单路线直查 ----
    # 直查是**唯一完全不过大模型**的档：省掉一次模型调用，且不会被语义漂移带偏。
    if exact:
        return RoutingDecision(
            route=QueryRoute.RULE_DIRECT,
            reason=f"查询含精确标识（{'、'.join(exact)}），"
                   f"属向量检索的固有盲区，直接查库不经大模型",
            matched_signals=exact,
            bypasses_llm=True,
        )

    # ---- 默认：多路线混合检索 ----
    return RoutingDecision(
        route=QueryRoute.HYBRID,
        reason="无精确标识、无歧义信号：走 BM25 + 向量并行 → 融合 → topN → Rerank → topK",
        matched_signals=(),
    )


def describe_routes() -> Mapping[str, str]:
    """各路线的人读说明，供日志与自检使用。"""
    return {
        QueryRoute.RULE_DIRECT.value: "单路线：规则直查库，完全不经过大模型",
        QueryRoute.HYBRID.value: "多路线：BM25 + 向量 → 融合 → topN → Rerank → topK",
        QueryRoute.LLM_ANALYSIS.value: "大模型分析（歧义 / 冲突 / 置信不足），样本落盘回流",
    }
