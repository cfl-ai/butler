"""P4 多子 Agent 协同体系测试。

计划书依据：三、P4（只读全局状态/仅写自有字段/无跨模块越权调用、隐私约束、
触发时机、Wiki 约束、Agent 间不得直连）+ 五、5（旅游 Agent 权责）
+ 七、19（质检 Agent 依据证据做事实校验）
"""

from __future__ import annotations

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, SOURCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore, VaultIsolationError
from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import (
    CAPABILITIES,
    CAP_LLM,
    CAP_PAYMENT,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CAP_WIKI_WRITE,
    FORBIDDEN_FOR_SUBAGENTS,
    AgentTools,
    CapabilityError,
    assert_capabilities_within,
    declared_capabilities,
    grant,
)
from ai_butler.agents.facts import AgentFact, FactError, FactWriter
from ai_butler.agents.planner import PlannerAgent
from ai_butler.agents.qa import (
    CONTRADICTION,
    FINDING_KINDS,
    LOW_CONFIDENCE,
    MISSING_EVIDENCE,
    STALE_SOURCE,
    QualityAgent,
)
from ai_butler.agents.registry import AgentRegistry, RegistryError
from ai_butler.agents.state import new_state
from ai_butler.agents.triggers import (
    TriggerBus,
    TriggerError,
    TriggerRule,
    changed_fields,
)
from ai_butler.material_center.chunking import ChunkingRule
from ai_butler.material_center.embedding import HashingEmbedder
from ai_butler.material_center.retrieval import BM25Store, HybridRetriever
from ai_butler.material_center.service import SearchService
from ai_butler.material_center.vector_store import ChromaVectorStore


# ============================== 桩 Agent ============================== #
class _RetrievalAgent(SubAgent):
    name = "retrieval_agent"
    writes = ("retrieval_out",)
    capabilities = (CAP_RETRIEVAL,)
    responsibilities = "只声明检索能力"

    def __init__(self, tools=None):
        self.tools = tools

    def run(self, state):
        return {"retrieval_out": {"ok": True}}


class _GreedyAgent(SubAgent):
    name = "greedy_agent"
    writes = ("greedy_out",)
    capabilities = (CAP_RETRIEVAL, CAP_PAYMENT)      # 违规：垂类 Agent 不得持有支付能力

    def run(self, state):
        return {"greedy_out": {}}


class _NoNameAgent(SubAgent):
    name = ""
    writes = ("x",)

    def run(self, state):
        return {"x": 1}


class _NoWritesAgent(SubAgent):
    name = "no_writes_agent"
    writes = ()

    def run(self, state):
        return {}


# ============================== 能力 ============================== #
def test_capability_list_is_implementation_named_but_each_traces_to_the_plan():
    assert CAPABILITIES == ("retrieval", "wiki_read", "wiki_write", "llm", "jev",
                            "data_read", "payment")


def test_every_capability_constant_has_a_matching_tool_field():
    """回归锁定：CAPABILITIES 与 AgentTools 字段必须一一对应。

    曾经漏加 jev 字段，而 grant() 当时会**静默丢弃**没有字段的能力——
    Agent 直到运行期 require 时才炸，报错点离病因很远。
    """
    from dataclasses import fields as _fields

    declared = set(CAPABILITIES)
    actual = {f.name for f in _fields(AgentTools)} - {"granted"}
    assert declared == actual, f"能力常量与工具面字段不一致：{declared ^ actual}"


def test_grant_refuses_a_capability_without_a_tool_field():
    """声明了能力却没有字段时必须当场报错，而不是悄悄丢掉。"""
    from ai_butler.agents.capabilities import grant as _grant

    class _Orphan(SubAgent):
        name = "orphan_agent"
        writes = ("x",)
        capabilities = ("ghost_capability",)

        def run(self, state):
            return {}

    with pytest.raises(CapabilityError):
        _grant(_Orphan, {})


def test_grant_only_hands_over_declared_capabilities():
    marker = object()
    tools = grant(_RetrievalAgent, {CAP_RETRIEVAL: marker, CAP_LLM: object()})
    assert tools.has(CAP_RETRIEVAL) is True
    assert tools.has(CAP_LLM) is False, "未声明的能力不得出现在工具面上"
    assert tools.require(CAP_RETRIEVAL) is marker


def test_require_raises_on_undeclared_capability():
    tools = grant(_RetrievalAgent, {CAP_RETRIEVAL: object()})
    with pytest.raises(CapabilityError) as ei:
        tools.require(CAP_LLM)
    assert "无跨模块越权调用" in str(ei.value)


def test_require_raises_on_unknown_capability():
    with pytest.raises(CapabilityError):
        AgentTools(granted=("hack",)).require("hack")


def test_declared_but_unwired_capability_raises():
    tools = grant(_RetrievalAgent, {})               # 声明了但装配层没给实现
    assert tools.has(CAP_RETRIEVAL) is True
    with pytest.raises(CapabilityError) as ei:
        tools.require(CAP_RETRIEVAL)
    assert "未提供实现" in str(ei.value)


def test_unknown_declaration_is_rejected():
    class _Weird(SubAgent):
        name = "weird"
        writes = ("w",)
        capabilities = ("telepathy",)

        def run(self, state):
            return {}

    with pytest.raises(CapabilityError):
        grant(_Weird, {})


def test_subagents_may_not_hold_payment_capability():
    """统一架构规范 10.11：垂类 Agent 不得直接调用支付执行能力。"""
    assert CAP_PAYMENT in FORBIDDEN_FOR_SUBAGENTS
    with pytest.raises(CapabilityError) as ei:
        grant(_GreedyAgent, {})
    assert "支付 Agent 中转" in str(ei.value)


def test_declared_capabilities_and_within_check():
    assert declared_capabilities(_RetrievalAgent) == (CAP_RETRIEVAL,)
    assert_capabilities_within(_RetrievalAgent, (CAP_RETRIEVAL, CAP_LLM))
    with pytest.raises(CapabilityError):
        assert_capabilities_within(_RetrievalAgent, (CAP_LLM,))


# ============================== 注册表 ============================== #
def test_register_and_ledger():
    registry = AgentRegistry()
    spec = registry.register(_RetrievalAgent())
    assert spec.name == "retrieval_agent"
    assert spec.capabilities == (CAP_RETRIEVAL,)
    assert spec.responsibilities
    assert registry.names() == ("retrieval_agent",)
    assert registry.ledger()[0]["writes"] == ["retrieval_out"]


def test_registry_rejects_duplicate_name():
    """计划书三、P4「逐个上线」——同名 Agent 不得重复注册。"""
    registry = AgentRegistry()
    registry.register(_RetrievalAgent())
    with pytest.raises(RegistryError) as ei:
        registry.register(_RetrievalAgent())
    assert "名称重复" in str(ei.value)


def test_registry_rejects_agents_without_name_or_writes():
    registry = AgentRegistry()
    with pytest.raises(RegistryError):
        registry.register(_NoNameAgent())
    with pytest.raises(RegistryError) as ei:
        registry.register(_NoWritesAgent())
    assert "仅写入自有字段" in str(ei.value)


def test_registry_get_and_unregister():
    registry = AgentRegistry()
    registry.register(_RetrievalAgent())
    assert registry.get("retrieval_agent").name == "retrieval_agent"
    registry.unregister("retrieval_agent")
    assert registry.names() == ()
    with pytest.raises(RegistryError):
        registry.get("retrieval_agent")


def test_registry_tools_for_only_grants_what_the_agent_declared():
    registry = AgentRegistry()
    registry.register(_RetrievalAgent())
    tools = registry.tools_for("retrieval_agent", {CAP_RETRIEVAL: "svc", CAP_LLM: "llm"})
    assert tools.granted == (CAP_RETRIEVAL,)
    with pytest.raises(CapabilityError):
        tools.require(CAP_LLM)


def test_registry_has_no_agent_to_agent_dispatch_entry():
    """计划书三、P4：Agent 之间不得直接互相传递任务，必须走调度层。

    这是**结构性**保证：注册表里根本没有那个方法，而不是靠文档约定。
    """
    public = {n for n in dir(AgentRegistry) if not n.startswith("_")}
    forbidden = {"dispatch", "call", "invoke", "forward", "delegate", "run_agent",
                 "send_to", "route", "set_router", "add_route", "chain"}
    assert not (public & forbidden), f"注册表出现了 Agent 互调入口：{public & forbidden}"


def test_base_agent_still_exposes_no_dispatch_surface():
    public = {n for n in dir(SubAgent) if not n.startswith("_")}
    assert not (public & {"dispatch", "call_agent", "send_to_agent", "delegate"})


# ============================== 产出事实 ============================== #
def test_fact_requires_claim_and_evidence_binding():
    """计划书三、P4：输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID。"""
    base = dict(statement="x", claim_page_id="c", evidence_ids=("e",), confidence=0.9,
                subject_id="s", object_id="o")

    with pytest.raises(FactError) as ei:
        AgentFact(**{**base, "claim_page_id": ""})
    assert "Claim 声明 ID" in str(ei.value)

    with pytest.raises(FactError) as ei2:
        AgentFact(**{**base, "evidence_ids": ()})
    assert "Evidence 证据 ID" in str(ei2.value)

    with pytest.raises(FactError):
        AgentFact(**{**base, "statement": ""})
    with pytest.raises(FactError):
        AgentFact(**{**base, "confidence": 1.5})

    # 本体要求 Claim 说明它关于什么（U17）：subject/object 不得留空
    with pytest.raises(FactError) as ei3:
        AgentFact(**{**base, "subject_id": ""})
    assert "关于什么" in str(ei3.value)


def test_fact_deduplicates_evidence_ids():
    fact = AgentFact(statement="x", claim_page_id="c",
                     evidence_ids=("e1", "e1", "e2"), confidence=0.9,
                     subject_id="s", object_id="o")
    assert fact.evidence_ids == ("e1", "e2")


@pytest.fixture()
def wiki(settings, workdir):
    return WikiStore(workdir / "wiki", settings=settings)


def test_fact_writer_requires_user_id(wiki):
    writer = FactWriter(wiki)
    fact = AgentFact(statement="x", claim_page_id="c", evidence_ids=("e",), confidence=0.9,
                     subject_id="s", object_id="o")
    for bad in ("", "   "):
        with pytest.raises(FactError) as ei:
            writer.write(fact, user_id=bad)
        assert "隐私约束" in str(ei.value)


def test_fact_writer_refuses_missing_evidence(wiki):
    writer = FactWriter(wiki)
    fact = AgentFact(statement="x", claim_page_id="c", evidence_ids=("ev-ghost",),
                     confidence=0.9, subject_id="s", object_id="o")
    with pytest.raises(FactError) as ei:
        writer.write(fact, user_id="user-A")
    assert "证据条目不存在" in str(ei.value)


def test_fact_writer_writes_into_the_private_vault(wiki):
    """计划书三、P4：产出事实**优先写入对应用户私有 Vault**。"""
    wiki.write_page(new_page(EVIDENCE, "ev-1", confidence=0.9, user_id="user-A",
                             source_id="src-1", source_text="来源片段", text_unit_id="tu-1"),
                    user_id="user-A")
    writer = FactWriter(wiki)
    page = writer.write(AgentFact(statement="西湖春季最佳", claim_page_id="claim-1",
                                  evidence_ids=("ev-1",), confidence=0.93,
                                  subject_id="src-1", object_id="tu-1"),
                        user_id="user-A")

    assert page.type == CLAIM and page.user_id == "user-A"
    assert page.extra["evidence_ids"] == ["ev-1"]
    assert wiki.read_page("claim-1", user_id="user-A") is not None
    assert wiki.read_page("claim-1", user_id="user-B") is None, "不得写进他人 Vault"


def test_fact_writer_accepts_public_evidence_but_still_writes_private(wiki):
    """素材溯源库属公共 Workspace，证据可以来自公共库，但事实仍写私有 Vault。"""
    wiki.write_page(new_page(EVIDENCE, "ev-pub", confidence=0.9,
                             source_id="src-pub", source_text="公共来源",
                             text_unit_id="tu-pub"))
    writer = FactWriter(wiki)
    page = writer.write(AgentFact(statement="公共事实", claim_page_id="claim-pub-fact",
                                 evidence_ids=("ev-pub",), confidence=0.9,
                                 subject_id="src-pub", object_id="tu-pub"),
                        user_id="user-A")
    assert page.user_id == "user-A"


def test_fact_writer_refuses_to_rewrite_another_accounts_claim(wiki):
    wiki.write_page(new_page(CLAIM, "claim-x", confidence=0.9, user_id="user-B",
                             claim_type="fact", description="B 的声明",
                             subject_id="a", object_id="b", evidence_ids=["ev-1"]),
                    user_id="user-B")
    writer = FactWriter(wiki)
    fact = AgentFact(statement="冒充", claim_page_id="claim-x", evidence_ids=("ev-1",),
                     confidence=0.9, subject_id="s", object_id="o")
    with pytest.raises(FactError):
        writer.write(fact, user_id="user-A")


def test_fact_writer_enforces_vault_isolation_on_write(wiki):
    """即便绕过校验，WikiStore 的隔离仍是最后一道闸。"""
    writer = FactWriter(wiki, require_evidence_pages=False)
    fact = AgentFact(statement="x", claim_page_id="c", evidence_ids=("e",), confidence=0.9,
                     subject_id="s", object_id="o")
    page = writer.write(fact, user_id="user-A")
    with pytest.raises(VaultIsolationError):
        wiki.write_page(page)                      # 私有页写进公共库


# ============================== 触发时机 ============================== #
def test_changed_fields_detects_differences():
    assert changed_fields({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ("b",)
    assert changed_fields({"a": 1}, {"a": 2, "c": 3}, ignore=("a",)) == ("c",)
    assert changed_fields({"a": 1}, {"a": 1}) == ()


def _bus(registry=None):
    registry = registry or AgentRegistry()
    registry.register(_RetrievalAgent())
    registry.register(QualityAgent(tools=None))
    return TriggerBus(registry=registry, rules=[
        TriggerRule(watch=("utterance",), agents=("retrieval_agent",), note="新请求"),
        TriggerRule(watch=("qa_report",), agents=("qa_agent",)),
    ], ignore_fields=("tool_call_history",))


def test_trigger_plan_fires_only_affected_agents():
    bus = _bus()
    plan = bus.plan(before={"utterance": "旧"}, after={"utterance": "新"})
    assert plan.fired == ("retrieval_agent",)
    assert plan.changed == ("utterance",)
    assert any("新请求" in r for r in plan.reasons)


def test_trigger_plan_is_empty_when_nothing_changed():
    bus = _bus()
    plan = bus.plan(before={"utterance": "同"}, after={"utterance": "同"})
    assert plan.is_empty and plan.fired == ()


def test_trigger_skips_everything_when_session_invalid():
    """计划书三、P4：会话失效则跳过执行。"""
    bus = _bus()
    plan = bus.plan(before={"utterance": "旧"}, after={"utterance": "新"},
                    login_status=False)
    assert plan.fired == ()
    assert plan.skipped == ("retrieval_agent",)
    assert any("会话失效" in r for r in plan.reasons)


def test_trigger_rule_validation_and_unknown_agent():
    with pytest.raises(TriggerError):
        TriggerRule(watch=(), agents=("a",))
    with pytest.raises(TriggerError):
        TriggerRule(watch=("a",), agents=())
    registry = AgentRegistry()
    registry.register(_RetrievalAgent())
    with pytest.raises(RegistryError):
        TriggerBus(registry=registry,
                   rules=[TriggerRule(watch=("x",), agents=("ghost",))])


# ============================== 规划 Agent ============================== #
@pytest.fixture()
def rig(settings, workdir, wiki):
    retriever = HybridRetriever(
        vector_store=ChromaVectorStore(workdir / "chroma", collection="p4_materials"),
        bm25_store=BM25Store(workdir / "bm25"), embedder=HashingEmbedder(dim=64))
    service = SearchService(retriever=retriever,
                            chunking=ChunkingRule(max_chars=200, overlap_chars=0,
                                                  min_chars=1))
    return wiki, service, FactWriter(wiki)


def _state(user_id="user-A", utterance="西湖春季游览"):
    return new_state(user_id=user_id, session_id="s1", utterance=utterance)


def test_planner_produces_evidence_bound_facts_into_vault(rig):
    wiki, service, writer = rig
    wiki.write_page(new_page(EVIDENCE, "ev-1", confidence=0.9, user_id="user-A",
                             source_id="src-1", source_text="来源", text_unit_id="tu-1"),
                    user_id="user-A")
    service.index_text(doc_id="doc-1", text="西湖春季是最佳游览季节，苏堤春晓尤为出名。",
                       confidence=0.93, evidence_ids=["ev-1"], page_id="src-1")

    agent = PlannerAgent(tools=grant(PlannerAgent, {CAP_RETRIEVAL: service,
                                                    CAP_WIKI_WRITE: writer}))
    out = agent(_state())["planner_digest"]

    assert out["user_id"] == "user-A"
    assert out["facts"], "应产出带证据绑定的事实"
    fact = out["facts"][0]
    assert fact["evidence_ids"] == ["ev-1"]
    assert fact["confidence"] == pytest.approx(0.93)
    assert wiki.read_page(fact["claim_page_id"], user_id="user-A") is not None
    assert wiki.read_page(fact["claim_page_id"], user_id="user-B") is None


def test_planner_skips_hits_without_evidence_or_confidence(rig):
    """计划书三、P2「强制所有素材绑定溯源来源」——无证据不得产出事实。"""
    wiki, service, writer = rig
    service.index_text(doc_id="doc-noev", text="没有绑定证据的素材内容片段。")   # 无证据/无置信
    agent = PlannerAgent(tools=grant(PlannerAgent, {CAP_RETRIEVAL: service,
                                                    CAP_WIKI_WRITE: writer}))
    out = agent(_state(utterance="没有绑定证据的素材"))["planner_digest"]
    assert out["facts"] == []
    assert out["skipped"], "被跳过的原因必须显式记录，不得静默丢弃"


def test_planner_requires_user_id_and_retrieval_capability(rig):
    _, service, writer = rig
    agent = PlannerAgent(tools=grant(PlannerAgent, {CAP_RETRIEVAL: service,
                                                    CAP_WIKI_WRITE: writer}))
    with pytest.raises(Exception):
        agent({"user_id": "", "session_id": "s", "login_status": True,
               "jev_decision_result": None, "wiki_digest": None, "utterance": "x"})

    bare = PlannerAgent(tools=grant(PlannerAgent, {CAP_RETRIEVAL: service}))
    with pytest.raises(CapabilityError):
        bare(_state())


def test_planner_reports_searched_scopes(rig):
    _, service, writer = rig
    service.index_text(doc_id="d", text="西湖春季游览的最佳时段。")
    agent = PlannerAgent(tools=grant(PlannerAgent, {CAP_RETRIEVAL: service,
                                                    CAP_WIKI_WRITE: writer}))
    out = agent(_state())["planner_digest"]
    assert out["searched_scopes"] == ["public", "vault:user-A"]


# ============================== 质检 Agent ============================== #
def test_qa_finding_kinds_match_plan_item_19():
    """计划书七、19 点名的四类：矛盾、证据缺失、过期素材 + 置信度。"""
    assert FINDING_KINDS == ("证据缺失", "矛盾声明", "过期素材", "低置信声明")


def test_qa_detects_all_four_kinds(wiki, settings):
    wiki.write_page(new_page(EVIDENCE, "ev-1", confidence=0.9, user_id="user-A",
                             source_id="s", source_text="t", text_unit_id="u"),
                    user_id="user-A")
    wiki.write_page(new_page(CLAIM, "c-missing", confidence=0.9, user_id="user-A",
                             claim_type="fact", description="无证据", subject_id="a",
                             object_id="b", evidence_ids=[]), user_id="user-A")
    wiki.write_page(new_page(CLAIM, "c-contra", confidence=0.9, user_id="user-A",
                             claim_type="fact", description="矛盾", subject_id="a",
                             object_id="b", evidence_ids=["ev-1"],
                             contradicts=["c-other"]), user_id="user-A")
    wiki.write_page(new_page(CLAIM, "c-low", confidence=0.3, user_id="user-A",
                             claim_type="fact", description="低置信", subject_id="a",
                             object_id="b", evidence_ids=["ev-1"]), user_id="user-A")
    wiki.write_page(new_page(SOURCE, "s-stale", status="archived", url="https://e.com"),
                    )

    agent = QualityAgent(tools=grant(QualityAgent, {CAP_WIKI_READ: wiki}),
                         settings=settings)
    report = agent(_state())["qa_report"]

    kinds = {f["kind"] for f in report["findings"]}
    assert kinds == set(FINDING_KINDS)
    assert report["by_kind"][MISSING_EVIDENCE] == 1
    assert report["by_kind"][CONTRADICTION] == 1
    assert report["by_kind"][LOW_CONFIDENCE] == 1
    assert report["by_kind"][STALE_SOURCE] == 1
    assert report["passed"] is False
    assert report["threshold"] == settings.jev_confidence_threshold


def test_qa_passes_on_clean_knowledge_base(wiki, settings):
    wiki.write_page(new_page(EVIDENCE, "ev-ok", confidence=0.9, user_id="user-A",
                             source_id="s", source_text="t", text_unit_id="u"),
                    user_id="user-A")
    wiki.write_page(new_page(CLAIM, "c-ok", confidence=0.95, user_id="user-A",
                             claim_type="fact", description="有证据高置信", subject_id="a",
                             object_id="b", evidence_ids=["ev-ok"]), user_id="user-A")
    agent = QualityAgent(tools=grant(QualityAgent, {CAP_WIKI_READ: wiki}),
                         settings=settings)
    report = agent(_state())["qa_report"]
    assert report["passed"] is True and report["findings"] == []
    assert report["claims_checked"] == 1


def test_qa_never_reads_another_accounts_vault(wiki, settings):
    wiki.write_page(new_page(CLAIM, "c-b", confidence=0.1, user_id="user-B",
                             claim_type="fact", description="B 的低置信声明",
                             subject_id="a", object_id="b", evidence_ids=[]),
                    user_id="user-B")
    agent = QualityAgent(tools=grant(QualityAgent, {CAP_WIKI_READ: wiki}),
                         settings=settings)
    report = agent(_state(user_id="user-A"))["qa_report"]
    assert report["claims_checked"] == 0
    assert all(f["page_id"] != "c-b" for f in report["findings"])


def test_qa_requires_wiki_read_capability(wiki, settings):
    agent = QualityAgent(tools=grant(QualityAgent, {}), settings=settings)
    with pytest.raises(CapabilityError):
        agent(_state())


# ------------------------------ 风控：不可信输入不得沉淀为事实 ------------------------------ #
def test_untrusted_input_cannot_become_a_confirmed_claim(wiki):
    """不可信输入产出的事实只能落成 SUSPECTED，**不能是 TRUE**。

    这一条是「不得直接沉淀为事实」的**可执行定义**。
    此前 input_untrusted 只传到状态和结果，**没有任何消费者** ——
    判定 → 传递 → 无人执行，约束等于没做。
    """
    from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, new_page
    from ai_butler.agents.facts import AgentFact, FactWriter

    wiki.write_page(new_page(EVIDENCE, "ev-x", title="来源", source_id="src-1",
                                        source_text="原文片段", text_unit_id="tu-1"))
    fact = AgentFact(statement="明天闭馆", claim_page_id="claim-x",
                     evidence_ids=("ev-x",), confidence=0.9,
                     subject_id="ev-x", object_id="chunk-x")

    page = FactWriter(wiki).write(fact, user_id="user-A", untrusted=True)
    assert page.status == "SUSPECTED", f"不可信输入不得落成 {page.status!r}"
    assert page.extra.get("untrusted_input") is True


def test_trusted_input_still_becomes_a_confirmed_claim(wiki):
    """可信输入不受影响 —— 否则正常业务无法沉淀事实。"""
    from ai_butler.agent_wiki.ontology import EVIDENCE, new_page
    from ai_butler.agents.facts import AgentFact, FactWriter

    wiki.write_page(new_page(EVIDENCE, "ev-y", title="来源", source_id="src-2",
                                        source_text="原文片段", text_unit_id="tu-2"))
    fact = AgentFact(statement="西湖在杭州", claim_page_id="claim-y",
                     evidence_ids=("ev-y",), confidence=0.95,
                     subject_id="ev-y", object_id="chunk-y")
    page = FactWriter(wiki).write(fact, user_id="user-A", untrusted=False)
    assert page.status == "TRUE"
    assert not page.extra.get("untrusted_input")
