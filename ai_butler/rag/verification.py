"""回答前的复检回路 —— 发出去之前再检索一遍，站不住就别发。

计划书依据
----------
- 三、P6「内容质检 Agent｜全局防错、防幻、防假」
- 四、1「质检 Agent 校验纠错（调用 Wiki 做事实溯源，Jev 辅助可信度判定）」
- 三、P2「返回结果携带 Agent-Wiki 证据 ID、置信度」「素材溯源」

裁定
----
计划书把质检定位在**产出之后**，未规定**回答发出之前**是否要再检索一遍。
本回路的形态与阈值为裁定 **U47**（来源与复用边界见 `core/decisions.py`）。

三条从开源实践借来的硬结论
--------------------------
1. **失败时改写查询，不是用同一 query 重试** ——
   同 query + 同索引必然召回同一批坏结果，重试只是烧 token。
   本实现拿**站不住的那一句**去检索，而不是重跑原问题。
2. **必须有硬逃逸上限** —— 对抗性或域外查询会让 critic 循环跑到 token 预算耗尽。
   `max_iterations` 默认 2，到顶即停，不再尝试。
3. **必须实现 abstain** —— 证据不足就说不知道，
   比编一个自信的错误答案便宜得多。缺失证据清单要给出来，
   交 P6 与 Agent-Wiki 沉淀为 Question。

溯源
----
每个被判定为「站得住」的声明都要带上支撑它的 evidence_ids。
**没有证据 ID 的声明不算通过** —— 这正是 P2「强制所有素材绑定溯源来源」在出口侧的落实。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

#: 分句边界。中英文混排，句号/问号/感叹号/分号 + 换行都算。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;\n])\s*")

#: 过短的片段不参与校验（"好的"、"是的"这类没有事实可验）
_MIN_CLAIM_CHARS = 6


class EvidenceFinder(Protocol):
    """检索接口。用 Protocol 而不是具体类，便于测试与替换实现。"""

    def find(self, claim: str, *, top_k: int) -> Sequence["EvidenceHit"]:
        ...


@dataclass(frozen=True)
class EvidenceHit:
    """一条支撑证据。字段对齐 `rag.service.SearchHit` 的最小集。"""

    text: str
    score: float
    evidence_ids: tuple[str, ...] = ()
    source_id: str = ""


@dataclass(frozen=True)
class ClaimCheck:
    """单条声明的校验结果。"""

    claim: str
    grounded: bool
    #: 支撑它的证据 ID。**为空即视为不通过**，哪怕分数很高
    evidence_ids: tuple[str, ...] = ()
    best_score: float = 0.0


@dataclass(frozen=True)
class VerificationResult:
    """一次复检的结论。"""

    #: 整体是否站得住
    grounded: bool
    #: 有证据支撑的声明占比（0~1）。短回复分母会很小，注意别拿它当唯一判据
    attribution_rate: float
    checks: tuple[ClaimCheck, ...] = ()
    #: 站不住的声明 —— **下一轮就拿这些去检索**，而不是重跑原查询
    unsupported: tuple[str, ...] = ()
    #: 缺失的事实清单，供 abstain 文案与 Agent-Wiki Question 沉淀
    missing_facets: tuple[str, ...] = ()
    #: 实际重试轮数
    attempts: int = 1
    #: 是否放弃回答（abstain）
    abstained: bool = False
    #: 是否撞到逃逸上限（撞上说明这个查询不该靠循环解决）
    hit_iteration_cap: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "grounded": self.grounded, "attribution_rate": round(self.attribution_rate, 3),
            "unsupported": list(self.unsupported), "missing_facets": list(self.missing_facets),
            "attempts": self.attempts, "abstained": self.abstained,
            "hit_iteration_cap": self.hit_iteration_cap,
            "checks": [{"claim": c.claim, "grounded": c.grounded,
                        "evidence_ids": list(c.evidence_ids),
                        "best_score": round(c.best_score, 3)} for c in self.checks],
        }


def split_claims(answer: str) -> tuple[str, ...]:
    """把回答拆成可校验的声明。过短的片段直接跳过。"""
    parts = (p.strip() for p in _SENTENCE_SPLIT.split(answer or ""))
    return tuple(p for p in parts if len(p) >= _MIN_CLAIM_CHARS)


def _bigrams(text: str) -> set[str]:
    """字符二元组集合，用于字面重叠度。中文没有空格分词，二元组是低成本的近似。"""
    cleaned = re.sub(r"\s+", "", text or "")
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def overlap_ratio(claim: str, evidence: str) -> float:
    """声明与证据的字面重叠度（Jaccard）。用于判断「这段证据能不能撑住这句话」。"""
    a, b = _bigrams(claim), _bigrams(evidence)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _claim_supported_by_hits(claim: str, hits: Sequence[EvidenceHit], *,
                             score_threshold: float,
                             min_overlap: float = 0.2) -> tuple[str, ...]:
    """一批已召回的证据里，有没有能撑住这条声明的（**且带证据 ID**）。

    返回支撑它的 evidence_ids；为空即没撑住。
    """
    ids: list[str] = []
    for h in hits:
        if h.score < score_threshold or not h.evidence_ids:
            continue
        if overlap_ratio(claim, h.text) >= min_overlap:
            for eid in h.evidence_ids:
                if eid not in ids:
                    ids.append(eid)
    return tuple(ids)


def _check_claim(claim: str, finder: EvidenceFinder, *, top_k: int,
                 score_threshold: float) -> ClaimCheck:
    """校验单条声明：检索回来有没有**带证据 ID**的支撑。"""
    hits = finder.find(claim, top_k=top_k)
    usable = [h for h in hits if h.score >= score_threshold and h.evidence_ids]
    if not usable:
        best = max((h.score for h in hits), default=0.0)
        return ClaimCheck(claim=claim, grounded=False, evidence_ids=(), best_score=best)
    best = max(usable, key=lambda h: h.score)
    ids: list[str] = []
    for h in usable:
        for eid in h.evidence_ids:
            if eid not in ids:
                ids.append(eid)
    return ClaimCheck(claim=claim, grounded=True,
                      evidence_ids=tuple(ids), best_score=best.score)


def verify(answer: str, finder: EvidenceFinder, *, top_k: int = 3,
           score_threshold: float = 0.35,
           min_attribution_rate: float = 0.8) -> VerificationResult:
    """复检一份回答，但**只做一轮、不重试**。

    想要「不通过就改写查询再试」的完整回路，用 `verify_with_retry()`。
    """
    claims = split_claims(answer)
    if not claims:
        # 没有可校验的事实陈述（例如纯问候），不判为幻觉
        return VerificationResult(grounded=True, attribution_rate=1.0, attempts=1)

    checks = tuple(_check_claim(c, finder, top_k=top_k,
                                score_threshold=score_threshold) for c in claims)
    grounded_count = sum(1 for c in checks if c.grounded)
    rate = grounded_count / len(checks)
    unsupported = tuple(c.claim for c in checks if not c.grounded)

    return VerificationResult(
        grounded=rate >= min_attribution_rate,
        attribution_rate=rate,
        checks=checks,
        unsupported=unsupported,
        missing_facets=unsupported,
    )


def verify_with_retry(answer: str, finder: EvidenceFinder, *,
                      top_k: int = 3, score_threshold: float = 0.35,
                      min_attribution_rate: float = 0.8,
                      max_iterations: int = 2,
                      rewriter: Callable[[Sequence[str]], str] | None = None
                      ) -> VerificationResult:
    """复检 + 失败改写查询再验，**带硬逃逸上限**。

    改写用**站不住的那些声明**，不是原查询 —— 同 query 同索引必然召回同一批坏结果。
    """
    result = verify(answer, finder, top_k=top_k, score_threshold=score_threshold,
                    min_attribution_rate=min_attribution_rate)
    if result.grounded:
        return result

    attempts = 1
    current = result
    while not current.grounded and attempts < max_iterations:
        attempts += 1
        # **用站不住的声明拼出新的检索词** —— 缺什么补什么，不是重跑原问题
        query = _rewrite(current.unsupported, rewriter)
        if not query:
            break
        # 用改写后的查询召回一批新证据
        new_hits = list(finder.find(query, top_k=top_k * 2))
        # 用这批新证据去覆盖此前站不住的声明
        merged: list[ClaimCheck] = []
        for old in current.checks:
            if old.grounded:
                merged.append(old)
                continue
            gained = _claim_supported_by_hits(
                old.claim, new_hits, score_threshold=score_threshold)
            if gained:
                merged.append(ClaimCheck(
                    claim=old.claim, grounded=True,
                    evidence_ids=old.evidence_ids + tuple(
                        e for e in gained if e not in old.evidence_ids),
                    best_score=old.best_score))
            else:
                merged.append(old)
        merged_tuple = tuple(merged)
        rate = sum(1 for c in merged_tuple if c.grounded) / len(merged_tuple)
        current = VerificationResult(
            grounded=rate >= min_attribution_rate,
            attribution_rate=rate, checks=merged_tuple,
            unsupported=tuple(c.claim for c in merged_tuple if not c.grounded),
            missing_facets=tuple(c.claim for c in merged_tuple if not c.grounded),
            attempts=attempts,
            abstained=False,
        )
    # 循环正常结束（可能因 break 提前退出）

    if not current.grounded:
        return VerificationResult(
            grounded=False,
            attribution_rate=current.attribution_rate,
            checks=current.checks,
            unsupported=current.unsupported,
            missing_facets=current.missing_facets,
            attempts=current.attempts,
            abstained=True,
            hit_iteration_cap=current.attempts >= max_iterations,
        )
    return current


def _rewrite(unsupported: Sequence[str],
             rewriter: Callable[[Sequence[str]], str] | None) -> str:
    """把站不住的声明拼成下一轮检索词。默认实现：取最长的一句。"""
    if rewriter is not None:
        return rewriter(unsupported)
    if not unsupported:
        return ""
    return max(unsupported, key=len)


def render_abstention(result: VerificationResult) -> str:
    """abstain 文案。**由系统渲染，不由模型生成**（与 decisions.U25 同口径）。

    说清缺什么，而不是含糊一句「我不知道」——
    用户据此能补充信息，这就是「缺失证据清单」的价值。
    """
    if not result.abstained:
        raise ValueError("仅在 abstained 时渲染弃答文案")
    if not result.missing_facets:
        return "现有资料不足以支撑这个回答，且我无法指出缺什么 —— 请转人工复核。"
    items = "；".join(result.missing_facets[:3])
    suffix = "等" if len(result.missing_facets) > 3 else ""
    return (f"现有资料不足以支撑这个回答。缺证据的是：{items}{suffix}。"
            f"补充这些材料后我可以重新检索并作答，或转人工复核。")
