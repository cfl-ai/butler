"""P3 上下文分项计量与 shadowing 压缩守护测试（裁定 U58）。

覆盖：
- shadowing：被压条目只让位不删除（影子带脱敏原文，可重建）；
  让位原因分档（超条数 / 超预算）
- 分项计量：source / scope / 相关度分带 + pressure
- token 口径：默认估算器如实标注 estimated，注入计数器即生效
- 脱敏纪律不回归：先脱敏后裁剪，影子里的原文同样是脱敏后的
- 接线：loop.run(journal=) 落 EV_CONTEXT_SHADOW；U58 登记
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.core import decisions
from ai_butler.core.event_journal import (
    EV_CONTEXT_SHADOW,
    EventJournal,
    MemoryJournalStore,
)
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.context import (
    SHADOW_MAX_ITEMS,
    SHADOW_TOKEN_BUDGET,
    CharHeuristicCounter,
    ContextBreakdown,
    ContextError,
    ContextItem,
    ShadowedItem,
    compress,
)
from ai_butler.harness.hooks import HookChain
from ai_butler.harness.loop import AgenticLoop, Proposal
from ai_butler.harness.sandbox import SandboxResult


def _redactor() -> Redactor:
    return Redactor()


# ------------------------------ shadowing ------------------------------ #

def test_shadowed_items_are_not_deleted_but_restorable():
    """被压条目只让位：影子带内容，restore_shadowed() 可重建。"""
    items = [ContextItem(f"i{i}", f"内容 {i}", relevance=float(i) / 10)
             for i in range(6)]
    got = compress(items, redactor=_redactor(), max_items=2)
    assert len(got.items) == 2
    assert got.dropped == 4
    assert len(got.shadowed) == 4
    assert all(s.reason == SHADOW_MAX_ITEMS for s in got.shadowed)
    restored = got.restore_shadowed()
    assert {r.item_id for r in restored} == {f"i{i}" for i in range(4)}
    # 影子文本与入账前一致（脱敏后）——「压掉」可逆
    assert all(r.text.startswith("内容") for r in restored)


def test_shadow_records_reason_and_tokens():
    items = [ContextItem("a", "很长的一段内容" * 10, relevance=1.0),
             ContextItem("b", "短内容", relevance=0.9)]
    got = compress(items, redactor=_redactor(), max_items=1)
    assert len(got.shadowed) == 1
    s = got.shadowed[0]
    assert s.reason == SHADOW_MAX_ITEMS
    assert s.item_id == "b"
    assert s.estimated_tokens > 0
    assert s.as_dict()["reason"] == SHADOW_MAX_ITEMS


def test_token_budget_shadows_overflow_items():
    """token 预算裁剪：超预算的让位，原因记录为 token_budget。"""
    big = "内容" * 100            # 约 100 token（CJK≈1/字）
    items = [ContextItem("a", big, relevance=1.0),
             ContextItem("b", big, relevance=0.9)]
    got = compress(items, redactor=_redactor(), max_items=10, token_budget=120)
    assert len(got.items) == 1
    assert len(got.shadowed) == 1
    assert got.shadowed[0].reason == SHADOW_TOKEN_BUDGET


def test_token_budget_keeps_at_least_one_item():
    """空上下文比超预算更糟：预算再小也至少留一条。"""
    items = [ContextItem("a", "内容" * 100, relevance=1.0)]
    got = compress(items, redactor=_redactor(), max_items=10, token_budget=1)
    assert len(got.items) == 1
    assert got.dropped == 0


def test_rejects_bad_token_budget():
    with pytest.raises(ContextError):
        compress([], redactor=_redactor(), token_budget=0)


# ------------------------------ 分项计量 ------------------------------ #

def test_breakdown_tallies_by_source_scope_and_band():
    items = [
        ContextItem("a", "甲", source="wiki", scope="public", relevance=0.9),
        ContextItem("b", "乙", source="log", scope="vault", relevance=0.5),
        ContextItem("c", "丙", source="wiki", scope="public", relevance=0.1),
    ]
    got = compress(items, redactor=_redactor(), max_items=10)
    bd = got.breakdown
    assert bd.total_items == 3
    assert bd.by_source == {"wiki": 2, "log": 1}
    assert bd.by_scope == {"public": 2, "vault": 1}
    assert bd.by_relevance_band == {"high": 1, "mid": 1, "low": 1}


def test_pressure_is_occupancy_over_budget():
    items = [ContextItem("a", "内容" * 50, relevance=1.0)]   # 100 字≈100 token
    got = compress(items, redactor=_redactor(), token_budget=200)
    assert got.breakdown.pressure == pytest.approx(100 / 200)
    # 超预算时压力封顶 1.0（不输出 >1 的无意义值）
    got2 = compress(items, redactor=_redactor(), token_budget=50)
    assert got2.breakdown.pressure == 1.0


def test_pressure_is_zero_without_budget():
    items = [ContextItem("a", "内容", relevance=1.0)]
    got = compress(items, redactor=_redactor())
    assert got.breakdown.pressure == 0.0
    assert got.breakdown.token_budget is None


def test_breakdown_as_dict_is_complete():
    got = compress([ContextItem("a", "内容", relevance=1.0)],
                   redactor=_redactor(), token_budget=10)
    d = got.breakdown.as_dict()
    for key in ("total_items", "by_source", "by_scope", "by_relevance_band",
                "estimated_tokens", "token_budget", "counter_estimated",
                "pressure"):
        assert key in d


# ------------------------------ token 口径 ------------------------------ #

def test_default_counter_is_estimator_and_says_so():
    c = CharHeuristicCounter()
    assert c.estimated is True
    assert c.count("内容内容") == 4            # 4 个汉字≈4 token（CJK≈1/字）
    assert c.count("abcd") == 1                # 4 字符≈1 token
    assert c.count("") == 0
    got = compress([ContextItem("a", "内容", relevance=1.0)], redactor=_redactor())
    assert got.breakdown.counter_estimated is True


def test_injected_counter_overrides_estimation():
    class Exact:
        estimated = False

        def count(self, text):
            return len(text) * 10

    got = compress([ContextItem("a", "内容内容", relevance=1.0)],
                   redactor=_redactor(), counter=Exact())
    assert got.breakdown.estimated_tokens == 40
    assert got.breakdown.counter_estimated is False


def test_summary_tokens_are_folded_into_breakdown():
    got = compress([ContextItem("a", "内容" * 10, relevance=1.0)],
                   redactor=_redactor(), summarizer=lambda t: "摘要" * 10)
    # 「内容」*10 = 20 字≈20 token；「摘要」*10 = 20 字≈20 token；同一口径合计 40
    assert got.breakdown.estimated_tokens == 40


# ------------------------------ 脱敏纪律不回归 ------------------------------ #

def test_shadow_text_is_redacted():
    """影子里的原文也必须是脱敏后的——影子不是隐私旁路。"""
    items = [ContextItem("a", "用户 user-A 手机 13812345678", relevance=1.0),
             ContextItem("b", "普通内容", relevance=0.5)]
    got = compress(items, redactor=_redactor(), max_items=1)
    assert len(got.shadowed) == 1
    blob = got.shadowed[0].text
    assert "13812345678" not in blob and "user-A" not in blob


# ------------------------------ 账本接线 ------------------------------ #

def test_loop_journal_records_context_shadow():
    class _FakeSandbox:
        def run(self, command, timeout=30):
            return SandboxResult(command=command, exit_code=0)

    async def _decide(_ctx, _hist):
        return Proposal("修复完成", done=True)

    loop_obj = AgenticLoop(decide=_decide,
                           hooks=HookChain.default(qa_verdict={"action": "pass"}),
                           sandbox=_FakeSandbox())
    j = EventJournal("s-ctx", MemoryJournalStore())
    items = [ContextItem(f"i{i}", f"内容 {i}", relevance=float(i) / 10)
             for i in range(6)]
    asyncio.run(loop_obj.run(goal="修复", items=items, redactor=_redactor(),
                             max_items=2, journal=j))
    shadows = [e for e in j.events if e.type == EV_CONTEXT_SHADOW]
    assert len(shadows) == 1
    payload = shadows[0].payload
    assert payload["kept"] == 2 and payload["shadowed_count"] == 4
    assert payload["shadowed"][0]["reason"] == SHADOW_MAX_ITEMS
    # 崩溃恢复后能知道压掉了什么：影子内容在账本里
    assert any(s["item_id"].startswith("i") for s in payload["shadowed"])
    j.verify()


# ------------------------------ 台账 ------------------------------ #

def test_u58_is_registered_on_p10():
    keys = {d.key for d in decisions.DECISIONS if d.phase == "P10"}
    assert "U58" in keys
