"""源码修改授权的守护测试（U88）。

这里挡的不是「授权流程走不通」，而是四种会让授权变成装饰的退化——
每一种都比「功能没做完」更难发现，因为它们都让测试更绿：

1. **没有限期的授权**：放出去就永远有效，而没人记得它为什么被放。
2. **不验签的核查**：授权簿被手改过（或整份从别的机器拷来）照样放行，
   「本机权威」四个字随之作废。
3. **范围判不准**：`ai_butler/retrieval_extra` 算进 `ai_butler/retrieval` 的范围，
   越界路径被洗成合法——而范围正是这个模块唯一要判准的东西。
4. **状态不留痕**：吊销/转让后记录被删，「这个主体的权从哪来」再也答不上来。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_butler.core.authority import (
    ACTIVE,
    EXPIRED,
    REVOKED,
    TRANSFERRED,
    AuthorityError,
    AuthorityRegistry,
    MachineIdentity,
    SourceGrant,
)


def _registry(tmp_path: Path) -> AuthorityRegistry:
    return AuthorityRegistry(root=tmp_path / "authority")


def _grant(reg: AuthorityRegistry, subject: str = "system:p10-repair", **kw):
    base = dict(allowed_prefixes=("ai_butler/retrieval/",),
                forbidden_paths=("tests/", "ai_butler/evaluation/"),
                granted_by="owner", ttl_seconds=3600, note="演示")
    base.update(kw)
    return reg.grant(subject, **base)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# 一、机器身份
# --------------------------------------------------------------------------- #
def test_machine_key_is_created_once_and_stays_stable(tmp_path):
    root = tmp_path / "authority"
    first = MachineIdentity.load_or_create(root)
    second = MachineIdentity.load_or_create(root)
    assert first.machine_id == second.machine_id
    assert first.secret == second.secret
    key_file = root / "machine.key"
    assert key_file.exists()
    data = json.loads(key_file.read_text(encoding="utf-8"))
    assert set(data) == {"machine_id", "secret"}
    assert len(data["secret"]) == 64          # token_hex(32)


def test_sign_and_verify_roundtrip(tmp_path):
    identity = MachineIdentity.load_or_create(tmp_path / "authority")
    payload = {"subject": "s", "expires_at": "2026-11-01T00:00:00+00:00"}
    signature = identity.sign(payload)
    assert identity.verify(payload, signature)


def test_verify_rejects_tampered_payload_and_empty_signature(tmp_path):
    identity = MachineIdentity.load_or_create(tmp_path / "authority")
    payload = {"subject": "s", "ttl": 3600}
    signature = identity.sign(payload)
    assert not identity.verify({**payload, "ttl": 99999}, signature)
    assert not identity.verify(payload, "")


def test_grants_from_another_machine_do_not_verify(tmp_path):
    """授权簿被整份拷到别的机器：密钥不同，签名不符。"""
    here = MachineIdentity.load_or_create(tmp_path / "a")
    there = MachineIdentity.load_or_create(tmp_path / "b")
    assert here.machine_id != there.machine_id
    payload = {"subject": "s"}
    assert not there.verify(payload, here.sign(payload))


def test_corrupt_machine_key_refuses_instead_of_regenerating(tmp_path):
    """密钥损坏不重新生成：重新生成 = 所有既有授权静默失效。"""
    root = tmp_path / "authority"
    root.mkdir(parents=True)
    (root / "machine.key").write_text("{坏文件", encoding="utf-8")
    with pytest.raises(AuthorityError) as err:
        MachineIdentity.load_or_create(root)
    assert "不要手改" in str(err.value)


# --------------------------------------------------------------------------- #
# 二、授权与核查
# --------------------------------------------------------------------------- #
def test_grant_persists_and_survives_a_new_registry_instance(tmp_path):
    reg = _registry(tmp_path)
    grant = _grant(reg)
    assert grant.status == ACTIVE
    assert grant.signature
    # 新实例（模拟重启/另一个进程）回读得到同一条
    again = _registry(tmp_path)
    (found,) = [g for g in again.grants() if g.subject == grant.subject]
    assert found.signature == grant.signature
    assert again.check(grant.subject).allowed


def test_check_without_grant_says_what_to_do(tmp_path):
    verdict = _registry(tmp_path).check("system:p10-repair")
    assert not verdict.allowed
    assert "grant" in verdict.reason          # 拒绝理由必须能行动


def test_check_covers_scope_and_forbids_rest(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg)
    assert reg.check("system:p10-repair",
                     paths=["ai_butler/retrieval/planner.py"]).allowed
    assert reg.check("system:p10-repair",
                     paths=["ai_butler/retrieval/", "ai_butler/retrieval/chat.py"]).allowed
    denied = reg.check("system:p10-repair", paths=["tests/test_repair_loop.py"])
    assert not denied.allowed                 # 禁区优先（deny overrides）
    denied = reg.check("system:p10-repair", paths=["ai_butler/scheduler/router.py"])
    assert not denied.allowed
    assert "scheduler" in denied.reason


def test_scope_matching_is_prefix_aware_not_substring(tmp_path):
    """`retrieval_extra` 不算 `retrieval` 的范围——子串匹配会洗白越界路径。"""
    reg = _registry(tmp_path)
    _grant(reg, subject="s", allowed_prefixes=("ai_butler/retrieval",))
    assert reg.check("s", paths=["ai_butler/retrieval/planner.py"]).allowed
    assert not reg.check("s", paths=["ai_butler/retrieval_extra/x.py"]).allowed
    assert not reg.check("s", paths=["ai_butler/retrievalX.py"]).allowed


def test_paths_are_normalized_before_matching(tmp_path):
    """反斜杠与 `./` 前缀不影响判定：同一份授权两个平台下覆盖范围一致。"""
    reg = _registry(tmp_path)
    _grant(reg, subject="s")
    assert reg.check("s", paths=["ai_butler\\retrieval\\chat.py"]).allowed
    assert reg.check("s", paths=["./ai_butler/retrieval/chat.py"]).allowed


def test_expired_grant_is_denied_with_renewal_hint(tmp_path):
    reg = _registry(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    _grant(reg, subject="s", ttl_seconds=1, now=past)
    verdict = reg.check("s")
    assert not verdict.allowed
    assert "到期" in verdict.reason
    assert "grant" in verdict.reason          # 理由里给出续期命令


def test_expired_grant_can_be_renewed_by_a_new_grant(tmp_path):
    """到期后重新授权：旧记录标 expired（留痕），新授权生效。"""
    reg = _registry(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    _grant(reg, subject="s", ttl_seconds=1, now=past)
    renewed = _grant(reg, subject="s", ttl_seconds=3600)
    assert renewed.status == ACTIVE
    statuses = {g.status for g in reg.grants()}
    assert EXPIRED in statuses and ACTIVE in statuses
    assert reg.check("s").allowed


def test_revoked_grant_is_denied_and_record_is_kept(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="s")
    revoked = reg.revoke("s", by="owner", reason="演示结束")
    assert revoked.status == REVOKED
    assert "演示结束" in revoked.note and "owner" in revoked.note
    assert not reg.check("s").allowed
    # 记录不删：审计要能回答「吊销过谁、为什么」
    (kept,) = [g for g in reg.grants() if g.subject == "s"]
    assert kept.status == REVOKED


# --------------------------------------------------------------------------- #
# 三、转让
# --------------------------------------------------------------------------- #
def test_transfer_moves_scope_and_keeps_the_chain(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="a", allowed_prefixes=("ai_butler/retrieval/",),
           forbidden_paths=("tests/",))
    moved = reg.transfer("a", "b", by="owner", note="交接给夜班链路")
    assert moved.subject == "b"
    assert moved.supersedes == "a"
    assert moved.allowed_prefixes == ("ai_butler/retrieval",)   # 归一后无尾斜杠
    assert moved.forbidden_paths == ("tests",)
    assert moved.expires_at == reg.check("b").grant.expires_at   # 期限照搬，不自动延长
    assert not reg.check("a").allowed
    assert reg.check("b").allowed
    old = next(g for g in reg.grants() if g.subject == "a")
    assert old.status == TRANSFERRED and old.transferred_to == "b"


def test_transfer_refuses_when_source_is_gone_or_expired(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(AuthorityError):
        reg.transfer("nobody", "b", by="owner")
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    _grant(reg, subject="a", ttl_seconds=1, now=past)
    with pytest.raises(AuthorityError) as err:
        reg.transfer("a", "b", by="owner")
    assert "到期" in str(err.value)


def test_transfer_refuses_when_target_already_holds_a_grant(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="a")
    _grant(reg, subject="b")
    with pytest.raises(AuthorityError) as err:
        reg.transfer("a", "b", by="owner")          # b 手里已有一份
    assert "b" in str(err.value)


# --------------------------------------------------------------------------- #
# 四、fail-closed：篡改、损坏、越权拷贝
# --------------------------------------------------------------------------- #
def test_tampered_active_grant_is_denied_with_signature_reason(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="s")
    data = json.loads((tmp_path / "authority" / "grants.json").read_text(encoding="utf-8"))
    data[0]["allowed_prefixes"] = ["ai_butler/"]        # 手改扩大范围
    (tmp_path / "authority" / "grants.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    verdict = reg.check("s")
    assert not verdict.allowed
    assert "签名" in verdict.reason


def test_grants_file_copied_to_another_machine_is_denied(tmp_path):
    """整份授权簿换机器：签名验不过（密钥不同），且不尝试修复。"""
    reg = _registry(tmp_path)
    _grant(reg, subject="s")
    copied = tmp_path / "elsewhere"
    copied.mkdir()
    (copied / "grants.json").write_text(
        (tmp_path / "authority" / "grants.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    stranger = AuthorityRegistry(root=copied)           # 新机器的新密钥
    verdict = stranger.check("s")
    assert not verdict.allowed
    assert "签名" in verdict.reason


def test_corrupt_grants_file_refuses_to_load(tmp_path):
    reg = _registry(tmp_path)
    (tmp_path / "authority" / "grants.json").write_text("{不是数组", encoding="utf-8")
    with pytest.raises(AuthorityError):
        reg.grants()
    with pytest.raises(AuthorityError):
        reg.check("s")                                   # 读不出簿 = 谁都没授权


def test_grant_refuses_scope_free_and_anonymous_and_endless(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(AuthorityError):
        reg.grant("s", allowed_prefixes=(), granted_by="owner", ttl_seconds=60)
    with pytest.raises(AuthorityError):
        reg.grant("s", allowed_prefixes=("ai_butler/",), granted_by="  ",
                  ttl_seconds=60)
    with pytest.raises(AuthorityError):
        reg.grant("s", allowed_prefixes=("ai_butler/",), granted_by="owner",
                  ttl_seconds=0)
    with pytest.raises(AuthorityError):
        reg.grant("s", allowed_prefixes=("ai_butler/",), granted_by="owner",
                  ttl_seconds=-1)
    with pytest.raises(AuthorityError):
        reg.grant("  ", allowed_prefixes=("ai_butler/",), granted_by="owner",
                  ttl_seconds=60)


def test_duplicate_active_grant_is_refused(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="s")
    with pytest.raises(AuthorityError) as err:
        _grant(reg, subject="s")
    assert "revoke" in str(err.value)                    # 理由给出下一步


def test_anonymous_revoke_is_refused(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="s")
    with pytest.raises(AuthorityError):
        reg.revoke("s", by="  ", reason="x")
    with pytest.raises(AuthorityError):
        reg.revoke("never-granted", by="owner", reason="x")


def test_from_dict_rejects_malformed_records(tmp_path):
    with pytest.raises(AuthorityError):
        SourceGrant.from_dict({"subject": "s"})           # 缺 expires_at 等
    with pytest.raises(AuthorityError):
        SourceGrant.from_dict(["不是", "dict"])


# --------------------------------------------------------------------------- #
# 五、跨实例的读写互斥（U86 的立场：锁内重读）
# --------------------------------------------------------------------------- #
def test_second_instance_sees_first_instances_write(tmp_path):
    """两个进程各持一个 registry：B 吊销必须看见 A 刚授的权。

    锁只包 append 不解决问题——B 会拿着加载时的旧快照判断「没有活跃授权」，
    然后写一份「已吊销」进去，把 A 的授权从簿里安静地抹掉。
    """
    root = tmp_path / "authority"
    a = AuthorityRegistry(root=root)
    _grant(a, subject="s")
    b = AuthorityRegistry(root=root)                      # 新实例 = 重新 load
    b.revoke("s", by="owner", reason="跨实例吊销")
    c = AuthorityRegistry(root=root)
    assert not c.check("s").allowed
    assert len([g for g in c.grants() if g.subject == "s"]) == 1


def test_status_summary_reports_counts_and_tampering(tmp_path):
    reg = _registry(tmp_path)
    _grant(reg, subject="a")
    _grant(reg, subject="b")
    reg.transfer("a", "c", by="owner")
    reg.revoke("b", by="owner", reason="收权")
    summary = reg.status()
    assert summary["counts"] == {"active": 1, "expired": 0,
                                 "revoked": 1, "transferred": 1}
    assert [g["subject"] for g in summary["active"]] == ["c"]
    assert summary["tampered"] == []
    assert "machine.key" not in json.dumps(summary)       # 摘要不泄密钥
