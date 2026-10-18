"""P5 动态事件运营 Agent 测试。

计划书依据：三、P5 六项开发步骤 + 核心能力「重规划条件判断交由 Jev 高速决策，
条件成立才调用大模型生成」
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agents.base import resolve
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_JEV,
    CAP_LLM,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CapabilityError,
    grant,
)
from ai_butler.agents.p5_dynamic_event import (
    CONSTRAINT_DISRUPTION,
    CONSTRAINT_KINDS,
    CONSTRAINT_LOCATION,
    CONSTRAINT_PREFERENCE,
    CONSTRAINT_TIME,
    CONSTRAINT_WEATHER,
    DEFAULT_PLAN_PROMPT,
    MAX_SPEECH_CHUNKS,
    REPLAN,
    REPLAN_LABELS,
    REPLAN_SKIP,
    DynamicEventAgent,
    PlanConstraint,
    PlanningError,
    collect_constraints,
)
from ai_butler.agents.risk import (
    RISK_LOW_CONFIDENCE,
    RISK_MISSING_EVIDENCE,
    RISK_NONE,
    OutputItem,
    RiskAnnotator,
    RiskError,
    RiskPolicy,
    split_for_speech,
    stream_chunks,
)
from ai_butler.agents.state import new_state
from ai_butler.infra.llm_gateway import JevDecision
from ai_butler.scheduler.graph import MAX_ROLLBACKS


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


# ============================== 风险标注 ============================== #
def test_risk_policy_defaults_to_the_frozen_threshold():
    assert RiskPolicy().min_confidence == 0.82, "复用【八】的 0.82，不新造数字"


def test_risk_policy_and_item_validation():
    with pytest.raises(RiskError):
        RiskPolicy(min_confidence=1.5)
    with pytest.raises(RiskError):
        OutputItem(statement="   ")


def test_annotator_flags_low_confidence_and_missing_evidence():
    annotator = RiskAnnotator()
    safe = annotator.annotate_one(OutputItem("有证据高置信", 0.95, ("ev-1",)))
    low = annotator.annotate_one(OutputItem("低置信", 0.4, ("ev-2",)))
    bare = annotator.annotate_one(OutputItem("无出处", 0.99, ()))

    assert safe.risk == RISK_NONE and safe.safe is True
    assert low.risk == RISK_LOW_CONFIDENCE and "阈值" in low.risk_note
    assert bare.risk == RISK_MISSING_EVIDENCE and "溯源" in bare.risk_note


def test_annotator_never_rewrites_the_statement():
    """风险标注只加字段，**原文一字不改**——改文案等于替用户做判断。"""
    text = "西湖春季六点半开放，门票可预约。"
    out = RiskAnnotator().annotate_one(OutputItem(text, 0.1, ()))
    assert out.statement == text


def test_annotator_lists_only_risky_items():
    annotator = RiskAnnotator()
    items = annotator.annotate([OutputItem("好的", 0.99, ("ev-1",)),
                                OutputItem("差的", 0.2, ("ev-1",))])
    risks = annotator.risks(items)
    assert len(risks) == 1 and risks[0]["statement"] == "差的"


def test_banner_is_empty_when_nothing_is_risky():
    annotator = RiskAnnotator()
    safe_only = annotator.annotate([OutputItem("好", 0.99, ("ev-1",))])
    assert annotator.banner(safe_only) == "", "无风险时不应输出无意义的提示"


def test_banner_counts_and_names_the_risk_kinds():
    annotator = RiskAnnotator()
    items = annotator.annotate([OutputItem("a", 0.2, ("ev-1",)),
                                OutputItem("b", 0.9, ()),
                                OutputItem("c", 0.3, ("ev-2",))])
    banner = annotator.banner(items)
    assert "3 条" in banner
    assert "置信偏低" in banner and "缺少证据" in banner


def test_evidence_check_can_be_turned_off():
    annotator = RiskAnnotator(RiskPolicy(require_evidence=False))
    out = annotator.annotate_one(OutputItem("无出处但高置信", 0.99, ()))
    assert out.risk == RISK_NONE


# ============================== 流式分片 ============================== #
def test_split_prefers_semantic_boundaries():
    text = "第一段说明情况。第二段给出建议。第三段补充提示。"
    chunks = split_for_speech(text, chunk_chars=8)
    assert len(chunks) >= 2
    assert chunks[-1].is_last is True
    assert all(c.is_last is False for c in chunks[:-1])
    assert "".join(c.text for c in chunks).replace(" ", "") == text.replace(" ", "")


def test_split_hard_cuts_only_when_there_is_no_boundary():
    long_no_boundary = "西湖" * 60          # 没有任何断句符
    chunks = split_for_speech(long_no_boundary, chunk_chars=20)
    assert len(chunks) > 1, "无边界的长文本必须硬切，否则播报会卡住"


def test_split_empty_text_and_bad_size():
    assert split_for_speech("") == []
    assert split_for_speech("   ") == []
    with pytest.raises(RiskError):
        split_for_speech("x", chunk_chars=0)


def test_split_can_mark_risk_carrying_chunks():
    chunks = split_for_speech("注意。这段有风险。", carry_risk=True)
    assert all(c.carries_risk is True for c in chunks)


def test_stream_chunks_yields_in_order():
    chunks = split_for_speech("甲。乙。丙。", chunk_chars=2)
    assert [c.index for c in stream_chunks(chunks)] == list(range(len(chunks)))


# ============================== 约束收集 ============================== #
def test_constraint_kinds_match_the_plan_step_four():
    assert CONSTRAINT_KINDS == ("location", "weather", "time", "disruption",
                                "preference")


def test_constraint_validation():
    with pytest.raises(PlanningError):
        PlanConstraint("mood", "开心")
    with pytest.raises(PlanningError):
        PlanConstraint(CONSTRAINT_LOCATION, "  ")


def test_collect_constraints_only_uses_what_state_actually_has():
    """不臆造缺失的约束。"""
    assert collect_constraints({}) == []

    state = {"location": "杭州", "disruption": "暴雨",
             "weather_snapshot": {"query": "杭州天气"},
             "time_progress": {"remaining_seconds": 3600}}
    kinds = {c.kind for c in collect_constraints(state)}
    assert kinds == {CONSTRAINT_LOCATION, CONSTRAINT_WEATHER, CONSTRAINT_TIME,
                     CONSTRAINT_DISRUPTION}


def test_collect_constraints_ignores_malformed_snapshots():
    state = {"weather_snapshot": "不是字典", "time_progress": ["也不是"]}
    assert collect_constraints(state) == []


# ============================== 桩件 ============================== #
class _SDK:
    async def read_profile(self, *, user_id, limit=200):
        return [{"attribute": "偏好出行", "value": "高铁"}]


class _Page:
    def __init__(self, pid, description, claim_type):
        self.id = pid
        self.extra = {"description": description, "claim_type": claim_type}


class _Wiki:
    def __init__(self, entities=(), claims=()):
        self._entities, self._claims = list(entities), list(claims)

    def list_pages_for_retrieval(self, user_id, page_type=None):
        from ai_butler.agent_wiki.ontology import CLAIM, ENTITY
        return list(self._entities) if page_type == ENTITY else list(self._claims)


class _Hit:
    def __init__(self, text, evidence, confidence):
        self.text = text
        self.evidence_ids = evidence
        self.confidence = confidence
        self.page_id = "claim-1"


class _Resp:
    def __init__(self, hits):
        self.hits = hits
        self.count = len(hits)
        self.searched_scopes = ["public", "vault:user-A"]
        self.reranked = True


class _Search:
    def __init__(self, hits):
        self._hits = hits
        self.calls = 0

    def search(self, query, *, user_id, top_k=None):
        self.calls += 1
        return _Resp(list(self._hits))


class _Jev:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def decide(self, *, user_id, question, allowed_labels, model=None):
        self.calls += 1
        assert set(allowed_labels) <= set(REPLAN_LABELS)
        return self.decision


class _LLM:
    def __init__(self, text="生成的方案草案。"):
        self.text = text
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        return type("R", (), {"text": self.text})()


def _agent(**over):
    available = {
        CAP_DATA_READ: over.get("sdk", _SDK()),
        CAP_WIKI_READ: over.get("wiki", _Wiki()),
        CAP_RETRIEVAL: over.get("search", _Search([])),
        CAP_LLM: over.get("llm", _LLM()),
        CAP_JEV: over.get("jev", _Jev(JevDecision(REPLAN, 0.9, "jev"))),
    }
    return DynamicEventAgent(settings=over.get("settings"),
                             tools=grant(DynamicEventAgent, available))


def _state(**extra):
    return new_state(user_id="user-A", session_id="s1", **extra)


# ============================== Agent 行为 ============================== #
def test_agent_reads_profile_and_private_vault():
    wiki = _Wiki(entities=[_Page("ent-1", "偏好高铁", "preference")],
                 claims=[_Page("c-1", "偏好高铁出行", "preference")])
    digest = _call(_agent(wiki=wiki), _state(utterance="安排行程"))["p5_plan"]
    assert digest["profile_rows"] == 1
    assert digest["vault_entities"] == 1 and digest["vault_claims"] == 1
    kinds = {c["kind"] for c in digest["constraints"]}
    assert CONSTRAINT_PREFERENCE in kinds, "私有 Vault 的偏好声明应成为约束"


def test_llm_is_called_only_when_jev_says_replan():
    """核心经济性要求：**条件成立才调用大模型生成**。"""
    llm = _LLM()
    agent = _agent(llm=llm, jev=_Jev(JevDecision(REPLAN, 0.95, "jev")))
    digest = _call(agent, _state(utterance="安排行程",
                                 location="杭州"))["p5_plan"]
    assert digest["replan"]["required"] is True
    assert digest["generated_by_llm"] is True
    assert llm.calls == 1


def test_llm_is_not_called_when_jev_declines():
    llm = _LLM()
    agent = _agent(llm=llm, jev=_Jev(JevDecision(REPLAN_SKIP, 0.95, "jev")))
    digest = _call(agent, _state(utterance="安排行程"))["p5_plan"]
    assert digest["replan"]["required"] is False
    assert digest["generated_by_llm"] is False
    assert llm.calls == 0, "Jev 未放行时不得调用大模型"


def test_circuit_breaker_blocks_both_jev_and_llm():
    """计划书三、P5 步骤 5「接入 LangGraph 循环熔断」：达上限即不再触发昂贵调用。"""
    jev, llm = _Jev(JevDecision(REPLAN, 0.9, "jev")), _LLM()
    state = _state(utterance="安排行程")
    state["rollback_count"] = MAX_ROLLBACKS
    digest = _call(_agent(jev=jev, llm=llm), state)["p5_plan"]
    assert digest["circuit_broken"] is True
    assert jev.calls == 0 and llm.calls == 0
    assert "熔断" in digest["replan"]["reason"]


def test_agent_falls_back_to_the_previous_plan_when_not_regenerating():
    state = _state(utterance="看看")
    state["previous_plan"] = "上一版方案"
    digest = _call(_agent(jev=_Jev(JevDecision(REPLAN_SKIP, 0.9, "jev"))),
                   state)["p5_plan"]
    assert digest["draft"] == "上一版方案"


def test_agent_annotates_low_confidence_retrieval_as_risk():
    search = _Search([_Hit("高置信素材", ("ev-1",), 0.95),
                      _Hit("低置信素材", ("ev-2",), 0.30),
                      _Hit("无证据素材", (), 0.99)])
    digest = _call(_agent(search=search), _state(utterance="查一下"))["p5_plan"]
    risks = {r["risk"] for r in digest["risks"]}
    assert risks == {RISK_LOW_CONFIDENCE, RISK_MISSING_EVIDENCE}
    assert "2 条" in digest["risk_banner"]


def test_agent_emits_streaming_chunks_for_speech():
    """计划书三、P5 步骤 6「流式 Token 输出，支撑手机端逐字文字 + 语音同步播报」。"""
    llm = _LLM("第一句说明。第二句建议。第三句提示。")
    digest = _call(_agent(llm=llm), _state(utterance="安排行程"))["p5_plan"]
    assert digest["stream_ready"] is True
    assert digest["speech_chunks"], "必须给出可供移动端逐字渲染的分片"
    assert digest["speech_chunks"][-1]["is_last"] is True
    assert len(digest["speech_chunks"]) <= MAX_SPEECH_CHUNKS


def test_agent_reports_the_scopes_it_searched():
    digest = _call(_agent(), _state(utterance="查一下"))["p5_plan"]
    assert digest["searched_scopes"] == ["public", "vault:user-A"]


def test_agent_without_query_does_not_call_jev_or_llm():
    jev, llm = _Jev(JevDecision(REPLAN, 0.9, "jev")), _LLM()
    digest = _call(_agent(jev=jev, llm=llm), _state())["p5_plan"]
    assert digest["replan"]["required"] is False
    assert jev.calls == 0 and llm.calls == 0


def test_agent_requires_each_declared_capability():
    for missing in (CAP_DATA_READ, CAP_WIKI_READ, CAP_RETRIEVAL, CAP_LLM, CAP_JEV):
        available = {
            CAP_DATA_READ: _SDK(), CAP_WIKI_READ: _Wiki(), CAP_RETRIEVAL: _Search([]),
            CAP_LLM: _LLM(), CAP_JEV: _Jev(JevDecision(REPLAN, 0.9, "jev")),
        }
        available.pop(missing)
        agent = DynamicEventAgent(tools=grant(DynamicEventAgent, available))
        with pytest.raises(CapabilityError):
            _call(agent, _state(utterance="安排行程", location="杭州"))


def test_agent_writes_only_its_own_field():
    out = _call(_agent(), _state(utterance="查一下"))
    assert set(out) <= set(DynamicEventAgent.writes)


def test_agent_skips_when_session_is_invalid():
    assert _agent()(_state(login_status=False)) == {}


def test_prompt_carries_the_three_required_slots():
    for slot in ("{query}", "{constraints}", "{evidence}"):
        assert slot in DEFAULT_PLAN_PROMPT


def test_p5_is_now_marked_implemented():
    from ai_butler.core import phases
    spec = phases.phase("P5")
    assert spec.implemented is True
    assert spec.title == "动态事件运营 Agent｜核心智能方案生成引擎"
