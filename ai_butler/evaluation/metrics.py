"""P9 指标计算 —— 全维度指标监控看板的口径层。

计划书依据
----------
三、P9「全维度指标监控看板搭建；业务指标聚合统计，原始用户隐私数据脱敏，
禁止展示明文用户信息；**新增监控指标：Jev 意图命中率、多意图识别准确率**；
**Agent-Wiki 指标：声明平均置信度、证据缺失占比、矛盾数量、过期素材占比**」

裁定 U31
--------
指标**由本模块计算**并以只读接口暴露（指标口径是资产，必须先固化）；
可视化层（Metabase）只是消费者。

口径一旦变化，历史对比就失去意义——因此指标 key 是**稳定契约**，
在 ~{METRIC_KEYS}~ 里显式冻结，并由测试守住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE
from ai_butler.evaluation.redaction import Redactor

#: 计划书 P9 新增的两项业务指标
JEV_INTENT_HIT_RATE = "jev_intent_hit_rate"
MULTI_INTENT_ACCURACY = "multi_intent_accuracy"

#: 计划书 P9 列出的四项 Agent-Wiki 指标
WIKI_CLAIM_AVG_CONFIDENCE = "wiki_claim_avg_confidence"
WIKI_EVIDENCE_MISSING_RATIO = "wiki_evidence_missing_ratio"
WIKI_CONTRADICTION_COUNT = "wiki_contradiction_count"
WIKI_STALE_MATERIAL_RATIO = "wiki_stale_material_ratio"

#: 指标 key 契约（**冻结**：改名等于破坏历史可比性）
METRIC_KEYS: tuple[str, ...] = (
    JEV_INTENT_HIT_RATE, MULTI_INTENT_ACCURACY,
    WIKI_CLAIM_AVG_CONFIDENCE, WIKI_EVIDENCE_MISSING_RATIO,
    WIKI_CONTRADICTION_COUNT, WIKI_STALE_MATERIAL_RATIO,
)

#: 素材过期判定天数（实现参数；计划书未给数值）
DEFAULT_STALE_DAYS = 180


@dataclass(frozen=True)
class MetricValue:
    key: str
    value: float
    unit: str
    sample_size: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": round(self.value, 6), "unit": self.unit,
                "sample_size": self.sample_size, "note": self.note}


@dataclass(frozen=True)
class JeVObservation:
    """一次 Jev 决策的观测记录。"""

    expected_intent: str
    actual_intent: str
    #: 该次请求本身是否为多意图
    multi_intent: bool = False
    #: 多意图场景下 Jev 是否正确识别出「不止一个意图」
    detected_multi: bool = False

    @property
    def hit(self) -> bool:
        return bool(self.expected_intent) and self.expected_intent == self.actual_intent


def jev_intent_hit_rate(observations: Sequence[JeVObservation]) -> MetricValue:
    """计划书 P9 新增指标：Jev 意图命中率。"""
    total = len(observations)
    if total == 0:
        return MetricValue(JEV_INTENT_HIT_RATE, 0.0, "ratio", 0, "无样本")
    hits = sum(1 for o in observations if o.hit)
    return MetricValue(JEV_INTENT_HIT_RATE, hits / total, "ratio", total,
                       f"{hits}/{total} 命中")


def multi_intent_accuracy(observations: Sequence[JeVObservation]) -> MetricValue:
    """计划书 P9 新增指标：多意图识别准确率。

    **只统计「本该是多意图」的样本**——把单意图样本混进来算，
    这个指标会被稀释得毫无意义（这正是它容易做错的地方）。
    """
    multi = [o for o in observations if o.multi_intent]
    if not multi:
        return MetricValue(MULTI_INTENT_ACCURACY, 0.0, "ratio", 0, "无多意图样本")
    correct = sum(1 for o in multi if o.detected_multi)
    return MetricValue(MULTI_INTENT_ACCURACY, correct / len(multi), "ratio", len(multi),
                       f"{correct}/{len(multi)} 正确识别为多意图")


# ============================== Agent-Wiki 指标 ============================== #
def wiki_claim_avg_confidence(pages: Sequence[Any]) -> MetricValue:
    """声明平均置信度。"""
    claims = [p for p in pages if getattr(p, "type", "") == CLAIM]
    if not claims:
        return MetricValue(WIKI_CLAIM_AVG_CONFIDENCE, 0.0, "score", 0, "无 Claim 页面")
    total = sum(float(getattr(p, "confidence", 0.0) or 0.0) for p in claims)
    return MetricValue(WIKI_CLAIM_AVG_CONFIDENCE, total / len(claims), "score",
                       len(claims))


def claim_evidence_ids(page: Any) -> tuple[str, ...]:
    """取一条 Claim 绑定的证据 id。

    **证据 id 存在 extra 里，不是 WikiPage 的顶层字段** ——
    写成 getattr(page, "evidence_ids") 会永远取到空，
    使「证据缺失占比」恒为 100%（本阶段实际踩过，见交付报告）。
    """
    extra = getattr(page, "extra", None) or {}
    return tuple(extra.get("evidence_ids") or ())


def wiki_evidence_missing_ratio(pages: Sequence[Any]) -> MetricValue:
    """证据缺失占比：Claim 未绑定任何 Evidence 的比例。"""
    claims = [p for p in pages if getattr(p, "type", "") == CLAIM]
    if not claims:
        return MetricValue(WIKI_EVIDENCE_MISSING_RATIO, 0.0, "ratio", 0, "无 Claim 页面")
    missing = sum(1 for p in claims if not claim_evidence_ids(p))
    return MetricValue(WIKI_EVIDENCE_MISSING_RATIO, missing / len(claims), "ratio",
                       len(claims), f"{missing}/{len(claims)} 缺证据")


def page_contradicts(page: Any) -> tuple[str, ...]:
    """取一页的矛盾记录。

    **矛盾是 WikiPage 的顶层字段**（P6 的教训：读 extra["contradicts"] 会永远检不出）。
    但 new_page 会把未识别关键字收进 extra，为兼容两种构造路径，
    顶层为空时回落到 extra —— 顺序不能反，否则又回到「永远检不出」。
    """
    top = tuple(getattr(page, "contradicts", None) or ())
    if top:
        return top
    extra = getattr(page, "extra", None) or {}
    return tuple(extra.get("contradicts") or ())


def wiki_contradiction_count(pages: Sequence[Any]) -> MetricValue:
    """矛盾数量。"""
    marked = [p for p in pages if page_contradicts(p)]
    edges = sum(len(page_contradicts(p)) for p in pages)
    return MetricValue(WIKI_CONTRADICTION_COUNT, float(edges), "count", len(marked),
                       f"{len(marked)} 个页面共 {edges} 条矛盾")


def wiki_stale_material_ratio(pages: Sequence[Any], *, now: datetime,
                              stale_days: int = DEFAULT_STALE_DAYS) -> MetricValue:
    """过期素材占比（按 updated_at 与阈值判定；阈值是实现参数）。"""
    materials = [p for p in pages if getattr(p, "type", "") == EVIDENCE]
    if not materials:
        return MetricValue(WIKI_STALE_MATERIAL_RATIO, 0.0, "ratio", 0, "无素材页面")
    cutoff = now - timedelta(days=stale_days)
    stale = 0
    for page in materials:
        stamp = getattr(page, "updated_at", "") or getattr(page, "created_at", "")
        parsed = _parse(stamp)
        if parsed is None or parsed < cutoff:
            stale += 1
    return MetricValue(WIKI_STALE_MATERIAL_RATIO, stale / len(materials), "ratio",
                       len(materials), f"{stale}/{len(materials)} 超过 {stale_days} 天")


def _parse(stamp: Any) -> datetime | None:
    if isinstance(stamp, datetime):
        return stamp
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================== 看板 ============================== #
@dataclass
class MetricBoard:
    """指标看板。**出口一律经脱敏**（计划书三、P9「禁止展示明文用户信息」）。"""

    values: list[MetricValue] = field(default_factory=list)
    generated_at: str = ""
    redactor: Redactor = field(default_factory=Redactor)

    def add(self, value: MetricValue) -> None:
        self.values.append(value)

    def keys(self) -> tuple[str, ...]:
        return tuple(v.key for v in self.values)

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "generated_at": self.generated_at,
            "metrics": {v.key: v.as_dict() for v in self.values},
            # 指标本身是聚合值；这里如实标注脱敏层级，不冒充做了模型级脱敏
            "redaction": {"regex_layer": True,
                          "model_layer": self.redactor.model_layer_active},
        }
        safe = self.redactor.redact_deep(payload)
        self.redactor.assert_deep_clean(safe, where="指标看板")
        return safe


def build_board(*, observations: Sequence[JeVObservation], pages: Sequence[Any],
                now: datetime, stale_days: int = DEFAULT_STALE_DAYS,
                generated_at: str = "", redactor: Redactor | None = None) -> MetricBoard:
    """按计划书 P9 的口径算出全部六项指标。"""
    board = MetricBoard(generated_at=generated_at or now.isoformat(),
                        redactor=redactor or Redactor())
    board.add(jev_intent_hit_rate(observations))
    board.add(multi_intent_accuracy(observations))
    board.add(wiki_claim_avg_confidence(pages))
    board.add(wiki_evidence_missing_ratio(pages))
    board.add(wiki_contradiction_count(pages))
    board.add(wiki_stale_material_ratio(pages, now=now, stale_days=stale_days))
    return board
