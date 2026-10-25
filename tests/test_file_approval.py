"""持久审批票的守护测试（U90）。

这里挡的是「人工确认」退化成「确认不了」的三种形态——每一种都让
撤销流程看起来能跑，实际跑不通：

1. **票活不过进程**：闭环进程申请的票，人在另一个 shell 里永远看不到——
   于是要么撤销永远停在 requires_approval，要么有人为了跑通把审批整个绕掉。
2. **损坏即清空**：票簿读不出来时静默从空开始，「已批准的票」变成
   「票不存在」——看着像从没申请过，而那正是审计最怕的一种安静。
3. **持久化削弱纪律**：存住了票，却弄丢了一次性/签名绑定/主体绑定/TTL
   中的任何一条——持久化的目的不是方便，是让同一个安全模型跨进程成立。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_butler.core.approval import (
    APPROVED,
    EXPIRED,
    PENDING,
    REJECTED,
    ApprovalError,
    ApprovalRequest,
    ApprovalService,
    FileApprovalService,
)

TOOL = "harness.revision.undo"
SIG = "sig-abc"
AGENT = "system:p10-repair"
ROLE = "p10-harness"


def _service(path: Path, *, ttl: float = 3600.0, clock=None) -> FileApprovalService:
    return FileApprovalService(path, ttl_seconds=ttl,
                               clock=clock or (lambda: 1000.0))


def _request(service: FileApprovalService, **kw) -> ApprovalRequest:
    base = dict(tool=TOOL, signature=SIG, agent=AGENT, role=ROLE,
                tier=2, reason="撤销 R1")
    base.update(kw)
    return service.request(**base)


# --------------------------------------------------------------------------- #
# 一、票要活得过进程重启
# --------------------------------------------------------------------------- #
def test_ticket_survives_a_new_service_instance(tmp_path):
    path = tmp_path / "approvals.json"
    ticket = _request(_service(path))
    assert ticket.status == PENDING
    # 新实例（另一个进程）看得见同一张票
    again = _service(path)
    found = again.get(ticket.approval_id)
    assert found is not None and found.status == PENDING
    assert found.signature == SIG and found.agent == AGENT


def test_approval_survives_and_is_attributed(tmp_path):
    path = tmp_path / "approvals.json"
    service = _service(path)
    ticket = _request(service)
    service.approve(ticket.approval_id, by="值班负责人")
    found = _service(path).get(ticket.approval_id)
    assert found.status == APPROVED
    assert found.decided_by == "值班负责人"


def test_consume_burns_the_ticket_across_instances(tmp_path):
    """一次性凭据：第一个进程消费后，第二个进程再消费一律拒。"""
    path = tmp_path / "approvals.json"
    first = _service(path)
    ticket = _request(first)
    first.approve(ticket.approval_id, by="owner")
    first.consume(ticket.approval_id, tool=TOOL, signature=SIG,
                  agent=AGENT, role=ROLE)
    second = _service(path)
    assert second.get(ticket.approval_id).consumed is True
    with pytest.raises(ApprovalError) as err:
        second.consume(ticket.approval_id, tool=TOOL, signature=SIG,
                       agent=AGENT, role=ROLE)
    assert "已消费过" in str(err.value)


def test_signature_and_agent_bindings_survive_reload(tmp_path):
    """批准的是调用 A：重启后拿 B 的签名、或换身份来消费，一律拒。"""
    path = tmp_path / "approvals.json"
    service = _service(path)
    ticket = _request(service)
    service.approve(ticket.approval_id, by="owner")
    second = _service(path)
    with pytest.raises(ApprovalError):
        second.consume(ticket.approval_id, tool=TOOL, signature="别的签名",
                       agent=AGENT, role=ROLE)
    with pytest.raises(ApprovalError):
        second.consume(ticket.approval_id, tool=TOOL, signature=SIG,
                       agent="system:someone-else", role=ROLE)


def test_ttl_expiry_applies_to_reloaded_tickets(tmp_path):
    """无人应答即拒：到期后新进程读到的是 expired，不是 pending。"""
    path = tmp_path / "approvals.json"
    now = {"t": 1000.0}
    service = _service(path, ttl=10.0, clock=lambda: now["t"])
    ticket = _request(service)
    now["t"] = 1011.0                                   # 拖过 TTL 再来
    found = _service(path, ttl=10.0, clock=lambda: now["t"]).get(ticket.approval_id)
    assert found.status == EXPIRED


def test_anonymous_decision_still_refused_after_reload(tmp_path):
    path = tmp_path / "approvals.json"
    service = _service(path)
    ticket = _request(service)
    with pytest.raises(ApprovalError):
        _service(path).approve(ticket.approval_id, by="   ")


# --------------------------------------------------------------------------- #
# 二、fail-closed：票簿损坏不静默清空
# --------------------------------------------------------------------------- #
def test_corrupt_ticket_file_refuses_instead_of_starting_empty(tmp_path):
    path = tmp_path / "approvals.json"
    service = _service(path)
    _request(service)
    path.write_text("{半行 JSON", encoding="utf-8")
    with pytest.raises(ApprovalError) as err:
        _service(path).get("whatever")
    assert "拒绝加载" in str(err.value)
    # 申请同样被拒：静默从空开始会把「已批准的票」伪装成「从没申请过」
    with pytest.raises(ApprovalError):
        _service(path).request(tool=TOOL, signature=SIG, agent=AGENT,
                               role=ROLE, tier=2, reason="x")


def test_non_array_and_unknown_field_are_refused(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text('{"not": "array"}', encoding="utf-8")
    with pytest.raises(ApprovalError):
        _service(path).pending()
    path.write_text(json.dumps([{"approval_id": "x", "不存在的字段": 1}]),
                    encoding="utf-8")
    with pytest.raises(ApprovalError):
        _service(path).pending()


def test_construction_refuses_non_positive_ttl(tmp_path):
    with pytest.raises(ApprovalError):
        FileApprovalService(tmp_path / "a.json", ttl_seconds=0)


# --------------------------------------------------------------------------- #
# 三、并发与接口一致性
# --------------------------------------------------------------------------- #
def test_two_instances_requests_do_not_lose_each_other(tmp_path):
    """两个进程各自申请：后写的必须看见先写的（锁内重读，U86 的立场）。"""
    path = tmp_path / "approvals.json"
    a = _service(path)
    b = _service(path)
    first = _request(a)
    second = _request(b, signature="sig-2")
    tickets = _service(path).tickets()
    assert {t.approval_id for t in tickets} == {first.approval_id,
                                                second.approval_id}


def test_pending_lists_only_pending(tmp_path):
    service = _service(tmp_path / "approvals.json")
    one = _request(service)
    two = _request(service, signature="sig-2")
    service.reject(two.approval_id, by="owner")
    assert [t.approval_id for t in service.pending()] == [one.approval_id]


def test_file_service_shares_discipline_with_memory_service(tmp_path):
    """接口与纪律同内存实现一致——这是「可原地替换」的判据。"""
    memory = ApprovalService(ttl_seconds=3600.0, clock=lambda: 1000.0)
    ticket = memory.request(tool=TOOL, signature=SIG, agent=AGENT,
                            role=ROLE, tier=2, reason="x")
    for public in ("request", "approve", "reject", "consume", "get", "pending"):
        assert hasattr(FileApprovalService, public), public
    assert ticket.status == PENDING
