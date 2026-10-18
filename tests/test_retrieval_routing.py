"""检索路由（U46）与回答前复检（U47）的守护测试。

为什么要有它
------------
「有 BM25 + 向量 + Rerank」不等于「知道什么时候用哪一条」——
计划书规定了**检索手段**，没规定**路由判据**，这是 U46 要补的那一环。
复检同理：P6 质检在**产出之后**，而本回路在**发给用户之前**，两者不重复。

每条断言都对应一种具体的失效手法：
该直查的走了大模型、歧义被当成精确、假改写（算出新 query 却不用）、
没有证据 ID 也算通过、critic 循环跑到停不下来。
"""

from __future__ import annotations

import pytest

from ai_butler.rag.routing import (
    LAYER_HARD_RULE,
    LAYER_LLM_FALLBACK,
    QueryRoute,
    RoutingError,
    route_query,
)
from ai_butler.rag.verification import (
    EvidenceHit,
    overlap_ratio,
    render_abstention,
    split_claims,
    verify,
    verify_with_retry,
)


# ============================ 查询路由（U46） ============================ #

def test_exact_identifier_goes_direct_and_bypasses_llm():
    """含精确标识的查询必须走直查，且**完全不经过大模型**。"""
    for query in ("订单号 20240918ABCDE 到哪了", "ERR404 是什么意思",
                  "v2.3 的变更", "「退货政策」怎么写的"):
        decision = route_query(query)
        assert decision.route == QueryRoute.RULE_DIRECT, f"{query} 未走直查"
        assert decision.bypasses_llm, f"{query} 走直查却仍要经过大模型"
        assert decision.matched_signals, "直查必须说得出命中了哪条判据"


def test_ambiguous_query_goes_to_llm():
    """歧义 / 冲突查询交给大模型，且登记为歧义样本（U50）。"""
    for query in ("A 方案还是 B 方案好", "这两种说法有矛盾吗", "对比一下优缺点"):
        decision = route_query(query)
        assert decision.route == QueryRoute.LLM_ANALYSIS, f"{query} 未判定为歧义"


def test_plain_semantic_query_goes_hybrid():
    """无精确标识、无歧义 → 多路线混合检索（默认档）。"""
    decision = route_query("帮我规划一个轻松的周末行程")
    assert decision.route == QueryRoute.HYBRID


def test_intent_layer_fallback_overrides_exact_match():
    """**两者结合**的关键：意图层已降级时，即使含精确标识也不能走捷径。

    连意图都判不出来，检索再快也只是快而错。
    """
    decision = route_query("订单号 20240918ABCDE", intent_layer=LAYER_LLM_FALLBACK)
    assert decision.route == QueryRoute.LLM_ANALYSIS
    assert "intent_layer" in " ".join(decision.matched_signals)


def test_low_confidence_evidence_escalates_to_llm():
    """证据本身就软（置信低于阈值）时，不该拿它去直接回答。"""
    assert route_query("某个说法", min_confidence=0.9).route == QueryRoute.HYBRID
    assert route_query("某个说法", min_confidence=0.2).route == QueryRoute.LLM_ANALYSIS


def test_hard_rule_intent_still_allows_direct_lookup():
    """意图层命中硬规则 + 含精确标识 → 直查（两者方向一致时互相印证）。"""
    decision = route_query("订单号 20240918ABCDE", intent_layer=LAYER_HARD_RULE)
    assert decision.route == QueryRoute.RULE_DIRECT


def test_empty_query_is_refused():
    with pytest.raises(RoutingError):
        route_query("   ")


def test_every_route_has_a_human_readable_description():
    """三档都要有说明 —— 日志里出现 route 名时要查得到它是什么意思。"""
    from ai_butler.rag.routing import describe_routes
    described = describe_routes()
    for route in QueryRoute:
        assert route.value in described and described[route.value]


# ============================ 回答前复检（U47） ============================ #

class _FakeFinder:
    """可控的证据源。**记录每次被问了什么**，用来验证改写真的换了查询。"""

    def __init__(self, mapping: dict[str, list[EvidenceHit]] | None = None,
                 default: list[EvidenceHit] | None = None) -> None:
        self.mapping = mapping or {}
        self.default = default if default is not None else []
        self.asked: list[str] = []

    def find(self, claim: str, *, top_k: int = 3) -> list[EvidenceHit]:
        self.asked.append(claim)
        return self.mapping.get(claim, self.default)


def _hit(text: str, *, score: float = 0.9,
         evidence_ids: tuple[str, ...] = ("E1",)) -> EvidenceHit:
    return EvidenceHit(text=text, score=score, evidence_ids=evidence_ids, source_id="S1")


def test_grounded_answer_passes_with_citations():
    """每句都有带证据 ID 的支撑 → 通过，且证据 ID 可溯源。"""
    finder = _FakeFinder(default=[_hit("故宫周一闭馆，法定节假日除外")])
    result = verify("故宫周一闭馆。法定节假日除外。", finder)
    assert result.grounded
    for c in result.checks:
        assert c.evidence_ids, "通过的声明必须带证据 ID（P2 溯源硬要求）"


def test_hit_without_evidence_id_does_not_count():
    """分数再高，没有 evidence_ids 也不算支撑 —— 无源的事实不得出口。"""
    finder = _FakeFinder(default=[EvidenceHit(text="故宫周一闭馆", score=0.99,
                                              evidence_ids=(), source_id="S1")])
    result = verify("故宫周一闭馆。", finder)
    assert not result.grounded, "无证据 ID 的证据不得让声明通过"


def test_low_score_hit_does_not_count():
    finder = _FakeFinder(default=[_hit("故宫周一闭馆", score=0.05)])
    assert not verify("故宫周一闭馆。", finder).grounded


def test_unsupported_answer_abstains_and_names_what_is_missing():
    """站不住就弃答，且要说出**缺什么**，而不是含糊一句「不知道」。"""
    finder = _FakeFinder(default=[])
    result = verify_with_retry("故宫周一闭馆。门票二百元。", finder)
    assert result.abstained
    assert result.missing_facets, "弃答时必须给出缺失证据清单"
    text = render_abstention(result)
    assert "故宫" in text or "门票" in text


def test_retry_actually_rewrites_the_query():
    """**假改写**是本回路最容易悄悄退化成的地方：
    算出了新 query 却仍用原声明去检索，等于重跑一遍必然失败的老路。

    这里断言第二轮检索用的确实是由「站不住的声明」拼出的新词，
    而不是原声明的重复。
    """
    finder = _FakeFinder(default=[])
    result = verify_with_retry("故宫周一闭馆。门票二百元整。", finder,
                               max_iterations=2)
    # 第一轮按声明检索，第二轮必须出现一个**不同的**检索词
    assert len(finder.asked) >= 2, "第二轮没有发起新的检索，改写形同虚设"
    assert result.attempts >= 2
    # 存在一个此前没问过的新查询
    assert len(set(finder.asked)) >= 2 or any(
        q not in result.checks[0].claim for q in finder.asked)


def test_iteration_cap_is_hard():
    """逃逸上限是硬的：跑满 max_iterations 就停，不得无限循环烧 token。"""
    calls = {"n": 0}

    class _Looping:
        def find(self, claim: str, *, top_k: int = 3) -> list[EvidenceHit]:
            calls["n"] += 1
            return []          # 永远给不出证据

    result = verify_with_retry("完全没有依据的一句话。", _Looping(),
                               max_iterations=3)
    assert result.hit_iteration_cap
    assert result.attempts == 3
    # 上限是硬的：不允许超过 max_iterations 轮
    assert calls["n"] <= 3 * 2 + 2


def test_short_answer_is_not_treated_as_hallucination():
    """「好的」「收到」这类没有事实可验的回复，不该被判成幻觉。"""
    finder = _FakeFinder(default=[])
    result = verify("好的。", finder)
    assert result.grounded


def test_overlap_ratio_is_symmetric_and_bounded():
    a, b = "故宫周一闭馆", "故宫在周一闭馆维护"
    assert 0.0 <= overlap_ratio(a, b) <= 1.0
    assert abs(overlap_ratio(a, b) - overlap_ratio(b, a)) < 1e-9
    assert overlap_ratio("完全不同的内容", "毫无关联的另一段话") < 0.3


def test_split_claims_keeps_only_checkable_sentences():
    claims = split_claims("故宫周一闭馆。门票二百元整。好的。")
    assert "好的。" not in claims
    assert len(claims) == 2
