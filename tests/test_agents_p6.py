"""P6 内容质检 Agent 测试。

计划书依据：三、P6 六项开发步骤 + 触发时机「方案生成、账单生成、数据入库前强制校验」
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, QUESTION, SOURCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.agents.base import resolve
from ai_butler.agents.capabilities import (
    CAP_JEV,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CapabilityError,
    grant,
)
from ai_butler.agents.p6_content_qa import (
    ACTION_BLOCK,
    ACTION_DOWNGRADE,
    ACTION_PASS,
    ACTIONS,
    CONFLICT_COST,
    CONFLICT_KINDS,
    CONFLICT_LOGIC,
    CONFLICT_SPACE,
    CONFLICT_TIME,
    CREDIBILITY_HIGH,
    CREDIBILITY_LABELS,
    FINDING_CONTRADICTION,
    FINDING_LOW_CONFIDENCE,
    FINDING_MISSING_EVIDENCE,
    FINDING_UNSUPPORTED,
    KIND_BILL,
    KIND_INGEST,
    KIND_PLAN,
    MAX_REGENERATIONS,
    PRIVACY_CROSS_USER_REFERENCE,
    PRIVACY_DANGLING_REFERENCE,
    PRIVACY_KINDS,
    PRIVACY_OTHER_ACCOUNT_MARK,
    ContentQaAgent,
    ContentVerifier,
    Proposition,
    QaError,
    collect_propositions,
    detect_conflicts,
    detect_cost_conflicts,
    detect_logic_conflicts,
    detect_space_conflicts,
    detect_time_conflicts,
    extract_propositions,
)
from ai_butler.agents.state import new_state
from ai_butler.core.storage import FileStore
from ai_butler.data_platform.governance import ReviewQueue
from ai_butler.infra.llm_gateway import JevDecision


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


# ============================== 契约 ============================== #
def test_constants_match_the_plan_steps():
    assert CONFLICT_KINDS == ("time", "space", "logic", "cost")
    assert ACTIONS == ("pass", "downgrade", "block")
    assert MAX_REGENERATIONS == 2, "计划书三、P6 步骤5 写的是「二次」重生成"
    assert CREDIBILITY_LABELS == ("high", "medium", "low")
    assert PRIVACY_KINDS == (PRIVACY_CROSS_USER_REFERENCE, PRIVACY_OTHER_ACCOUNT_MARK,
                             PRIVACY_DANGLING_REFERENCE)


def test_proposition_validation_and_references():
    with pytest.raises(QaError):
        Proposition(statement="  ")
    with pytest.raises(QaError):
        Proposition(statement="x", kind="poem")
    prop = Proposition(statement="x", claim_page_id="c-1", evidence_ids=("ev-1", "ev-2"))
    assert prop.references == ("c-1", "ev-1", "ev-2")


def test_extract_propositions_splits_on_sentence_boundaries():
    props = extract_propositions("西湖六点半开放。门票六十元！请提前预约？")
    assert [p.statement for p in props] == ["西湖六点半开放", "门票六十元", "请提前预约"]
    assert all(p.kind == KIND_PLAN for p in props)


# ============================== 冲突检测 ============================== #
def test_time_conflict_needs_same_subject_with_two_times():
    conflict = detect_time_conflicts([
        Proposition(statement="六点半开放", subject_id="e:xihu"),
        Proposition(statement="七点开放", subject_id="e:xihu")])
    assert len(conflict) == 1 and conflict[0]["kind"] == CONFLICT_TIME
    assert conflict[0]["values"] == ["06:30", "07:00"]

    assert detect_time_conflicts([
        Proposition(statement="六点半开放", subject_id="e:a"),
        Proposition(statement="七点开放", subject_id="e:b")]) == []


def test_cost_conflict_detects_two_amounts_for_one_subject():
    conflict = detect_cost_conflicts([
        Proposition(statement="门票 60 元", subject_id="e:xihu"),
        Proposition(statement="门票 80 元", subject_id="e:xihu")])
    assert len(conflict) == 1 and conflict[0]["kind"] == CONFLICT_COST
    assert set(conflict[0]["values"]) == {"60", "80"}


def test_space_conflict_only_for_location_semantics():
    conflict = detect_space_conflicts([
        Proposition(statement="入口位于北门", subject_id="e:xihu", object_id="north"),
        Proposition(statement="入口位于南门", subject_id="e:xihu", object_id="south")])
    assert len(conflict) == 1 and conflict[0]["kind"] == CONFLICT_SPACE

    assert detect_space_conflicts([
        Proposition(statement="很热闹", subject_id="e:xihu", object_id="north"),
        Proposition(statement="很不错", subject_id="e:xihu", object_id="south")]) == []


def test_logic_conflict_reuses_wiki_contradiction_records(settings, workdir):
    wiki = WikiStore(workdir / "wiki", settings=settings)
    wiki.write_page(new_page(CLAIM, "c-1", confidence=0.9, user_id="user-A",
                             claim_type="fact", description="春季最佳", subject_id="e:x",
                             object_id="e:spring", evidence_ids=["ev-1"],
                             contradicts=["c-9"]), user_id="user-A")
    conflicts = detect_logic_conflicts(
        [Proposition(statement="春季最佳", claim_page_id="c-1")],
        wiki=wiki, user_id="user-A")
    assert len(conflicts) == 1 and conflicts[0]["kind"] == CONFLICT_LOGIC
    assert conflicts[0]["values"] == ["c-9"]


def test_detect_conflicts_combines_all_four_kinds(settings, workdir):
    wiki = WikiStore(workdir / "wiki", settings=settings)
    wiki.write_page(new_page(CLAIM, "c-1", confidence=0.9, user_id="user-A",
                             claim_type="fact", description="d", subject_id="e:x",
                             object_id="e:y", evidence_ids=["ev-1"],
                             contradicts=["c-9"]), user_id="user-A")
    items = [
        Proposition(statement="六点半开放", subject_id="t", object_id="o"),
        Proposition(statement="七点开放", subject_id="t", object_id="o"),
        Proposition(statement="门票 60 元", subject_id="m"),
        Proposition(statement="门票 80 元", subject_id="m"),
        Proposition(statement="入口位于北门", subject_id="s", object_id="north"),
        Proposition(statement="入口位于南门", subject_id="s", object_id="south"),
        Proposition(statement="x", claim_page_id="c-1"),
    ]
    kinds = {c["kind"] for c in detect_conflicts(items, wiki=wiki, user_id="user-A")}
    assert kinds == set(CONFLICT_KINDS)


# ============================== 隐私校验 ============================== #
@pytest.fixture()
def wiki(settings, workdir):
    return WikiStore(workdir / "wiki", settings=settings)


def _verifier(wiki, **over):
    return ContentVerifier(wiki=wiki, retrieval=over.get("retrieval"),
                           jev=over.get("jev"), review_queue=over.get("review_queue"),
                           settings=over.get("settings"), min_confidence=None)


def test_privacy_blocks_cross_user_reference(wiki):
    """计划书三、P6 表注：检测输出是否混入其他用户隐私；发现即拦截并告警。"""
    wiki.write_page(new_page(CLAIM, "c-b", confidence=0.9, user_id="user-B",
                             claim_type="fact", description="B 的私事",
                             subject_id="a", object_id="b", evidence_ids=["ev-b"]),
                    user_id="user-B")
    # 由生产者提供来源表 → 能直接判出「越界引用他人数据」
    problems = _verifier(wiki).check_privacy(
        user_id="user-A", items=[Proposition(statement="x", claim_page_id="c-b")],
        output_text="", reference_owners={"c-b": "user-B"})
    assert len(problems) == 1
    assert problems[0]["kind"] == PRIVACY_CROSS_USER_REFERENCE

    verdict = asyncio.run(_verifier(wiki).verify(
        user_id="user-A", items=[Proposition(statement="x", claim_page_id="c-b")],
        output_text="正文"))
    assert verdict.action == ACTION_BLOCK
    assert verdict.blocked_text == "正文"
    assert verdict.alerts, "拦截必须同时告警"


def test_privacy_flags_dangling_reference(wiki):
    problems = _verifier(wiki).check_privacy(
        user_id="user-A", items=[Proposition(statement="x", claim_page_id="c-ghost")],
        output_text="")
    assert problems[0]["kind"] == PRIVACY_DANGLING_REFERENCE


def test_privacy_allows_own_and_public_references(wiki):
    wiki.write_page(new_page(CLAIM, "c-a", confidence=0.9, user_id="user-A",
                             claim_type="fact", description="自己的", subject_id="a",
                             object_id="b", evidence_ids=["ev-a"]), user_id="user-A")
    wiki.write_page(new_page(SOURCE, "s-pub", url="https://e.com/a"))
    problems = _verifier(wiki).check_privacy(
        user_id="user-A",
        items=[Proposition(statement="x", claim_page_id="c-a", evidence_ids=("s-pub",))],
        output_text="")
    assert problems == []


def test_privacy_flags_another_account_mark_in_the_text(wiki):
    problems = _verifier(wiki).check_privacy(user_id="user-A", items=[],
                                             output_text="该方案来自 user-B 的偏好")
    assert len(problems) == 1 and problems[0]["kind"] == PRIVACY_OTHER_ACCOUNT_MARK


def test_privacy_allows_the_current_account_mark(wiki):
    problems = _verifier(wiki).check_privacy(user_id="user-A", items=[],
                                             output_text="账号 user-A 的偏好已应用")
    assert problems == []


def test_privacy_check_never_enumerates_other_users_data(wiki):
    """核心设计约束：隐私校验只能按引用逐条 read_page，**不得遍历任何用户的数据**。

    计划书三、P6 步骤 6 明确「禁止批量遍历全部用户隐私」，
    因此这里用一个「一旦调用 list_* 就失败」的探针来锁死实现方式。
    """
    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def read_page(self, page_id, user_id=None):
            return self._inner.read_page(page_id, user_id=user_id)

        def __getattr__(self, name):
            if name.startswith("list_"):
                raise AssertionError(f"隐私校验不得遍历数据，却调用了 {name}")
            raise AttributeError(name)

    verifier = ContentVerifier(wiki=_Spy(wiki))
    problems = verifier.check_privacy(
        user_id="user-A",
        items=[Proposition(statement="x", claim_page_id="c-1",
                           evidence_ids=("ev-1",))],
        output_text="")
    assert len(problems) == 2, "两个引用都解析不到，应各自报一处"


# ============================== 事实核验 ============================== #
def test_verify_propositions_flags_missing_evidence_and_low_confidence(wiki):
    verifier = ContentVerifier(wiki=wiki, min_confidence=0.82)
    findings = verifier.verify_propositions(user_id="user-A", items=[
        Proposition(statement="没证据"),
        Proposition(statement="置信低", evidence_ids=("ev-1",), confidence=0.3)])
    kinds = {f["kind"] for f in findings}
    assert kinds == {FINDING_MISSING_EVIDENCE, FINDING_LOW_CONFIDENCE}


def test_verify_propositions_detects_contradiction(wiki):
    wiki.write_page(new_page(CLAIM, "c-x", confidence=0.9, user_id="user-A",
                             claim_type="fact", description="d", subject_id="a",
                             object_id="b", evidence_ids=["ev-1"],
                             contradicts=["c-other"]), user_id="user-A")
    findings = _verifier(wiki).verify_propositions(
        user_id="user-A",
        items=[Proposition(statement="x", claim_page_id="c-x", evidence_ids=("ev-1",),
                           confidence=0.95)])
    assert any(f["kind"] == FINDING_CONTRADICTION for f in findings)


class _Hit:
    def __init__(self, evidence):
        self.evidence_ids = evidence


class _Resp:
    def __init__(self, hits):
        self.hits = hits


class _Search:
    def __init__(self, supported):
        self.supported = supported

    def search(self, query, *, user_id, top_k=None):
        return _Resp([_Hit(["ev-1"])] if self.supported else [_Hit([])])


def test_reverse_search_flags_unsupported_propositions(wiki):
    """步骤 1「反向检索事实核验」：拿输出命题反查知识库，查不到支撑即疑似幻觉。"""
    verifier = ContentVerifier(wiki=wiki, retrieval=_Search(False))
    findings = verifier.reverse_search_unsupported(user_id="user-A",
                                                   text="凭空捏造的说法。")
    assert findings and findings[0]["kind"] == FINDING_UNSUPPORTED

    ok = ContentVerifier(wiki=wiki, retrieval=_Search(True))
    assert ok.reverse_search_unsupported(user_id="user-A", text="有支撑的说法。") == []


def test_reverse_search_is_skipped_without_retrieval(wiki):
    assert ContentVerifier(wiki=wiki).reverse_search_unsupported(
        user_id="user-A", text="x") == []


# ============================== 结论与重生成 ============================== #
def test_clean_input_passes(wiki):
    wiki.write_page(new_page(EVIDENCE, "ev-1", confidence=0.9, user_id="user-A",
                             source_id="s", source_text="t", text_unit_id="u"),
                    user_id="user-A")
    verdict = asyncio.run(_verifier(wiki).verify(
        user_id="user-A",
        items=[Proposition(statement="干净内容", evidence_ids=("ev-1",), confidence=0.95)]))
    assert verdict.action == ACTION_PASS and verdict.passed is True
    assert verdict.open_questions == []


def test_privacy_blocks_a_reference_that_cannot_be_resolved(wiki):
    """隔离之下，越界引用他人数据**正是以「解析不到」的形态出现**，故同样拦截。"""
    verdict = asyncio.run(_verifier(wiki).verify(
        user_id="user-A",
        items=[Proposition(statement="x", claim_page_id="c-unknown")], output_text="正文"))
    assert verdict.action == ACTION_BLOCK
    assert any(p["kind"] == PRIVACY_DANGLING_REFERENCE for p in verdict.privacy)


def test_conflicts_downgrade_rather_than_block(wiki):
    """冲突是内容问题，不是隐私事故 → 降级提示，不拦截。"""
    wiki.write_page(new_page(EVIDENCE, "ev-1", confidence=0.9, user_id="user-A",
                             source_id="s", source_text="t", text_unit_id="u"),
                    user_id="user-A")
    verdict = asyncio.run(_verifier(wiki).verify(
        user_id="user-A",
        items=[Proposition(statement="六点半开放", subject_id="t",
                           evidence_ids=("ev-1",), confidence=0.95),
               Proposition(statement="七点开放", subject_id="t",
                           evidence_ids=("ev-1",), confidence=0.95)]))
    assert verdict.action == ACTION_DOWNGRADE, "冲突应降级提示，不直接拦截"
    assert verdict.conflicts
    assert verdict.privacy == []


def test_regeneration_is_limited_to_two_attempts():
    assert ContentVerifier.regeneration_plan(attempt=0)["allowed"] is True
    assert ContentVerifier.regeneration_plan(attempt=1)["allowed"] is True
    final = ContentVerifier.regeneration_plan(attempt=2)
    assert final["allowed"] is False and final["exhausted"] is True
    assert "信息不足" in final["message"]


def test_open_questions_are_written_as_question_pages(wiki):
    """步骤 5「信息不足在 Wiki 生成 question.* 开放问题页面标记待核实」。"""
    verifier = _verifier(wiki)
    ids = verifier.raise_open_questions(user_id="user-A",
                                        statements=["缺少营业时间", "缺少票价"],
                                        now="2026-10-16T00:00:00Z")
    assert len(ids) == 2 and all(i.startswith("question-") for i in ids)
    page = wiki.read_page(ids[0], user_id="user-A")
    assert page is not None and page.type == QUESTION
    assert page.extra["text"].startswith("信息不足")
    assert wiki.read_page(ids[0], user_id="user-B") is None, "开放问题属本人 Vault"


def test_problematic_verdict_creates_open_questions(wiki):
    verdict = asyncio.run(_verifier(wiki).verify(
        user_id="user-A", items=[Proposition(statement="没有证据的说法")]))
    assert verdict.action == ACTION_DOWNGRADE
    assert verdict.open_questions, "降级时应留下待核实问题"


# ============================== 人工审核 ============================== #
def test_privacy_and_conflicts_are_enqueued_for_manual_review(settings, workdir, wiki):
    """步骤 6「挂载统一人工审核节点」。"""
    queue = ReviewQueue(FileStore(workdir / "storage", settings=settings))
    wiki.write_page(new_page(CLAIM, "c-b", confidence=0.9, user_id="user-B",
                             claim_type="fact", description="B", subject_id="a",
                             object_id="b", evidence_ids=["ev-b"]), user_id="user-B")
    verifier = _verifier(wiki, review_queue=queue)
    verdict = asyncio.run(verifier.verify(
        user_id="user-A", items=[Proposition(statement="x", claim_page_id="c-b")],
        output_text=""))
    assert verdict.review_items
    assert queue.list_items(user_id="user-A"), "复核项必须按账号可见"
    assert queue.list_items() == [], "不得出现在别的账号或公共视图里"


def test_review_is_skipped_without_a_queue(wiki):
    verdict = asyncio.run(_verifier(wiki).verify(
        user_id="user-A", items=[Proposition(statement="x", claim_page_id="c-ghost")]))
    assert verdict.review_items == []


# ============================== Jev 可信度分类 ============================== #
class _Jev:
    def __init__(self, label):
        self.label, self.calls = label, 0

    async def decide(self, *, user_id, question, allowed_labels, model=None):
        self.calls += 1
        assert set(allowed_labels) <= set(CREDIBILITY_LABELS)
        return JevDecision(self.label, 0.9, "jev")


def test_jev_classifies_credibility(wiki):
    jev = _Jev(CREDIBILITY_HIGH)
    verdict = asyncio.run(_verifier(wiki, jev=jev).verify(
        user_id="user-A",
        items=[Proposition(statement="x", evidence_ids=("e",), confidence=0.95)]))
    assert verdict.credibility == CREDIBILITY_HIGH and jev.calls == 1


def test_credibility_is_empty_without_jev(wiki):
    verdict = asyncio.run(_verifier(wiki).verify(user_id="user-A", items=[]))
    assert verdict.credibility == ""


def test_verify_requires_user_id(wiki):
    with pytest.raises(QaError):
        asyncio.run(_verifier(wiki).verify(user_id="", items=[]))


# ============================== 阶段 Agent ============================== #
def _agent(wiki, **over):
    available = {CAP_WIKI_READ: wiki,
                 CAP_RETRIEVAL: over.get("retrieval", _Search(True)),
                 CAP_JEV: over.get("jev", _Jev(CREDIBILITY_HIGH))}
    return ContentQaAgent(settings=over.get("settings"), tools=grant(ContentQaAgent,
                                                                     available),
                          review_queue=over.get("review_queue"))


def test_collect_propositions_gathers_plans_bills_and_ingest():
    """步骤 4：**所有方案、所有账单、所有入库数据**都要过检。"""
    state = {
        "p5_plan": {"items": [{"statement": "方案条目", "evidence_ids": ["ev-1"],
                               "confidence": 0.9}]},
        "bills": [{"statement": "账单 60 元", "subject_id": "b1"}],
        "pending_ingest": [{"statement": "入库数据", "subject_id": "i1"}],
    }
    props = collect_propositions(state)
    assert [p.kind for p in props] == [KIND_PLAN, KIND_BILL, KIND_INGEST]


def test_collect_propositions_skips_empty_entries():
    assert collect_propositions({"p5_plan": {"items": [{"statement": "  "}]},
                                 "bills": [None]}) == []


def test_agent_verifies_and_writes_only_its_own_field(wiki):
    state = new_state(user_id="user-A", session_id="s1")
    state["plan_text"] = "西湖六点半开放。"
    out = _call(_agent(wiki), state)
    assert set(out) == {"p6_verdict"}
    assert out["p6_verdict"]["phase"] == "P6"
    assert out["p6_verdict"]["action"] in ACTIONS


def test_agent_blocks_cross_user_content_from_state(wiki):
    wiki.write_page(new_page(CLAIM, "c-b", confidence=0.9, user_id="user-B",
                             claim_type="fact", description="B", subject_id="a",
                             object_id="b", evidence_ids=["ev-b"]), user_id="user-B")
    state = new_state(user_id="user-A", session_id="s1")
    state["p5_plan"] = {"items": [{"statement": "混进来的内容",
                                   "claim_page_id": "c-b",
                                   "evidence_ids": ["ev-b"], "confidence": 0.9}]}
    verdict = _call(_agent(wiki), state)["p6_verdict"]
    assert verdict["action"] == ACTION_BLOCK and verdict["alerts"]


def test_agent_requires_wiki_read_and_jev(wiki):
    for missing in (CAP_WIKI_READ, CAP_RETRIEVAL, CAP_JEV):
        available = {CAP_WIKI_READ: wiki, CAP_RETRIEVAL: _Search(True),
                     CAP_JEV: _Jev(CREDIBILITY_HIGH)}
        available.pop(missing)
        agent = ContentQaAgent(tools=grant(ContentQaAgent, available))
        state = new_state(user_id="user-A", session_id="s1")
        state["plan_text"] = "x"
        with pytest.raises(CapabilityError):
            _call(agent, state)


def test_agent_skips_when_session_is_invalid(wiki):
    assert _agent(wiki)(new_state(user_id="user-A", session_id="s1",
                                  login_status=False)) == {}


def test_p6_is_now_marked_implemented():
    from ai_butler.core import phases
    spec = phases.phase("P6")
    assert spec.implemented is True
    assert spec.title == "内容质检 Agent｜全局防错、防幻、防假"
