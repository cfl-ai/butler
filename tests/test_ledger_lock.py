"""账本跨进程互斥（U86）与每行 fsync（U87）的守护。

这组测试的分工
--------------
- **锁本身**（一）：拿到/释放/不可重入/超时诊断/**跨进程真的互斥**/
  持有者退出后自动释放。最后两条必须起子进程：同一进程内的两次 open
  互斥只能证明 API 用了对，证明不了内核语义。
- **接线**（二、三）：本模块有没有真的把写动作放进临界区。
  判据是「另一个持有者挡住它」——只看「加了个 lock 参数」不算数。
- **重读**（二、三）：锁内先重读再判断。只加锁不重读，等于把窗口变窄，
  问题照旧。
- **落盘**（四）：三处 append 真的调了 `os.fsync`。
- **诊断**（五）：超时能说清「谁在写」，锁文件不留垃圾。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_butler.agents.repair_loop import REPAIR_USER_ID, build_repair_loop
from ai_butler.core import event_journal as journal_mod
from ai_butler.core.event_journal import EventJournal, FileJournalStore
from ai_butler.core.filelock import FileLock, LedgerLockTimeout, null_lock
from ai_butler.harness import revision as revision_mod
from ai_butler.harness import trail as trail_mod
from ai_butler.harness.revision import (
    CommittedPatch,
    FileLedgerStore,
    RevisionError,
    RevisionLedger,
)
from ai_butler.harness.trail import RepairTrail, RoundRecord, TrailError

# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
#: 子进程脚本：拿锁 → 报一声 → 持有一会儿 → 退出。
#: 报一声是必要的握手，否则父进程可能在它还没拿到锁时就去试，
#: 那样测到的是「它还没开始」，而不是「锁真的互斥」。
_HOLDER = """
import sys, time
from ai_butler.core.filelock import FileLock
with FileLock(sys.argv[1], timeout=10, purpose="子进程持锁") as lock:
    print("LOCKED", flush=True)
    time.sleep(float(sys.argv[2]))
print("DONE", flush=True)
"""


def _spawn_holder(lock_path: Path, hold: float) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path), str(hold)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(Path(__file__).resolve().parent.parent))
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line == "LOCKED", f"子进程没能拿锁：{line!r} {proc.stderr.read() if proc.stderr else ''}"
    return proc


def _patch(round_id: str = "R1", path: str = "a.py") -> CommittedPatch:
    return CommittedPatch(round_id=round_id, change_id=f"c-{round_id}", diff="x",
                          paths=(path,), before={path: "old\n"},
                          defect_ids=(), rationale="r", committed_at="2026-01-01T00:00:00")


def _round(round_id: str = "R1") -> RoundRecord:
    return RoundRecord(round_id=round_id, started_at="", verdict="unverified")


# --------------------------------------------------------------------------- #
# 一、锁本身
# --------------------------------------------------------------------------- #
def test_lock_can_be_taken_released_and_taken_again(tmp_path):
    lock = FileLock(tmp_path / "x.lock", timeout=1)
    with lock:
        assert lock._fd is not None
    with lock:
        assert lock._fd is not None
    assert lock._fd is None


def test_lock_refuses_to_be_reentered(tmp_path):
    """同一个对象二次进入必须**报错**，不能静默变成「锁还在我手里」。

    静默重入会让「我以为持着锁」和「其实上一段已经退出了」看起来一样。
    """
    lock = FileLock(tmp_path / "x.lock", timeout=1)
    with lock:
        with pytest.raises(RuntimeError, match="不可重入"):
            lock.__enter__()


def test_second_holder_times_out_with_holder_diagnosis(tmp_path):
    path = tmp_path / "x.lock"
    holder = FileLock(path, timeout=1, purpose="版本账")
    with holder:
        with pytest.raises(LedgerLockTimeout) as exc:
            FileLock(path, timeout=0.15).__enter__()
    text = str(exc.value)
    assert "0.15" in text
    # 关键：读得到「谁在里面」。没有这句，人只看到「等了 5 秒」，
    # 然后开始怀疑锁坏了、去删锁文件。
    assert f"pid {os.getpid()}" in text
    assert "版本账" in text


def test_timeout_message_says_not_to_delete_the_lock_file(tmp_path):
    """超时的正确处置是「等」或「查那个进程」，不是删文件。

    这条守的是错误信息本身：讲不清楚就会有人去删锁，而删锁正好
    制造出两个写者——比原来的问题严重得多。
    """
    path = tmp_path / "x.lock"
    with FileLock(path, timeout=1):
        with pytest.raises(LedgerLockTimeout) as exc:
            FileLock(path, timeout=0.1).__enter__()
    text = str(exc.value)
    assert "自动释放" in text
    assert "不需要删锁文件" in text
    assert "分布式锁" in text          # 多机场景指了路，而不是含糊带过


def test_lock_is_exclusive_across_processes(tmp_path):
    path = tmp_path / "x.lock"
    proc = _spawn_holder(path, hold=1.2)
    try:
        with pytest.raises(LedgerLockTimeout):
            FileLock(path, timeout=0.2).__enter__()
    finally:
        proc.wait(timeout=15)


def test_lock_is_released_when_holder_process_exits(tmp_path):
    """**持有者退出，内核自动放锁**——这就是不用「O_EXCL 标记文件」的理由。

    标记文件在持有者被 kill 之后会永远留在那儿，于是每个用它的人都得
    再写一套陈旧判定，而那套判定判错的后果是「人工删锁 → 两个写者同时进来」。
    """
    path = tmp_path / "x.lock"
    proc = _spawn_holder(path, hold=0.2)
    proc.wait(timeout=15)
    with FileLock(path, timeout=2):
        pass                       # 能拿到就说明没有残留


def test_null_lock_is_a_no_op():
    with null_lock():
        pass
    with null_lock():
        pass


# --------------------------------------------------------------------------- #
# 二、版本账接线
# --------------------------------------------------------------------------- #
def test_record_blocks_while_another_holder_is_inside(tmp_path):
    """record() 真的走了锁：别人持锁时它应该**超时**而不是照写。

    这条是接线判据——参数加了不等于用了。
    """
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    ledger = RevisionLedger(root=tmp_path, store=store)
    ledger.lock = FileLock(tmp_path / "ledger.jsonl.lock", timeout=0.15)
    with FileLock(tmp_path / "ledger.jsonl.lock", timeout=2):
        with pytest.raises(LedgerLockTimeout):
            ledger.record(_patch())


def test_record_rereads_under_lock_and_refuses_duplicate(tmp_path):
    """锁内重读：另一个进程刚写的 R1，本进程必须看见。

    不重读的查重只在本进程有效——两个进程各自 load 到 `{R1}` 之外的
    同一份历史，各自认为自己的轮次可用，然后各写一行。
    """
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    other = RevisionLedger(root=tmp_path, store=store)     # 模拟另一个进程
    other.record(_patch("R1"))

    mine = RevisionLedger(root=tmp_path, store=store)
    # 构造时已回读，但为了证明「是锁内重读而不是构造时那份」，
    # 把内存清空再记——重读必须自己把 R1 找回来。
    mine._records.clear()
    with pytest.raises(RevisionError, match="已登记过"):
        mine.record(_patch("R1"))
    assert [r.round_id for r in mine.rounds()] == ["R1"]   # 重读把记录恢复了


def test_undo_rereads_under_lock_and_refuses_second_undo(tmp_path):
    """撤销侧同理：另一个进程撤过之后，本进程必须拒绝二次撤销。

    检查与写盘之间留窗口的后果是「用一份过期快照覆盖刚写回的基线」。
    """
    from ai_butler.core.approval import ApprovalService
    from ai_butler.harness.revision import UNDO_TOOL, undo_signature

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("old\n", encoding="utf-8")
    store = FileLedgerStore(tmp_path / "ledger.jsonl")
    service = ApprovalService(ttl_seconds=3600)

    def _ticket() -> str:
        ticket = service.request(tool=UNDO_TOOL, signature=undo_signature(["R1"]),
                                 agent=REPAIR_USER_ID, role="p10-harness",
                                 tier=2, reason="撤销测试")
        service.approve(ticket.approval_id, by="reviewer")
        return ticket.approval_id

    other = RevisionLedger(root=repo, store=store)
    other.record(_patch("R1"))
    other.undo("R1", approval_id=_ticket(), service=service,
               requester=REPAIR_USER_ID, role="p10-harness")

    mine = RevisionLedger(root=repo, store=store)
    mine._undone.clear()                    # 假装本进程内存里没这条
    with pytest.raises(RevisionError, match="撤销过"):
        mine.undo("R1", approval_id=_ticket(), service=service,
                  requester=REPAIR_USER_ID, role="p10-harness")


def test_ledger_without_store_needs_no_lock(tmp_path):
    """纯内存账本没有第二个写者，加锁只是形式——空实现不该报错。"""
    ledger = RevisionLedger(root=tmp_path)
    ledger.record(_patch())
    assert ledger.get("R1") is not None


def test_memory_store_has_no_lock_hook(tmp_path):
    """内存存储没有 `lock()`，`_guard()` 要能优雅退化而不是 AttributeError。"""
    from ai_butler.harness.revision import MemoryLedgerStore

    ledger = RevisionLedger(root=tmp_path, store=MemoryLedgerStore())
    ledger.record(_patch())
    assert ledger.get("R1") is not None


# --------------------------------------------------------------------------- #
# 三、溯源账接线
# --------------------------------------------------------------------------- #
def test_trail_append_blocks_while_another_holder_is_inside(tmp_path):
    path = tmp_path / "trail.jsonl"
    trail = RepairTrail(path=path)
    with FileLock(f"{path}.lock", timeout=2):
        with pytest.raises(LedgerLockTimeout):
            trail.append(_round("R1"))


def test_trail_rereads_under_lock_and_refuses_duplicate(tmp_path):
    path = tmp_path / "trail.jsonl"
    other = RepairTrail(path=path)
    other.append(_round("R1"))

    mine = RepairTrail(path=path)
    mine._records.clear()                 # 模拟「本进程内存里什么都没有」
    with pytest.raises(TrailError, match="不能重复登记"):
        mine.append(_round("R1"))


def test_trail_without_path_is_a_no_op_lock(tmp_path):
    trail = RepairTrail()
    trail.append(_round("R1"))
    assert trail.get("R1") is not None


# --------------------------------------------------------------------------- #
# 四、每行 fsync
# --------------------------------------------------------------------------- #
def test_journal_store_fsyncs_every_line(tmp_path, monkeypatch):
    calls: list[int] = []
    real = os.fsync
    monkeypatch.setattr(journal_mod.os, "fsync",
                        lambda fd: (calls.append(fd), real(fd))[1])
    journal = EventJournal(journal_id="s1",
                           store=FileJournalStore(tmp_path / "j.jsonl"))
    journal.append("stage", {"n": 1})
    journal.append("stage", {"n": 2})
    assert len(calls) == 2


def test_ledger_store_fsyncs_every_line(tmp_path, monkeypatch):
    calls: list[int] = []
    real = os.fsync
    monkeypatch.setattr(revision_mod.os, "fsync",
                        lambda fd: (calls.append(fd), real(fd))[1])
    ledger = RevisionLedger(root=tmp_path,
                            store=FileLedgerStore(tmp_path / "l.jsonl"))
    ledger.record(_patch("R1"))
    ledger.record(_patch("R2", path="b.py"))
    assert len(calls) == 2


def test_trail_fsyncs_every_line(tmp_path, monkeypatch):
    calls: list[int] = []
    real = os.fsync
    monkeypatch.setattr(trail_mod.os, "fsync",
                        lambda fd: (calls.append(fd), real(fd))[1])
    trail = RepairTrail(path=tmp_path / "t.jsonl")
    trail.append(_round("R1"))
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# 五、诊断与卫生
# --------------------------------------------------------------------------- #
def test_lock_file_is_emptied_on_release(tmp_path):
    """释放后不留持有者信息。留下的话，下一次超时就会报一个**早就走了的** pid。"""
    path = tmp_path / "x.lock"
    with FileLock(path, timeout=1, purpose="x"):
        pass
    assert path.read_bytes() == b""


def test_build_repair_loop_wires_a_lock_for_file_ledger(tmp_path):
    """给了 `ledger_path` 就该自带锁——否则「落盘了」听起来像「安全了」。"""
    loop = build_repair_loop(patch_root=str(tmp_path),
                             ledger_path=str(tmp_path / "l.jsonl"))
    assert isinstance(loop.ledger.store, FileLedgerStore)
    guard = loop.ledger._guard()
    assert isinstance(guard, FileLock)
    with guard:
        pass
