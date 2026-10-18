"""P1 数据置信度打分算法。

计划书依据
----------
- 三、P1「实现数据置信度打分算法；**置信分数融合 Agent-Wiki 证据权重**」
- 三、P1 表注「每一条增量事实生成 Claim 声明并绑定 Evidence 证据；自动标记置信度」
- 三、P9「Agent-Wiki 指标：声明平均置信度、证据缺失占比」

算法口径
--------
计划书要求「实现」算法但未给出公式，故公式属**实现参数**，此处显式登记：

    evidence_score = Σ(reliabilityᵢ × weightᵢ) / Σ(weightᵢ)         证据加权平均
    coverage       = min(1, Σ(weightᵢ) / 饱和权重)                   证据充分度
    influence      = 证据影响力上限 × coverage
    confidence     = base × (1 − influence) + evidence_score × influence

含义：**证据越充分，结论越向证据看齐；证据不足时回落到基础分**。
无证据时 confidence == base，不做任何推测性抬升。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

#: 证据对最终置信度的最大影响力（实现参数）。
#: 取 0.75 而非更低：若上限过低（如 0.6），即使基础分 0.5 配上完美证据也只能到 0.80，
#: 永远够不到【八】的入库阈值 0.82，「融合证据权重」就形同虚设。
#: 同时保留 25% 的基础分话语权，使**弱基础分无法仅凭证据被顶过阈值**（见边界用例）。
EVIDENCE_INFLUENCE_CAP = 0.75
#: 证据总权重达到该值即视为「证据充分」（实现参数）
EVIDENCE_SATURATION_WEIGHT = 3.0


class ConfidenceError(ValueError):
    pass


@dataclass(frozen=True)
class EvidenceRef:
    """一条 Agent-Wiki 证据的评分视图。"""

    evidence_id: str
    weight: float = 1.0           # 证据权重（可为来源可靠度加权后的结果）
    reliability: float = 1.0      # 该证据本身的可信度 0~1

    def __post_init__(self) -> None:
        if not 0.0 <= self.reliability <= 1.0:
            raise ConfidenceError(f"证据 {self.evidence_id} 的 reliability 必须在 0~1")
        if self.weight < 0:
            raise ConfidenceError(f"证据 {self.evidence_id} 的 weight 不得为负")


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """打分过程，便于质检 Agent 与人工复核追溯。"""

    base: float
    evidence_score: float
    coverage: float
    influence: float
    confidence: float
    evidence_count: int

    @property
    def evidence_missing(self) -> bool:
        """计划书七、19「证据缺失」判定的依据。"""
        return self.evidence_count == 0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def score_confidence(*, base: float, evidences: Sequence[EvidenceRef] | Iterable[EvidenceRef] = ()
                     ) -> ConfidenceBreakdown:
    """融合基础分与 Agent-Wiki 证据权重，得到最终置信度。"""
    if not 0.0 <= base <= 1.0:
        raise ConfidenceError(f"base 必须在 0~1，实际 {base!r}")
    refs = list(evidences)

    total_weight = sum(e.weight for e in refs)
    if total_weight <= 0:
        return ConfidenceBreakdown(base=_clamp01(base), evidence_score=0.0, coverage=0.0,
                                   influence=0.0, confidence=_clamp01(base),
                                   evidence_count=len(refs))

    evidence_score = sum(e.reliability * e.weight for e in refs) / total_weight
    coverage = min(1.0, total_weight / EVIDENCE_SATURATION_WEIGHT)
    influence = EVIDENCE_INFLUENCE_CAP * coverage
    confidence = base * (1.0 - influence) + evidence_score * influence
    return ConfidenceBreakdown(
        base=_clamp01(base), evidence_score=_clamp01(evidence_score),
        coverage=coverage, influence=influence, confidence=_clamp01(confidence),
        evidence_count=len(refs),
    )


def aggregate_evidence_score(refs: Sequence[EvidenceRef]) -> float:
    """仅取证据侧的加权平均分（供 Agent-Wiki 声明置信度使用）。"""
    total = sum(e.weight for e in refs)
    if total <= 0:
        return 0.0
    return _clamp01(sum(e.reliability * e.weight for e in refs) / total)
