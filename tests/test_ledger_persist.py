"""已提交补丁账本的持久化（U83）。

U81/U82 的账本只活在内存里，于是三件事跨不了进程重启：**哪些轮改过什么**、
**哪轮撤过**、**编号该从几开始**。补存储端口之后这三件事自动成立，
但代价是「账本文件本身成了不可信输入」——它可能被改、被截断、被删掉一段。

所以这一组测试的重点不是「能不能存下来」，而是**存坏了的账本会不会被当成好的用**：

- `before` 缺键 → 撤销时 `before.get(path)` 得到 `None`，`None` 的意思是删文件；
- 回读重算摘要 → 漂移检查永远通过，撤销盲写回旧内容，冲掉别人的改动；
- 坏行跳过 → 这一轮的文件其实改过，账本却以为没改，之后每轮的边界都错；
- 孤立 undo / 重复 commit → 历史被删改过，不能接着用。

每条都配一个「正常路径」的对照，否则「拒载」也可能只是判据太严。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from ai_butler.agents.repair_loop import REPAIR_USER_ID, RepairLoop, build_repair_loop
from ai_butler.core.approval import ApprovalService
from ai_butler.harness.revision import (
    CommittedPatch,
    FileLedgerStore,
    MemoryLedgerStore,
    RevisionError,
    RevisionLedger,
    undo_signature,
)
from ai_butler.harness.trail import RepairTrail, RoundRecord

_PLANNER = "ai_butler/retrieval/planner.py"
_CHAT = "ai_butler/retrieval/chat.py"
_ORIGINAL = "return SMALL_TALK\nsecond\n"
_PATCHED = "return UNKNOWN\nsecond\n"
_DIFF = (f"--- a/{_PLANNER}\n+++ b/{_PLANNER}\n@@ -1,1 +1,1 @@\n"
         f"-return SMALL_TALK\n+return UNKNOWN\n")


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
def _repo(tmp_path: Path) -> Path:
    """补丁**已应用**的工作根：`before` 是原文，磁盘上是改后的内容。"""
    root = tmp_path / "repo"
    (root / "ai_butler" / "retrieval").mkdir(parents=True, exist_ok=True)
    (root / _PLANNER).write_text(_PATCHED, encoding="utf-8")
    return root


def _patch(round_id: str = "R1", *, path: str = _PLANNER,
           before: str | None = _ORIGINAL) -> CommittedPatch:
    return CommittedPatch(round_id=round_id, change_id=f"round:{round_id}",
                          diff=_DIFF, paths=(path,), before={path: before},
                          rationale="历史补丁")


def _store(tmp_path: Path) -> FileLedgerStore:
    return FileLedgerStore(tmp_path / "ledger.jsonl")


def _service() -> ApprovalService:
    return ApprovalService(ttl_seconds=3600)


def _approved(service: ApprovalService, round_ids: list[str]) -> str:
    ticket = service.request(tool="harness.revision.undo",
                             signature=undo_signature(round_ids),
                             agent=REPAIR_USER_ID, role="p10-harness",
                             tier=2, reason="撤销测试")
    service.approve(ticket.approval_id, by="reviewer")
    return ticket.approval_id


def _undo(ledger: RevisionLedger, round_id: str) -> Any:
    service = _service()
    return ledger.undo(round_id, approval_id=_approved(service, [round_id]),
                       service=service, requester=REPAIR_USER_ID,
                       role="p10-harness", reason="测试")


def _write_rows(tmp_path: Path, rows: list[Mapping[str, Any]]) -> FileLedgerStore:
    """手工写一份账本文件 —— 用来模拟「被改过 / 被截断」的账本。"""
    path = tmp_path / "ledger.jsonl"
    path.write_text("".join(json.dumps(dict(r), ensure_ascii=False) + "\n"
                            for r in rows), encoding="utf-8")
    return FileLedgerStore(path)


# --------------------------------------------------------------------------- #
# 一、正常路径：重启之后账本还在
# --------------------------------------------------------------------------- #
def test_ledger_reloads_rounds_from_file(tmp_path):
    root, store = _repo(tmp_path), _store(tmp_path)
    first = RevisionLedger(root=root, store=store)
    first.record(_patch("R1"))
    first.record(_patch("R2", path=_CHAT, before="x\n"))

    second = RevisionLedger(root=root, store=store)
    assert [r.round_id for r in second.rounds()] == ["R1", "R2"]
    # 跨轮边界靠它：重启后仍知道哪些文件被改过
    assert second.locked_paths() == (_PLANNER, _CHAT)
    # 出口形态不带 before 正文，落盘形态带 —— 两边不能混
    assert "before" not in second.get("R1").as_dict()
    assert second.get("R1").before[_PLANNER] == _ORIGINAL


def test_cross_process_undo_writes_files_back(tmp_path):
    """A 进程记账，B 进程撤销 —— 这才是持久化存在的理由。"""
    root, store = _repo(tmp_path), _store(tmp_path)
    RevisionLedger(root=root, store=store).record(_patch("R1"))
    assert (root / _PLANNER).read_text(encoding="utf-8") == _PATCHED

    writer = RevisionLedger(root=root, store=store)   # 模拟另一个进程
    record = _undo(writer, "R1")
    assert record.decided_by == "reviewer"
    assert (root / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL


def test_reload_keeps_undo_state(tmp_path):
    root, store = _repo(tmp_path), _store(tmp_path)
    ledger = RevisionLedger(root=root, store=store)
    ledger.record(_patch("R1"))
    _undo(ledger, "R1")

    again = RevisionLedger(root=root, store=store)
    assert again.locked_paths() == ()                  # 撤销后解锁，跨进程成立
    assert [u.round_id for u in again.undone()] == ["R1"]
    with pytest.raises(RevisionError) as exc:
        _undo(again, "R1")                             # 重启不会重新允许二次撤销
    assert "二次撤销" in str(exc.value)


def test_reload_uses_stored_digest_not_current_file(tmp_path):
    """回读**不能**重算 after_digest，否则漂移检查永远通过。

    错误实现的后果：提交后有人手动改了这个文件，重启后撤销仍然通过，
    然后把那份手改的内容一起冲掉——正是「盲写回 before」要防的事。
    """
    root, store = _repo(tmp_path), _store(tmp_path)
    RevisionLedger(root=root, store=store).record(_patch("R1"))
    (root / _PLANNER).write_text("third\n", encoding="utf-8")

    again = RevisionLedger(root=root, store=store)
    with pytest.raises(RevisionError) as exc:
        _undo(again, "R1")
    assert "又被改动过" in str(exc.value)
    assert (root / _PLANNER).read_text(encoding="utf-8") == "third\n"


# --------------------------------------------------------------------------- #
# 二、坏账本一律拒载（fail-closed）
# --------------------------------------------------------------------------- #
def test_missing_before_key_is_refused(tmp_path):
    """缺键的后果不是「少写一个文件」，是**删掉一个源文件**。

    `undo()` 里是 `before.get(path)`：缺键 → `None` → `_write()` 走删除分支。
    这一条是整组里最该存在的——它不报错，它删文件。
    """
    record = _patch("R1").to_record()
    record["before"] = {}
    store = _write_rows(tmp_path, [
        {"kind": "commit", "ledger_id": "p10-revision", "patch": record}])
    with pytest.raises(RevisionError) as exc:
        RevisionLedger(root=_repo(tmp_path), store=store)
    assert "before 缺少" in str(exc.value)


def test_tampered_before_is_refused(tmp_path):
    """改了 before 却没改摘要 → 拒。否则撤销会写回一份陌生的源码。"""
    record = _patch("R1").to_record()
    record["before"][_PLANNER] = "return EVIL\n"
    store = _write_rows(tmp_path, [
        {"kind": "commit", "ledger_id": "p10-revision", "patch": record}])
    with pytest.raises(RevisionError) as exc:
        RevisionLedger(root=_repo(tmp_path), store=store)
    assert "与摘要不符" in str(exc.value)


def test_broken_json_line_is_refused(tmp_path):
    """坏行**不跳过**：跳过等于让这一轮从历史上消失，而它的文件已经改过。"""
    store = _write_rows(tmp_path, [
        {"kind": "commit", "ledger_id": "p10-revision",
         "patch": _patch("R1").to_record()}])
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "commit", "patch": {oops\n')      # 写盘截断的形态
    with pytest.raises(RevisionError) as exc:
        RevisionLedger(root=_repo(tmp_path), store=store)
    assert "不是合法 JSON" in str(exc.value)


def test_unknown_event_kind_is_refused(tmp_path):
    store = _write_rows(tmp_path, [{"kind": "rollback", "ledger_id": "p10-revision"}])
    with pytest.raises(RevisionError) as exc:
        RevisionLedger(root=_repo(tmp_path), store=store)
    assert "未知事件类型" in str(exc.value)


def test_undo_without_commit_is_refused(tmp_path):
    """孤立 undo：说明账本被删过一段，剩下的也不能信。"""
    store = _write_rows(tmp_path, [{
        "kind": "undo", "ledger_id": "p10-revision",
        "undo": {"round_id": "R9", "approval_id": "x", "decided_by": "y",
                 "requester": "z", "reason": "", "restored": [],
                 "undone_at": ""}}])
    with pytest.raises(RevisionError) as exc:
        RevisionLedger(root=_repo(tmp_path), store=store)
    assert "找不到它的提交" in str(exc.value)


def test_duplicate_commit_is_refused(tmp_path):
    row = {"kind": "commit", "ledger_id": "p10-revision",
           "patch": _patch("R1").to_record()}
    store = _write_rows(tmp_path, [row, row])
    with pytest.raises(RevisionError) as exc:
        RevisionLedger(root=_repo(tmp_path), store=store)
    assert "重复提交" in str(exc.value)


def test_other_ledger_ids_are_ignored(tmp_path):
    """一本文件里多本账，各管各的 —— 不串味（对照组：拒载不能拒到别人头上）。"""
    rows = [{"kind": "commit", "ledger_id": "another",
             "patch": _patch("X1", path="gone.py", before=None).to_record()},
            {"kind": "commit", "ledger_id": "p10-revision",
             "patch": _patch("R1").to_record()}]
    ledger = RevisionLedger(root=_repo(tmp_path), store=_write_rows(tmp_path, rows))
    assert [r.round_id for r in ledger.rounds()] == ["R1"]


# --------------------------------------------------------------------------- #
# 三、写失败不留半份
# --------------------------------------------------------------------------- #
class _BrokenStore:
    """load 正常、append 失败：模拟磁盘满 / 只读挂载。"""

    def __init__(self, inner: MemoryLedgerStore, *, fail_after: int = 0) -> None:
        self.inner, self.fail_after = inner, fail_after
        self.calls = 0

    def load(self, ledger_id: str) -> list[dict[str, Any]]:
        return self.inner.load(ledger_id)

    def append(self, ledger_id: str, record: dict[str, Any]) -> None:
        self.calls += 1
        if self.calls > self.fail_after:
            raise OSError("No space left on device")
        self.inner.append(ledger_id, record)


def test_store_write_failure_leaves_memory_clean(tmp_path):
    """落盘失败时内存里也不能留这条记录：否则本进程以为改过、重启后查不到。"""
    store = _BrokenStore(MemoryLedgerStore())
    ledger = RevisionLedger(root=_repo(tmp_path), store=store)
    with pytest.raises(RevisionError) as exc:
        ledger.record(_patch("R1"))
    assert "未能落盘" in str(exc.value)
    assert ledger.rounds() == ()
    assert ledger.locked_paths() == ()


def test_undo_ledger_failure_says_file_already_restored(tmp_path):
    """撤销已写盘、落账失败：错误信息必须说清「文件已恢复、账本没记住」。

    说反了（比如报「撤销失败」）会引导人重试，而重试会被漂移检查挡住；
    说漏了（只报磁盘错误）则让重启后凭空多出一轮可撤销。
    """
    root = _repo(tmp_path)
    store = _BrokenStore(MemoryLedgerStore(), fail_after=1)   # 只让 undo 那次失败
    ledger = RevisionLedger(root=root, store=store)
    ledger.record(_patch("R1"))
    with pytest.raises(RevisionError) as exc:
        _undo(ledger, "R1")
    assert "已恢复到基线" in str(exc.value)
    assert "未能入账" in str(exc.value)
    # 本进程内不得重复撤销（文件已回原文，再撤一次就是覆盖）
    assert [u.round_id for u in ledger.undone()] == ["R1"]
    assert (root / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL


# --------------------------------------------------------------------------- #
# 四、装配与编号
# --------------------------------------------------------------------------- #
def test_round_numbering_continues_after_reload(tmp_path):
    """跨进程的编号续接：溯源账落盘 + 版本账落盘，重启后接在 R1 之后。

    这条是 U82 那个真机症状的重现——只是触发条件从「同进程第二次 run」
    换成了「另一个进程」。缺任何一半都不成立。
    """
    root, store = _repo(tmp_path), _store(tmp_path)
    trail_path = tmp_path / "trail.jsonl"
    first = RepairLoop(evaluator=None, harness=None,
                       ledger=RevisionLedger(root=root, store=store),
                       trail=RepairTrail(path=str(trail_path)))
    first.ledger.record(_patch("R1"))
    first.trail.append(_record("R1"))
    assert first._next_round_no() == 2

    second = build_repair_loop(patch_root=str(root), ledger_path=str(store.path),
                               trail_path=str(trail_path))
    assert second._next_round_no() == 2                # 不是 1
    assert second.ledger.get("R1") is not None          # 版本账也回读了


def test_ledger_without_store_stays_in_memory(tmp_path):
    """对照组：不给 store 就不该有持久化 —— 别把「默认落盘」偷偷加进来。"""
    root = _repo(tmp_path)
    RevisionLedger(root=root).record(_patch("R1"))
    assert RevisionLedger(root=root).rounds() == ()
    assert not (tmp_path / "ledger.jsonl").exists()


def test_build_repair_loop_wires_the_store(tmp_path):
    loop = build_repair_loop(patch_root=str(_repo(tmp_path)),
                             ledger_path=str(tmp_path / "ledger.jsonl"))
    assert isinstance(loop.ledger, RevisionLedger)
    assert isinstance(loop.ledger.store, FileLedgerStore)
    assert loop.trail is None                  # 没给 trail_path，就不落盘


def test_empty_ledger_id_is_refused(tmp_path):
    """账本 id 为空时，多本账会互相覆盖——这是构造期错误，不留到运行期。"""
    with pytest.raises(RevisionError):
        RevisionLedger(root=_repo(tmp_path), store=MemoryLedgerStore(),
                       ledger_id="  ")


# --------------------------------------------------------------------------- #
# 辅助：一条最小的轮次记录（只要 round_id 够编号用）
# --------------------------------------------------------------------------- #
def _record(round_id: str) -> RoundRecord:
    return RoundRecord(round_id=round_id, started_at="", verdict="unverified")
