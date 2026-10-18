"""P1 变更审计与版本追溯测试。

计划书依据：三、P1「搭建数据版本溯源、变更审计、回溯系统；审计记录账号变更行为；
同步保留 Wiki 页面变更历史」
"""

from __future__ import annotations

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, new_page
from ai_butler.core.storage import FileStore
from ai_butler.data_platform.audit import (
    ACTIONS,
    CREATE,
    UPDATE,
    AuditError,
    ChangeAudit,
    snapshot,
)


@pytest.fixture()
def audit(settings, workdir):
    return ChangeAudit(FileStore(workdir / "storage", settings=settings))


def test_versions_are_monotonic_per_entity(audit):
    a = audit.record(entity="data_user_profile", entity_id="1", action=CREATE,
                     user_id="user-A", after={"value": "高铁"})
    b = audit.record(entity="data_user_profile", entity_id="1", action=UPDATE,
                     user_id="user-A", before={"value": "高铁"}, after={"value": "自驾"})
    c = audit.record(entity="data_user_profile", entity_id="1", action=UPDATE,
                     user_id="user-A", before={"value": "自驾"}, after={"value": "飞机"})
    assert [e.version for e in (a, b, c)] == [1, 2, 3]
    assert ACTIONS == ("create", "update", "delete")


def test_history_is_scoped_by_account(audit):
    """审计记录账号变更行为，但不得跨账号读取。"""
    audit.record(entity="t", entity_id="1", action=CREATE, user_id="user-A")
    audit.record(entity="t", entity_id="2", action=CREATE, user_id="user-B")
    audit.record(entity="t", entity_id="3", action=CREATE, user_id=None)   # 公共域

    assert {e.entity_id for e in audit.history(user_id="user-A")} == {"1"}
    assert {e.entity_id for e in audit.history(user_id="user-B")} == {"2"}
    assert {e.entity_id for e in audit.history()} == {"3"}, "不传账号只能读公共域"
    assert len(audit.history(user_id="user-A", entity="t")) == 1


def test_latest_returns_the_newest_entry(audit):
    audit.record(entity="t", entity_id="1", action=CREATE, user_id="user-A", after={"v": 1})
    audit.record(entity="t", entity_id="1", action=UPDATE, user_id="user-A", after={"v": 2})
    assert audit.latest(entity="t", entity_id="1", user_id="user-A").after == {"v": 2}
    assert audit.latest(entity="t", entity_id="missing", user_id="user-A") is None


def test_stats_are_aggregate_only(audit):
    audit.record(entity="t", entity_id="1", action=CREATE, user_id="user-A")
    audit.record(entity="t", entity_id="2", action=UPDATE, user_id="user-B")
    stats = audit.stats()
    assert stats == {"total_changes": 2, "by_action": {"create": 1, "update": 1}}
    assert "user-A" not in str(stats)


def test_invalid_action_and_entity_are_refused(audit):
    with pytest.raises(AuditError):
        audit.record(entity="t", entity_id="1", action="upsert", user_id="user-A")
    with pytest.raises(AuditError):
        audit.record(entity="t", entity_id="../escape", action=CREATE, user_id="user-A")


def test_snapshot_serialises_datetimes():
    from datetime import datetime, timezone

    class Row:
        id = 1
        created_at = datetime(2026, 10, 16, tzinfo=timezone.utc)
        note = "x"

    assert snapshot(Row(), ("id", "created_at", "note")) == {
        "id": 1, "created_at": "2026-10-16T00:00:00+00:00", "note": "x"}


# -------------------- Wiki 页面变更历史与回溯 -------------------- #
def test_wiki_page_history_is_kept_and_scoped(audit):
    """计划书三、P1「同步保留 Wiki 页面变更历史」。"""
    page = new_page(CLAIM, "claim-h", confidence=0.9, claim_type="fact", description="v1",
                    subject_id="a", object_id="b", evidence_ids=["ev-1"], user_id="user-A")
    audit.record_wiki_page(page, action=CREATE, actor="ingest")

    page.extra["description"] = "v2"
    audit.record_wiki_page(page, action=UPDATE, before=page.to_markdown())

    history = audit.wiki_page_history("claim-h", user_id="user-A")
    assert [e.version for e in history] == [1, 2]
    assert audit.wiki_page_history("claim-h", user_id="user-B") == []


def test_rollback_wiki_page_returns_the_previous_state(audit):
    """回溯：取回「变更前」内容，而不是删除历史。"""
    from ai_butler.agent_wiki.ontology import WikiPage

    v1 = new_page(CLAIM, "claim-r", confidence=0.9, claim_type="fact", description="原始内容",
                  subject_id="a", object_id="b", evidence_ids=["ev-1"], user_id="user-A")
    audit.record_wiki_page(v1, action=CREATE)

    v2 = WikiPage.parse(v1.to_markdown())
    v2.extra["description"] = "被改坏的内容"
    entry = audit.record_wiki_page(v2, action=UPDATE, before=v1.to_markdown())

    restored = audit.rollback_wiki_page(entry.change_id, user_id="user-A")
    assert restored.extra["description"] == "原始内容"

    # 回滚本身也要能留下痕迹（历史不删）
    assert len(audit.wiki_page_history("claim-r", user_id="user-A")) == 2


def test_rollback_refuses_when_there_is_no_previous_state(audit):
    page = new_page(CLAIM, "claim-n", confidence=0.9, claim_type="fact", description="首版",
                    subject_id="a", object_id="b", evidence_ids=["ev-1"], user_id="user-A")
    entry = audit.record_wiki_page(page, action=CREATE)
    with pytest.raises(AuditError):
        audit.rollback_wiki_page(entry.change_id, user_id="user-A")
    with pytest.raises(AuditError):
        audit.rollback_wiki_page("不存在的编号", user_id="user-A")
