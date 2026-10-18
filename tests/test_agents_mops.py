"""P4 Mops 多智能体集群测试。

计划书依据：三、P4「包含子 Agent：设备感知、天气感知、时间进度、增量抽取、路线方案、
备选预案、代码迭代 Agent」+「逐个开发、逐个单测、逐个上线；只读全局状态、仅写入自有字段、
无跨模块越权调用」+ 表注「隐私约束 / Wiki 约束」+ 触发时机
"""

from __future__ import annotations

import time

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_RETRIEVAL,
    CAP_WIKI_WRITE,
    CapabilityError,
    grant,
)
from ai_butler.agents.facts import FactWriter
from ai_butler.agents.mops import (
    MOPS_AGENT_CLASSES,
    ROSTER,
    CodeIterationAgent,
    ContingencyPlanAgent,
    DevicePerceptionAgent,
    IncrementalExtractionAgent,
    RoutePlanAgent,
    TimeProgressAgent,
    WeatherPerceptionAgent,
    build_mops_cluster,
    roster_manifest,
)
from ai_butler.agents.p4_mops import MopsClusterAgent
from ai_butler.agents.state import new_state

def _call(agent, state):
    """统一调用：Agent 的 run 可能是同步或异步（见 agents/base.resolve）。"""
    import asyncio

    from ai_butler.agents.base import resolve

    return asyncio.run(resolve(agent(state)))


def _run_phase(butler, code, state):
    import asyncio

    return asyncio.run(butler.run_phase(code, state))


#: 计划书三、P4 原文「包含子 Agent：设备感知、天气感知、时间进度、增量抽取、
#: 路线方案、备选预案、代码迭代 Agent」——「Agent」是七项共用的总称，
#: 与阶段命名同一口径（P1 就叫「数据中台」不叫「数据中台 Agent」），故不逐项带后缀。
PLAN_NAMES = ["设备感知", "天气感知", "时间进度", "增量抽取",
              "路线方案", "备选预案", "代码迭代"]


# ============================== 名册 ============================== #
def test_roster_matches_the_plan_line_word_for_word():
    """计划书三、P4 逐字点名的 7 个 Agent，顺序与名称都要对上。"""
    assert len(ROSTER) == 7
    assert [row[1] for row in ROSTER] == PLAN_NAMES
    assert [row[0] for row in ROSTER] == [c.name for c in MOPS_AGENT_CLASSES]


def test_every_member_writes_exactly_one_own_field():
    """计划书三、P4「仅写入自有字段」——一个成员只认领一个字段，避免互相踩。"""
    for (name, _, write_field), cls in zip(ROSTER, MOPS_AGENT_CLASSES):
        assert cls.writes == (write_field,), f"{name} 的写入字段与名册不一致"
        assert cls.responsibilities


def test_write_fields_are_unique_across_the_cluster():
    fields = [c.writes[0] for c in MOPS_AGENT_CLASSES]
    assert len(fields) == len(set(fields))


def test_roster_manifest_is_machine_readable():
    rows = roster_manifest()
    assert len(rows) == 7
    assert {r["plan_name"] for r in rows} == set(PLAN_NAMES)
    assert all(r["capabilities"] is not None for r in rows)


def test_cluster_members_are_business_subagents_not_phase_agents():
    """业务子 Agent 归 P3 管辖，不混进管家的阶段名册。"""
    from ai_butler.agents.phase import PhaseAgent
    for cls in MOPS_AGENT_CLASSES:
        assert issubclass(cls, SubAgent)
        assert not issubclass(cls, PhaseAgent)
        assert not hasattr(cls, "phase_code")


def test_build_mops_cluster_returns_seven_instances():
    cluster = build_mops_cluster()
    assert len(cluster) == 7
    assert {c.name for c in cluster} == {c.name for c in MOPS_AGENT_CLASSES}


# ============================== 桩件 ============================== #
class _StubDevice:
    def read_device_state(self, *, user_id, limit=200, metric=None):
        async def _inner():
            return [{"device_id": "dev-1", "metric": "battery", "value": "80"},
                    {"device_id": "dev-1", "metric": "location", "value": "杭州"},
                    {"device_id": "dev-2", "metric": "network", "value": "wifi"}]
        return _inner()          # 故意返回协程，检验 run_sync 的处理


class _Hit:
    def __init__(self, chunk_id, text, evidence, confidence=0.9, source_id="src-1"):
        self.chunk_id = chunk_id
        self.text = text
        self.evidence_ids = evidence
        self.confidence = confidence
        self.source_id = source_id
        self.doc_id = chunk_id.split("#")[0]
        self.page_id = "src-page"
        self.rerank_score = None
        self.bm25_score = 0.5
        self.vector_score = 0.8
        self.score = 0.9


class _Response:
    def __init__(self, hits):
        self.hits = hits
        self.count = len(hits)
        self.searched_scopes = ["public", "vault:user-A"]
        self.reranked = False


class _StubSearch:
    def __init__(self, hits):
        self._hits = hits

    def search(self, query, *, user_id, top_k=None):
        return _Response(list(self._hits))


@pytest.fixture()
def wiki(settings, workdir):
    return WikiStore(workdir / "wiki", settings=settings)


@pytest.fixture()
def evidence_pages(wiki):
    for i in (1, 2):
        wiki.write_page(new_page(EVIDENCE, f"ev-{i}", confidence=0.9, user_id="user-A",
                                 source_id="src-1", source_text=f"来源片段{i}",
                                 text_unit_id=f"tu-{i}"), user_id="user-A")
    return wiki


def _tools(cls, **available):
    return grant(cls, {k: v for k, v in available.items() if v is not None})


def _state(**extra):
    return new_state(user_id="user-A", session_id="s1", **extra)


# ============================== 1. 设备感知 ============================== #
def test_device_perception_summarises_the_accounts_devices():
    agent = DevicePerceptionAgent(tools=_tools(DevicePerceptionAgent, data_read=_StubDevice()))
    digest = _call(agent, _state())["device_snapshot"]
    assert digest["user_id"] == "user-A"
    assert digest["device_count"] == 2
    assert digest["metrics"]["battery"] == "80"


def test_device_perception_requires_data_read_capability():
    agent = DevicePerceptionAgent(tools=_tools(DevicePerceptionAgent))
    with pytest.raises(CapabilityError):
        _call(agent, _state())


# ============================== 2. 天气感知 ============================== #
def test_weather_perception_reports_scopes_and_evidence():
    search = _StubSearch([_Hit("d#0000", "明日天气晴朗", ["ev-1"])])
    agent = WeatherPerceptionAgent(tools=_tools(WeatherPerceptionAgent, retrieval=search))
    digest = agent(_state(weather_query="杭州天气"))["weather_snapshot"]
    assert digest["hits"] == 1 and digest["evidence_ids"] == ["ev-1"]
    assert digest["searched_scopes"] == ["public", "vault:user-A"]


# ============================== 3. 时间进度 ============================== #
def test_time_progress_computes_ratio_and_overdue():
    now = time.time()
    agent = TimeProgressAgent(tools=_tools(TimeProgressAgent))
    digest = agent(_state(now_epoch=now, task_started_epoch=now - 100,
                          deadline_epoch=now + 100))["time_progress"]
    assert digest["progress_ratio"] == pytest.approx(0.5, abs=0.02)
    assert digest["overdue"] is False

    late = agent(_state(now_epoch=now, task_started_epoch=now - 300,
                        deadline_epoch=now - 100))["time_progress"]
    assert late["overdue"] is True


def test_time_progress_handles_missing_context():
    digest = TimeProgressAgent(tools=_tools(TimeProgressAgent))(_state())["time_progress"]
    assert digest["progress_ratio"] is None
    assert digest["overdue"] is False


# ============================== 4. 增量抽取 ============================== #
def test_incremental_extraction_writes_evidence_bound_facts_into_private_vault(
        evidence_pages):
    """计划书三、P4 Wiki 约束：产出事实优先写入本人私有 Vault，绑定 Claim/Evidence ID。"""
    wiki = evidence_pages
    search = _StubSearch([_Hit("d#0000", "西湖春季六点开放", ["ev-1"], confidence=0.93)])
    agent = IncrementalExtractionAgent(tools=_tools(
        IncrementalExtractionAgent, retrieval=search, wiki_write=FactWriter(wiki)))
    result = agent(_state(utterance="西湖开放时间"))["extracted_facts"]

    assert result["facts"], "应产出绑定证据的事实"
    fact = result["facts"][0]
    assert fact["evidence_ids"] == ["ev-1"]
    assert fact["confidence"] == pytest.approx(0.93)
    assert wiki.read_page(fact["claim_page_id"], user_id="user-A") is not None
    assert wiki.read_page(fact["claim_page_id"], user_id="user-B") is None


def test_incremental_extraction_skips_hits_without_evidence(evidence_pages):
    """证据缺失的命中不得被硬凑成声明。"""
    search = _StubSearch([_Hit("d#0000", "无证据素材", []),
                          _Hit("d#0001", "无置信度素材", ["ev-1"], confidence=None)])
    agent = IncrementalExtractionAgent(tools=_tools(
        IncrementalExtractionAgent, retrieval=search,
        wiki_write=FactWriter(evidence_pages)))
    result = agent(_state(utterance="任意"))["extracted_facts"]
    assert result["facts"] == []
    assert len(result["skipped"]) == 2


def test_incremental_extraction_reports_which_snapshots_it_saw(evidence_pages):
    search = _StubSearch([])
    agent = IncrementalExtractionAgent(tools=_tools(
        IncrementalExtractionAgent, retrieval=search,
        wiki_write=FactWriter(evidence_pages)))
    state = _state(utterance="x")
    state["device_snapshot"] = {"device_count": 1}
    state["time_progress"] = {"overdue": False}
    result = agent(state)["extracted_facts"]
    assert result["observed"] == ["device_snapshot", "time_progress"]


# ============================== 5. 路线方案 ============================== #
def test_route_plan_produces_evidence_bound_options(evidence_pages):
    search = _StubSearch([_Hit("r#0000", "地铁 1 号线可直达", ["ev-1"])])
    agent = RoutePlanAgent(tools=_tools(RoutePlanAgent, retrieval=search,
                                        wiki_write=FactWriter(evidence_pages)))
    digest = agent(_state(route_query="去西湖怎么走"))["route_options"]
    assert digest["options"] and digest["options"][0]["evidence_ids"] == ["ev-1"]
    assert "P5" in digest["note"], "必须写明行程编排留给 P5，不越界"


# ============================== 6. 备选预案 ============================== #
def test_contingency_plan_does_not_invent_plans_without_a_disruption(evidence_pages):
    """没有被打破的前提就不产出备选——不凭空造预案。"""
    search = _StubSearch([_Hit("c#0000", "备用路线", ["ev-1"])])
    agent = ContingencyPlanAgent(tools=_tools(ContingencyPlanAgent, retrieval=search,
                                              wiki_write=FactWriter(evidence_pages)))
    digest = agent(_state())["contingency_plans"]
    assert digest["plans"] == [] and "未给出受阻前提" in digest["note"]


def test_contingency_plan_reacts_to_a_disruption(evidence_pages):
    search = _StubSearch([_Hit("c#0000", "雨天可改走室内展馆", ["ev-1"])])
    agent = ContingencyPlanAgent(tools=_tools(ContingencyPlanAgent, retrieval=search,
                                              wiki_write=FactWriter(evidence_pages)))
    digest = agent(_state(disruption="暴雨"))["contingency_plans"]
    assert digest["disruption"] == "暴雨" and digest["plans"], "有受阻前提才产出备选"


# ============================== 7. 代码迭代 ============================== #
def test_code_iteration_only_proposes_and_never_executes():
    """计划书三、P10 才允许在隔离沙箱里改代码；业务链路内严格止步于提案。"""
    assert CodeIterationAgent.EXECUTES_CODE is False
    state = _state()
    state["qa_report"] = {"findings": [{"kind": "证据缺失", "page_id": "c-1"}]}
    state["degradation"] = ["子 Agent 执行失败并回滚"]
    digest = CodeIterationAgent(tools=_tools(CodeIterationAgent))(state)[
        "code_iteration_proposals"]
    assert digest["executes_code"] is False
    assert digest["signal_count"] == 2
    assert len(digest["proposals"]) == 2
    assert "P10" in digest["note"]


def test_code_iteration_without_signals_proposes_nothing():
    digest = CodeIterationAgent(tools=_tools(CodeIterationAgent))(_state())[
        "code_iteration_proposals"]
    assert digest["proposals"] == []


# ============================== 通用约束 ============================== #
def test_every_member_skips_when_session_is_invalid():
    """计划书三、P4 触发时机「会话失效则跳过执行」。"""
    search = _StubSearch([])
    for cls in MOPS_AGENT_CLASSES:
        agent = cls(tools=_tools(cls, retrieval=search, data_read=_StubDevice(),
                                 wiki_write=FactWriter(WikiStore.__new__(WikiStore))))
        assert agent(_state(login_status=False)) == {}, f"{cls.name} 未跳过"


def test_every_member_refuses_blank_user_id():
    """计划书三、P4 隐私约束：user_id 不允许硬编码、不能为空。"""
    search = _StubSearch([])
    for cls in MOPS_AGENT_CLASSES:
        agent = cls(tools=_tools(cls, retrieval=search))
        with pytest.raises(Exception):
            agent(_state(user_id=""))


def test_every_member_only_writes_its_own_field(evidence_pages):
    search = _StubSearch([_Hit("d#0000", "素材", ["ev-1"])])
    for cls in MOPS_AGENT_CLASSES:
        agent = cls(tools=_tools(cls, retrieval=search, data_read=_StubDevice(),
                                 wiki_write=FactWriter(evidence_pages)))
        updates = _call(agent, _state(utterance="x", weather_query="x",
                                      route_query="x"))
        assert set(updates) <= set(cls.writes), f"{cls.name} 越界写入"


def test_every_member_reads_state_without_mutating_it():
    """计划书三、P4「只读全局状态」。"""
    search = _StubSearch([])
    for cls in MOPS_AGENT_CLASSES:
        agent = cls(tools=_tools(cls, retrieval=search, data_read=_StubDevice(),
                                 wiki_write=FactWriter(WikiStore.__new__(WikiStore))))
        state = _state(utterance="x")
        before = dict(state)
        _call(agent, state)
        assert state == before, f"{cls.name} 改动了全局状态"


# ============================== 阶段视图 ============================== #
def test_p4_phase_agent_reports_the_cluster():
    digest = MopsClusterAgent()(_state())["p4_digest"]
    assert digest["cluster_size"] == 7
    assert [r["plan_name"] for r in digest["roster"]] == PLAN_NAMES
    assert digest["dispatched_by"] == "p3_scene_scheduler", "业务子 Agent 由 P3 管辖"


def test_p4_is_now_marked_implemented():
    from ai_butler.core import phases
    spec = phases.phase("P4")
    assert spec.implemented is True
    assert spec.title == "Mops 多智能体集群｜感知与基础执行单元"
