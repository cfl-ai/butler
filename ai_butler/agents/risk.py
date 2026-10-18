"""P5 低置信信息风险标注 与 流式输出分片。

计划书依据
----------
- 三、P5「开发多约束智能推理逻辑；**低置信 Wiki 信息输出时做风险标注**」
- 三、P5「开发**流式 Token 输出**，支撑手机端**逐字文字 + 语音同步播报**」
- 七、19「矛盾、证据缺失、过期素材可自动产出报告」（风险标注与质检同源）
- 八、jev_confidence_threshold = 0.82

两条机制
--------
1. **风险标注**：输出前逐条判定，低置信或缺证据的信息**不隐藏、不丢弃**，
   而是打上标记并在整体提示语里讲清楚——防幻觉靠的是让用户看得见不确定性，
   而不是把不确定的内容悄悄删掉（删掉反而会让用户以为剩下的都是确定的）。
2. **流式分片**：把文本切成适合「逐字渲染 + 同步 TTS」的小片。
   分片只在**语义边界**断开，不把一句话劈成两半念出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

#: 风险标记（枚举）
RISK_NONE = ""
RISK_LOW_CONFIDENCE = "low_confidence"
RISK_MISSING_EVIDENCE = "missing_evidence"
RISK_LEVELS: tuple[str, ...] = (RISK_LOW_CONFIDENCE, RISK_MISSING_EVIDENCE)

#: 整体风险提示语模板。**由系统渲染，不由模型生成**（与 decisions.U25 同口径）。
RISK_BANNER_TEMPLATE = "提示：以下 {count} 条信息证据不足或置信偏低（{kinds}），请谨慎参考。"

RISK_KIND_LABELS: dict[str, str] = {
    RISK_LOW_CONFIDENCE: "置信偏低",
    RISK_MISSING_EVIDENCE: "缺少证据",
}

#: 语音分片的目标长度与断句边界（实现参数）
DEFAULT_CHUNK_CHARS = 24
SPEECH_BOUNDARIES: tuple[str, ...] = ("。", "！", "？", "；", "\n", "，", "、")


class RiskError(ValueError):
    pass


@dataclass(frozen=True)
class RiskPolicy:
    """标注口径。阈值**复用【八】已有的 0.82**，不新造第二个数字。"""

    min_confidence: float = 0.82
    require_evidence: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_confidence <= 1.0:
            raise RiskError(f"min_confidence 必须落在 0~1，实际 {self.min_confidence!r}")


@dataclass(frozen=True)
class OutputItem:
    """一条待输出的信息。"""

    statement: str
    confidence: float | None = None
    evidence_ids: tuple[str, ...] = ()
    claim_page_id: str = ""

    def __post_init__(self) -> None:
        if not self.statement or not str(self.statement).strip():
            raise RiskError("待输出信息不得为空")


@dataclass(frozen=True)
class AnnotatedItem:
    """标注后的信息。**原文一字不改**，只增加风险字段。"""

    statement: str
    confidence: float | None
    evidence_ids: tuple[str, ...]
    claim_page_id: str
    risk: str = RISK_NONE
    risk_note: str = ""

    @property
    def safe(self) -> bool:
        return self.risk == RISK_NONE

    def as_dict(self) -> dict[str, Any]:
        return {"statement": self.statement, "confidence": self.confidence,
                "evidence_ids": list(self.evidence_ids),
                "claim_page_id": self.claim_page_id,
                "risk": self.risk, "risk_note": self.risk_note, "safe": self.safe}


class RiskAnnotator:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy or RiskPolicy()

    def annotate_one(self, item: OutputItem) -> AnnotatedItem:
        risk, note = RISK_NONE, ""
        if self.policy.require_evidence and not item.evidence_ids:
            risk = RISK_MISSING_EVIDENCE
            note = "该条信息未绑定 Evidence 证据 ID，无法溯源"
        elif item.confidence is not None and item.confidence < self.policy.min_confidence:
            risk = RISK_LOW_CONFIDENCE
            note = (f"置信 {item.confidence:.3f} 低于阈值 {self.policy.min_confidence}")
        return AnnotatedItem(statement=item.statement, confidence=item.confidence,
                             evidence_ids=tuple(item.evidence_ids),
                             claim_page_id=item.claim_page_id, risk=risk, risk_note=note)

    def annotate(self, items: Sequence[OutputItem]) -> list[AnnotatedItem]:
        return [self.annotate_one(i) for i in items]

    @staticmethod
    def risks(items: Sequence[AnnotatedItem]) -> list[dict[str, Any]]:
        return [{"statement": i.statement, "risk": i.risk, "note": i.risk_note,
                 "claim_page_id": i.claim_page_id} for i in items if not i.safe]

    def banner(self, items: Sequence[AnnotatedItem]) -> str:
        """整体风险提示语。无风险时返回空串（不输出无意义的提示）。"""
        risky = [i for i in items if not i.safe]
        if not risky:
            return ""
        kinds = "、".join(sorted({RISK_KIND_LABELS.get(i.risk, i.risk) for i in risky}))
        return RISK_BANNER_TEMPLATE.format(count=len(risky), kinds=kinds)


# --------------------------------------------------------------------------- #
# 流式输出分片
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SpeechChunk:
    index: int
    text: str
    is_last: bool = False
    #: 该片是否含风险提示——移动端据此做视觉/语音上的区别处理
    carries_risk: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "text": self.text, "is_last": self.is_last,
                "carries_risk": self.carries_risk}


def split_for_speech(text: str, *, chunk_chars: int = DEFAULT_CHUNK_CHARS,
                     carry_risk: bool = False) -> list[SpeechChunk]:
    """按语义边界把文本切成适合逐字渲染 + 同步 TTS 的小片。

    超长句（没有边界可断）才硬切——避免把一句话念成两半。
    """
    if chunk_chars <= 0:
        raise RiskError("chunk_chars 必须为正数")
    body = (text or "").strip()
    if not body:
        return []

    pieces: list[str] = []
    buf = ""
    for ch in body:
        buf += ch
        if ch in SPEECH_BOUNDARIES and len(buf) >= chunk_chars:
            pieces.append(buf.strip())
            buf = ""
    if buf.strip():
        pieces.append(buf.strip())

    # 没有自然边界的长句再按长度硬切
    final: list[str] = []
    for piece in pieces:
        if len(piece) <= chunk_chars * 2:
            final.append(piece)
        else:
            final.extend(piece[i:i + chunk_chars]
                         for i in range(0, len(piece), chunk_chars))
    final = [p for p in final if p]
    return [SpeechChunk(index=i, text=p, is_last=(i == len(final) - 1),
                        carries_risk=carry_risk) for i, p in enumerate(final)]


def stream_chunks(chunks: Sequence[SpeechChunk]) -> Iterator[SpeechChunk]:
    """流式吐片。真实部署里这里对接 SSE / WebSocket 逐片下发。"""
    for chunk in chunks:
        yield chunk
