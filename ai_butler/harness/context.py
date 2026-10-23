"""P10 上下文压缩（裁定 U36 + U58）。

计划书依据
----------
- 三、P10 步骤7「开发上下文压缩机制，对接数据中台记忆摘要；
  **摘要脱敏，不带原始用户隐私**」
- 三、P10 触发时机「离线链路**绝对不能访问原始未脱敏用户私有数据**」

裁定 U36
--------
压缩分两级：结构化裁剪（默认生效）+ 模型摘要（端口注入）。
**压缩不替代脱敏**：顺序固定为「先脱敏，再裁剪，再摘要，最后再验一次」——
压缩会把分散的片段聚到一起，原本单看不敏感的字段拼起来可能就构成身份。

裁定 U58（2026-10-23 补，计划书未定义项）
--------------------------------------
- **shadowing（只让位，不删除）**：被压条目不再物理丢弃。原实现
  ~{dropped}~ 只是个计数，被压内容连同让位原因一起留痕（影子记录），
  经账本可恢复——「压掉」变成「让位」，不是「抹除」。
- **分项计量（contextBreakdown）**：压缩结果按 source / scope / 相关度
  分带分项统计条数与 token，输出压力值 pressure（占用/预算）。
  单维度总数无法回答「是哪类内容挤占了预算」，分项才能。
- **token 口径是估算**：默认计数器为字符启发式（CJK≈1 token/字，
  其余≈1 token/4 字符），~{estimated=True}~ 如实标注——**不冒充模型精确
  计数**。精确计数为可注入端口（TokenCounter），调用方接 tiktoken 之类的
  实现即生效，本项目不内置该依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from ai_butler.evaluation.redaction import Redactor


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    text: str
    source: str = ""
    #: 相关性 0~1，用于裁剪排序
    relevance: float = 0.0
    occurred_at: str = ""
    scope: str = "public"


#: token 计数端口。实现方决定口径；~{estimated}~ 必须如实反映是不是精确计数。
class TokenCounter(Protocol):
    @property
    def estimated(self) -> bool:
        ...

    def count(self, text: str) -> int:
        ...


#: CJK 字符（按字计）与非 CJK（按 4 字符≈1 token 计）的判定
_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


@dataclass(frozen=True)
class CharHeuristicCounter:
    """字符启发式估算器。**estimate 口径，不是模型精确计数。**"""

    chars_per_token: int = 4

    @property
    def estimated(self) -> bool:
        return True

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = len(_CJK.findall(text))
        other = len(text) - cjk
        return cjk + max(1, other // self.chars_per_token) if other else cjk


@dataclass(frozen=True)
class ShadowedItem:
    """被压条目的影子：**让位，不是删除**。

    带脱敏后的原文，因此从账本/返回值即可重建完整上下文；
    `reason` 记录让位原因（超条数 / 超预算），恢复时可判据。
    """

    item_id: str
    text: str
    source: str
    scope: str
    relevance: float
    occurred_at: str
    reason: str
    estimated_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "text": self.text, "source": self.source,
                "scope": self.scope, "relevance": self.relevance,
                "occurred_at": self.occurred_at, "reason": self.reason,
                "estimated_tokens": self.estimated_tokens}


#: 让位原因
SHADOW_MAX_ITEMS = "max_items"        # 超出条数上限
SHADOW_TOKEN_BUDGET = "token_budget"  # 超出 token 预算


@dataclass(frozen=True)
class ContextBreakdown:
    """分项计量：按 source / scope / 相关度分带。

    单维度总数回答不了「哪类内容挤占预算」——分项才能。
    `estimated_tokens` 的口径由计数器决定（默认估算，如实标注）。
    """

    total_items: int
    by_source: dict[str, int]
    by_scope: dict[str, int]
    by_relevance_band: dict[str, int]
    estimated_tokens: int
    token_budget: int | None
    counter_estimated: bool

    @property
    def pressure(self) -> float:
        """上下文压力 = 占用 / 预算。无预算时 0.0（并如实标注）。"""
        if not self.token_budget:
            return 0.0
        return min(1.0, self.estimated_tokens / self.token_budget)

    def as_dict(self) -> dict[str, Any]:
        return {"total_items": self.total_items, "by_source": dict(self.by_source),
                "by_scope": dict(self.by_scope),
                "by_relevance_band": dict(self.by_relevance_band),
                "estimated_tokens": self.estimated_tokens,
                "token_budget": self.token_budget,
                "counter_estimated": self.counter_estimated,
                "pressure": self.pressure}


#: 相关度分带（与评测的 band 化一致：看区间，不看单点）
def _band(relevance: float) -> str:
    if relevance >= 0.8:
        return "high"
    if relevance >= 0.4:
        return "mid"
    return "low"


@dataclass(frozen=True)
class CompressedContext:
    items: tuple[ContextItem, ...]
    dropped: int
    summary: str
    model_layer_active: bool
    #: U58：被压条目的影子（让位记录，内容已脱敏，可恢复）
    shadowed: tuple[ShadowedItem, ...] = ()
    #: U58：分项计量
    breakdown: ContextBreakdown | None = None

    @property
    def text(self) -> str:
        parts = [i.text for i in self.items]
        if self.summary:
            parts.append(self.summary)
        return "\n".join(parts)

    @property
    def shadowed_tokens(self) -> int:
        return sum(s.estimated_tokens for s in self.shadowed)

    def as_dict(self) -> dict[str, Any]:
        return {"item_count": len(self.items), "dropped": self.dropped,
                "summary": self.summary, "model_layer_active": self.model_layer_active,
                "shadowed": [s.as_dict() for s in self.shadowed],
                "breakdown": self.breakdown.as_dict() if self.breakdown else None,
                "items": [{"item_id": i.item_id, "source": i.source,
                           "relevance": i.relevance, "scope": i.scope}
                          for i in self.items]}

    def restore_shadowed(self) -> tuple[ContextItem, ...]:
        """从影子重建被让位的条目——「压掉」可逆，「抹除」不可逆。"""
        return tuple(ContextItem(item_id=s.item_id, text=s.text, source=s.source,
                                 relevance=s.relevance, occurred_at=s.occurred_at,
                                 scope=s.scope)
                     for s in self.shadowed)


#: 模型摘要器：接收已脱敏文本，返回摘要
Summarizer = Callable[[str], str]


def compress(items: Sequence[ContextItem], *, redactor: Redactor,
             max_items: int = 20, summarizer: Summarizer | None = None,
             where: str = "Harness 上下文",
             token_budget: int | None = None,
             counter: TokenCounter | None = None,
             journal: Any = None) -> CompressedContext:
    """先脱敏 → 再裁剪 → 再摘要 → **最后再验一次**。

    U58 扩展：`token_budget` 给出即按预算裁剪（超预算的条目让位并留影子）；
    `journal`（EventJournal）传入即把让位记录与分项计量落账——
    崩溃恢复后知道「压掉了什么、为什么」。
    """
    if max_items <= 0:
        raise ContextError("max_items 必须为正")
    if token_budget is not None and token_budget <= 0:
        raise ContextError("token_budget 必须为正")
    counter = counter or CharHeuristicCounter()

    # ---- 1) 先脱敏：任何原文都不许越过这一步 ----
    safe_items: list[ContextItem] = []
    for item in items:
        safe_items.append(ContextItem(
            item_id=item.item_id, text=redactor.redact(item.text),
            source=item.source, relevance=item.relevance,
            occurred_at=item.occurred_at, scope=item.scope))

    # ---- 2) 结构化裁剪：相关度优先，其次新近；预算内装入，超限让位 ----
    ordered = sorted(safe_items, key=lambda i: (i.relevance, i.occurred_at),
                     reverse=True)
    kept: list[ContextItem] = []
    shadowed: list[ShadowedItem] = []
    used = 0
    for item in ordered:
        cost = counter.count(item.text)
        if len(kept) >= max_items:
            reason = SHADOW_MAX_ITEMS
        elif token_budget is not None and used + cost > token_budget and kept:
            # 已装入的至少留一条：空上下文比超预算更糟
            reason = SHADOW_TOKEN_BUDGET
        else:
            kept.append(item)
            used += cost
            continue
        shadowed.append(ShadowedItem(
            item_id=item.item_id, text=item.text, source=item.source,
            scope=item.scope, relevance=item.relevance,
            occurred_at=item.occurred_at, reason=reason,
            estimated_tokens=cost))
    dropped = len(ordered) - len(kept)
    assert dropped == len(shadowed), "让位数与影子数必须一致（U58 不变量）"

    # ---- 3) 分项计量（对**保留后**的上下文计量；摘要 token 在其后并入）----
    breakdown = ContextBreakdown(
        total_items=len(kept),
        by_source=_tally(i.source for i in kept),
        by_scope=_tally(i.scope for i in kept),
        by_relevance_band=_tally(_band(i.relevance) for i in kept),
        estimated_tokens=used,
        token_budget=token_budget,
        counter_estimated=bool(counter.estimated))

    # ---- 4) 模型摘要（可选）----
    summary = ""
    if summarizer is not None and kept:
        summary = redactor.redact(summarizer("\n".join(i.text for i in kept)))
    if summary:
        # 摘要也占预算：并入分项（不另起一份口径）
        breakdown = ContextBreakdown(
            total_items=breakdown.total_items,
            by_source=breakdown.by_source, by_scope=breakdown.by_scope,
            by_relevance_band=breakdown.by_relevance_band,
            estimated_tokens=used + counter.count(summary),
            token_budget=breakdown.token_budget,
            counter_estimated=breakdown.counter_estimated)

    # ---- 5) 让位记录落账（U58：留痕，可恢复）----
    if journal is not None and shadowed:
        journal.append("context_shadow", {
            "kept": len(kept), "shadowed_count": len(shadowed),
            "token_budget": token_budget,
            "breakdown": breakdown.as_dict(),
            "shadowed": [s.as_dict() for s in shadowed]})

    # ---- 6) 压缩完再验一次：合并后可能拼出身份 ----
    redactor.assert_clean("\n".join(i.text for i in kept), where=where)
    if summary:
        redactor.assert_clean(summary, where=where + "（摘要）")

    return CompressedContext(items=tuple(kept), dropped=dropped, summary=summary,
                             model_layer_active=summarizer is not None,
                             shadowed=tuple(shadowed), breakdown=breakdown)


def _tally(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        key = v or "(unknown)"
        out[key] = out.get(key, 0) + 1
    return out
