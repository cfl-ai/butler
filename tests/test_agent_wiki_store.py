"""Agent-Wiki v2 本体与双库存储层测试。

计划书依据：一、项目概述本体范式；三、P0-1 运行时存储层；三、P1/P2；七、17。
"""

from __future__ import annotations

import pytest

from ai_butler.agent_wiki.ontology import (
    CLAIM,
    RELATION,
    SOURCE,
    SOURCE_STATUSES,
    OntologyError,
    WikiPage,
    new_page,
)
from ai_butler.agent_wiki.workspace import VaultIsolationError, WikiStore


def _store(settings, workdir, **over):
    return WikiStore(workdir, settings=settings.model_copy(update=over))


def _claim(page_id: str, *, user_id=None, confidence=0.9, status="TRUE", **extra):
    base = dict(claim_type="preference", description="偏好春季出行",
                subject_id="user:A", object_id="season:spring",
                evidence_ids=["ev-1"])
    base.update(extra)
    return new_page(CLAIM, page_id, title=page_id, confidence=confidence,
                    user_id=user_id, status=status, **base)


# ----------------------------- 本体 ----------------------------- #
def test_page_markdown_roundtrip():
    page = _claim("claim-0001")
    page.observations = ["西湖春季最佳", "避开清明假期"]
    page.relations = ["relates_to: entity:西湖"]
    parsed = WikiPage.parse(page.to_markdown())
    assert parsed.id == page.id and parsed.type == CLAIM
    assert parsed.confidence == page.confidence
    assert parsed.extra["subject_id"] == "user:A"
    assert parsed.observations == page.observations
    assert parsed.relations == page.relations


def test_claim_requires_u17_fields():
    """U17：claim 必填字段缺失即拒绝。"""
    with pytest.raises(OntologyError) as ei:
        new_page(CLAIM, "c1", claim_type="x")           # 缺 description/subject_id/object_id
    assert "缺少必填字段" in str(ei.value)


def test_claim_status_must_be_in_allowed_set():
    with pytest.raises(OntologyError):
        new_page(CLAIM, "c1", status="MAYBE", claim_type="x", description="d",
                 subject_id="s", object_id="o")


def test_source_status_is_active_or_archived():
    page = new_page(SOURCE, "src-1", url="https://example.com", status="archived")
    assert page.status in SOURCE_STATUSES
    with pytest.raises(OntologyError):
        new_page(SOURCE, "src-2", url="https://example.com", status="expired")


def test_confidence_must_be_within_0_1():
    with pytest.raises(OntologyError):
        _claim("c-bad", confidence=1.4)


# --------------------------- 双库隔离 --------------------------- #
def test_public_workspace_page_must_not_carry_user_id(settings, workdir):
    """计划书三、P1：素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID。"""
    store = _store(settings, workdir)
    page = _claim("c-pub", user_id=None)
    store.write_page(page, user_id=None)                     # 正常

    leaky = new_page(CLAIM, "c-leak", user_id="u-1", claim_type="x",
                     description="d", subject_id="s", object_id="o")
    with pytest.raises(VaultIsolationError) as ei:
        store.write_page(leaky, user_id=None)
    assert "公私域隔离" in str(ei.value)


def test_vault_write_requires_matching_user_id(settings, workdir):
    store = _store(settings, workdir)
    with pytest.raises(VaultIsolationError):
        store.write_page(_claim("c1", user_id="u-1"), user_id=None)   # 写公共库

    with pytest.raises(VaultIsolationError):                          # 跨账号写入
        store.write_page(_claim("c2", user_id="u-2"), user_id="u-1")

    store.write_page(_claim("c3", user_id="u-1"), user_id="u-1")      # 正常


def test_vault_access_without_user_id_is_refused(settings, workdir):
    store = _store(settings, workdir)
    with pytest.raises(VaultIsolationError) as ei:
        store.vault_dir(None)
    assert "必须携带 user_id" in str(ei.value)


def test_cross_account_pages_are_invisible(settings, workdir):
    """七、17：用户私有 Vault 互相隔离。"""
    store = _store(settings, workdir)
    store.write_page(_claim("c-a", user_id="user-A"), user_id="user-A")
    store.write_page(_claim("c-b", user_id="user-B"), user_id="user-B")

    assert [p.id for p in store.list_pages(user_id="user-A")] == ["c-a"]
    assert [p.id for p in store.list_pages(user_id="user-B")] == ["c-b"]
    assert store.read_page("c-b", user_id="user-A") is None
    assert [p.id for p in store.list_pages(user_id=None)] == []


def test_retrieval_merges_workspace_and_own_vault_only(settings, workdir):
    """计划书三、P2：合并返回公共 Workspace + 当前用户私有 Vault，不加载其他用户页面。"""
    store = _store(settings, workdir)
    store.write_page(_claim("c-pub"), user_id=None)
    store.write_page(_claim("c-a", user_id="user-A"), user_id="user-A")
    store.write_page(_claim("c-b", user_id="user-B"), user_id="user-B")

    got = sorted(p.id for p in store.list_pages_for_retrieval("user-A"))
    assert got == ["c-a", "c-pub"], "不得包含 user-B 的私有页面"

    with pytest.raises(VaultIsolationError):
        store.list_pages_for_retrieval(None)


def test_disable_flags_from_plan_section_eight(settings, workdir):
    store = _store(settings, workdir, wiki_public_workspace_enable=False)
    with pytest.raises(VaultIsolationError):
        _ = store.workspace_dir

    store2 = _store(settings, workdir, wiki_user_vault_per_user=False)
    with pytest.raises(VaultIsolationError):
        store2.vault_dir("u-1")
