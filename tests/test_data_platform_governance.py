"""P1 数据治理规则与冲突复核队列测试。

计划书依据：三、P1「实现三条数据治理规则：高分入库、低分留日志、冲突人工复核；
Wiki 检测到矛盾自动送入冲突复核队列」
"""

from __future__ import annotations

import pytest

from ai_butler.agent_wiki.compiler import WikiCompiler
from ai_butler.agent_wiki.ontology import CLAIM, SOURCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.storage import FileStore
from ai_butler.data_platform.governance import (
    ACTIONS,
    LOG_ONLY,
    PENDING,
    PERSIST,
    RESOLVED,
    REVIEW,
    DataGovernor,
    GovernanceError,
    ReviewQueue,
)
from ai_butler.data_platform.models import CONFLICT_NONE, CONFLICT_PENDING


@pytest.fixture()
def governor(settings):
    return DataGovernor(settings=settings)


def test_high_score_is_persisted(governor):
    d = governor.decide(confidence=0.95)
    assert d.action == PERSIST and "高分入库" in d.reason


def test_low_score_is_logged_only(governor):
    d = governor.decide(confidence=0.4)
    assert d.action == LOG_ONLY and "仅留日志" in d.reason


def test_conflict_always_goes_to_manual_review_regardless_of_score(governor):
    """冲突规则优先于分数：即使置信很高也必须人工复核。"""
    d = governor.decide(confidence=0.99, conflict_status=CONFLICT_PENDING)
    assert d.action == REVIEW and "冲突复核队列" in d.reason

    d2 = governor.decide(confidence=0.1, conflict_status=CONFLICT_PENDING)
    assert d2.action == REVIEW


def test_threshold_is_reused_not_invented(settings, governor):
    """计划书【八】已有 0.82；不再新造第二个阈值。"""
    assert governor.threshold == settings.jev_confidence_threshold == 0.82
    assert governor.decide(confidence=0.82).action == PERSIST     # 含边界
    assert governor.decide(confidence=0.819).action == LOG_ONLY


def test_exactly_three_governance_actions(governor):
    assert ACTIONS == (PERSIST, LOG_ONLY, REVIEW)
    assert governor.decide(confidence=0.9, conflict_status=CONFLICT_NONE).action == PERSIST


def test_invalid_confidence_is_refused(governor):
    with pytest.raises(GovernanceError):
        governor.decide(confidence=1.2)


# ------------------------------ 复核队列 ------------------------------ #
@pytest.fixture()
def queue(settings, workdir):
    return ReviewQueue(FileStore(workdir / "storage", settings=settings))


def test_enqueue_and_list_scoped_by_user(queue):
    queue.enqueue(kind="contradiction", ref_id="c-pub", reason="公共矛盾", user_id=None)
    queue.enqueue(kind="contradiction", ref_id="c-a", reason="A 的矛盾", user_id="user-A")
    queue.enqueue(kind="contradiction", ref_id="c-b", reason="B 的矛盾", user_id="user-B")

    # 计划书三、P6：人工后台只能按 user_id 查看对应账号业务
    assert [i.ref_id for i in queue.list_items()] == ["c-pub"]
    assert [i.ref_id for i in queue.list_items(user_id="user-A")] == ["c-a"]
    assert [i.ref_id for i in queue.list_items(user_id="user-B")] == ["c-b"]


def test_resolve_moves_item_out_of_pending(queue):
    item = queue.enqueue(kind="low_confidence", ref_id="c1", reason="低置信")
    assert len(queue.list_items()) == 1

    resolved = queue.resolve(item.item_id, resolution="人工确认属实", by="reviewer")
    assert resolved.status == RESOLVED and resolved.resolved_by == "reviewer"
    assert queue.list_items(status=PENDING) == []
    assert len(queue.list_items(status=RESOLVED)) == 1


def test_enqueue_validates_inputs(queue):
    with pytest.raises(GovernanceError):
        queue.enqueue(kind="", ref_id="x", reason="r")
    with pytest.raises(GovernanceError):
        queue.resolve("不存在的编号", resolution="x")


def test_stats_are_aggregate_only(queue):
    queue.enqueue(kind="contradiction", ref_id="c1", reason="r", user_id="user-A")
    queue.enqueue(kind="low_confidence", ref_id="c2", reason="r", user_id="user-B")
    stats = queue.stats()
    assert stats["total"] == 2 and stats["pending"] == 2
    assert stats["by_kind"] == {"contradiction": 1, "low_confidence": 1}
    assert "user-A" not in str(stats), "统计只返回聚合计数，不夹带账号"


def test_wiki_contradictions_are_fed_into_the_queue(settings, workdir, queue):
    """计划书三、P1「Wiki 检测到矛盾自动送入冲突复核队列」。"""
    store = WikiStore(workdir / "wiki", settings=settings)
    store.write_page(new_page(SOURCE, "src-1", url="https://e.com/a", status="active"))
    store.write_page(new_page(CLAIM, "claim-1", confidence=0.9, claim_type="fact",
                              description="春季最佳", subject_id="e:西湖", object_id="e:春季",
                              evidence_ids=["ev-1"]))
    store.write_page(new_page(CLAIM, "claim-2", confidence=0.5, status="SUSPECTED",
                              claim_type="fact", description="秋季最佳",
                              subject_id="e:西湖", object_id="e:秋季",
                              evidence_ids=["ev-1"], contradicts=["claim-1"]))

    report = WikiCompiler(store).compile_workspace()
    assert report.contradictions, "前提：编译应检出矛盾"

    items = queue.enqueue_wiki_contradictions(report)
    assert {i.ref_id for i in items} == set(report.contradictions)
    assert all(i.kind == "contradiction" for i in items)
    assert all(i.user_id is None for i in items), "公共库的矛盾不含用户隐私"
