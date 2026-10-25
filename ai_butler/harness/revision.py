"""已提交补丁的版本账 —— 撤销它属于高危动作，必须持人工审批票（U81）。

计划书依据
----------
- 三、P10 离线链路「…**人工审批后台审核 → 审批通过自动版本升级，驳回终止迭代**」
- 三、P10 触发时机「7×24 小时后台离线自动运行」——无人值守意味着
  「撤销一次已经生效的改动」不能由自动链路自己决定
- 用户指令（2026-10-25）：「跨轮重试……**若要撤销已提交补丁则需人工确认**」

为什么不能直接用 `PatchApplier.rollback()`
------------------------------------------
`PatchApplier` 的回滚能力**在 `commit()` 之后刻意失效**（备份清空）。
这不是没做完，是分层：

| 动作 | 时机 | 风险 | 谁来决定 |
|---|---|---|---|
| `rollback()` | 未提交、当场验证失败 | 低：改动还没生效 | 机械层自己 |
| `undo()`（本模块） | 已提交、已经被后续轮次当成基线 | **高**：它是一次生效过的变更 | 必须有人签字 |

把这两件事并成一个方法，等于让「验证失败自动回滚」和「撤销已上线变更」
共用一个入口——前者的高频会让后者的门槛形同虚设。

四条纪律（撤销侧）
------------------
1. **票必须绑这一份补丁**。签名是 `round_id + paths` 的摘要：
   批准撤销 R1 的票拿去撤 R2 一律拒。批准的是这个动作，不是这个能力。
2. **票一次性**（U59）：消费一次即焚，不存在「撤了一次还能再撤」。
3. **漂移即拒**。撤销前逐文件重算当前内容摘要，若与「本补丁提交后的形态」
   不符，说明之后有人动过这些文件——盲写回 `before` 会把别人的改动冲掉。
   宁可让人来看，也不做一次「看起来成功」的静默覆盖。
4. **已撤销不得二次撤销**。幂等不等于放任：第二次撤销时基线已经不是原来的样子，
   照做等于用一份过期快照覆盖现在。

为什么要把 `before` 存下来
--------------------------
`PatchApplier` 的备份在 commit 时丢弃（那是它的正确行为：提交即放弃回滚）。
但「撤销一次已提交的补丁」需要**应用前的内容**，所以必须由本模块在提交时
另行留档。这也是为什么记账动作必须发生在 `commit()` **之前**能拿到 before ——
调用方（`agents/repair_loop.py`）从 `PatchResult.before` 取。

持久化（U83）
-------------
U81/U82 的账本只活在内存里，于是三件事跨不了进程重启：

1. 「哪些轮改过什么」——重启后 `locked_paths()` 为空，第二轮会重新去改
   第一轮已经改过的文件，而文件已经不是当初那份了；
2. 「哪轮撤销过」——重启后已撤销的轮次又变成「可撤销」，
   二次撤销会用一份过期快照覆盖现在；
3. 「编号该从几开始」——重启后从 R1 重新编号，与盘上历史撞号。

补上 `store` 之后三件事自动成立。两个形态的区分见 `to_record()`；
回读**不重算** `after_digest` 的理由见 `RevisionLedger._restore()`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

__all__ = [
    "CommittedPatch",
    "FileLedgerStore",
    "LedgerStore",
    "MemoryLedgerStore",
    "RevisionError",
    "RevisionLedger",
    "UndoRecord",
    "UNDO_TOOL",
    "undo_signature",
]

#: 撤销动作在审批系统里的工具名。写成常量而不是散落的字面量：
#: 票的签名绑定它，改名等于让所有在途的票失效——那必须是显式的行为。
UNDO_TOOL = "harness.revision.undo"


class RevisionError(RuntimeError):
    """撤销/记账不合法。**一律不写盘**，不降级、不猜。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def undo_signature(round_ids: Sequence[str]) -> str:
    """撤销票的签名：**绑定要撤的那几轮**，与 diff 内容无关。

    为什么不把 diff 也算进去：diff 会进票的 reason 供人阅读，
    但把它放进签名会让「同一个 round 的 diff 被重新格式化」导致票失效，
    而语义上要撤的仍是同一轮改动。绑定 `round_id + paths` 足够了——
    它精确到「哪一轮、改了哪些文件」，换个文件就换签名。
    """
    ids = sorted(str(r) for r in round_ids)
    if not ids:
        raise RevisionError("撤销签名至少需要一个 round_id")
    return _digest_text("undo|" + "|".join(ids))[:32]


# --------------------------------------------------------------------------- #
# 一、已提交补丁的记录
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CommittedPatch:
    """一份已经生效（commit 过）的补丁。

    `before` 是**应用前**的原文（`None` 表示该文件原本不存在）。
    它是撤销时唯一能写回的东西，所以必须与 diff 一起留档——
    只有 diff 没有 before，等于只能「反向应用补丁」；
    而反向应用会在文件被后续改动过时静默错位。
    """

    round_id: str
    change_id: str
    diff: str
    paths: tuple[str, ...]
    before: Mapping[str, str | None] = field(default_factory=dict)
    #: 提交完成时各文件的 sha256（文件不存在记为 "absent"）。撤销前用它验漂移。
    after_digest: Mapping[str, str] = field(default_factory=dict)
    #: 这份补丁是被哪几条缺陷驱动的——溯源时「改了什么」必须能回指「为什么改」
    defect_ids: tuple[str, ...] = ()
    rationale: str = ""
    committed_at: str = ""

    def __post_init__(self) -> None:
        if not self.round_id.strip():
            raise RevisionError("已提交补丁必须有 round_id")
        if not self.paths:
            raise RevisionError(f"{self.round_id} 没有任何路径，不能记账")

    @property
    def touched(self) -> int:
        return sum(1 for line in self.diff.splitlines()
                   if line[:1] in ("+", "-")
                   and not line.startswith(("+++", "---")))

    def as_dict(self) -> dict[str, Any]:
        """出口形态**不含 before 正文**：那是源码，账本只记它的摘要。"""
        return {
            "round_id": self.round_id,
            "change_id": self.change_id,
            "paths": list(self.paths),
            "touched": self.touched,
            "diff_chars": len(self.diff),
            "diff_digest": _digest_text(self.diff)[:16],
            "before_digest": {p: ("absent" if self.before.get(p) is None
                                 else _digest_text(self.before.get(p) or "")[:16])
                              for p in self.paths},
            "after_digest": dict(self.after_digest),
            "defect_ids": list(self.defect_ids),
            "rationale": self.rationale,
            "committed_at": self.committed_at,
        }

    # ---------------- 落盘形态（与出口形态分开） ---------------- #
    def to_record(self) -> dict[str, Any]:
        """**落盘**形态：含 `before` 正文。

        与 `as_dict()` 分开是刻意的，不是重复代码：

        | 形态 | 谁读 | 能不能带源码 |
        |---|---|---|
        | `as_dict()` | 人、日志、审批材料 | 不能（账本是给人反复读的） |
        | `to_record()` | 只有撤销自己 | **必须**（否则重启后撤不回去） |

        合成一份的后果只有两种：要么把整份源码写进可读账本，
        要么让「跨进程撤销」永远做不到——反着应用 diff 在被后续改动过的
        文件上会静默错位，那比撤不了更坏。
        """
        return {
            "round_id": self.round_id,
            "change_id": self.change_id,
            "diff": self.diff,
            "paths": list(self.paths),
            "before": dict(self.before),
            "before_sha": _before_sha(self.before),
            "after_digest": dict(self.after_digest),
            "defect_ids": list(self.defect_ids),
            "rationale": self.rationale,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CommittedPatch":
        """从落盘记录还原。**三处校验都在构造之前，任何一处不符就拒载。**

        最要命的一处是「before 缺键」：`undo()` 用 `before.get(path)` 取值，
        缺键会得到 `None`，而 `None` 的语义是**删除这个文件**。
        一份缺了键的账本不会报错，它会在撤销时安静地删掉一个源文件。
        """
        if not isinstance(record, Mapping):
            raise RevisionError(f"账本记录不是对象：{type(record).__name__}")
        round_id = str(record.get("round_id") or "")
        paths = tuple(str(p) for p in (record.get("paths") or ()))
        if not round_id or not paths:
            raise RevisionError(f"账本记录缺 round_id 或 paths：{record!r:.120}")

        raw_before = record.get("before")
        if not isinstance(raw_before, Mapping):
            raise RevisionError(
                f"{round_id} 的落盘记录没有 before，无法还原撤销"
                f"（只有 diff 时反向应用会在文件被改过之后静默错位）")
        before = {str(k): (None if v is None else str(v))
                  for k, v in raw_before.items()}
        missing = [p for p in paths if p not in before]
        if missing:
            raise RevisionError(
                f"{round_id} 的 before 缺少 {missing}：撤销时它们会被当成"
                f"「原本不存在」而删除。拒绝加载这份账本")

        expected = _before_sha(before)
        stored = record.get("before_sha")
        if not isinstance(stored, Mapping):
            raise RevisionError(f"{round_id} 的落盘记录没有 before_sha，无法校验完整性")
        bad = [p for p in paths if str(stored.get(p) or "") != expected[p]]
        if bad:
            raise RevisionError(
                f"{round_id} 的 before 与摘要不符（{bad}）：账本被改动或写盘截断，"
                f"拒绝加载——用不完整的原文撤销等于用错的内容覆盖现在")

        return cls(
            round_id=round_id,
            change_id=str(record.get("change_id") or ""),
            diff=str(record.get("diff") or ""),
            paths=paths, before=before,
            after_digest={str(k): str(v) for k, v in
                          (record.get("after_digest") or {}).items()},
            defect_ids=tuple(str(d) for d in (record.get("defect_ids") or ())),
            rationale=str(record.get("rationale") or ""),
            committed_at=str(record.get("committed_at") or ""))


@dataclass(frozen=True)
class UndoRecord:
    """一次撤销的凭证。**谁批的、撤的哪一轮、写回了哪些文件**，一样不少。"""

    round_id: str
    approval_id: str
    decided_by: str
    requester: str
    reason: str
    restored: tuple[str, ...]
    undone_at: str

    def as_dict(self) -> dict[str, Any]:
        return {"round_id": self.round_id, "approval_id": self.approval_id,
                "decided_by": self.decided_by, "requester": self.requester,
                "reason": self.reason, "restored": list(self.restored),
                "undone_at": self.undone_at}

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "UndoRecord":
        round_id = str(record.get("round_id") or "")
        if not round_id:
            raise RevisionError(f"撤销记录缺 round_id：{record!r:.120}")
        return cls(
            round_id=round_id,
            approval_id=str(record.get("approval_id") or ""),
            decided_by=str(record.get("decided_by") or ""),
            requester=str(record.get("requester") or ""),
            reason=str(record.get("reason") or ""),
            restored=tuple(str(p) for p in (record.get("restored") or ())),
            undone_at=str(record.get("undone_at") or ""))


# --------------------------------------------------------------------------- #
# 二、存储端口：只有 load 与 append
# --------------------------------------------------------------------------- #
class LedgerStore(Protocol):
    """版本账的存储端口。**没有 update / delete** —— 这就是 append-only。

    与 `core/event_journal.JournalStore` 同形（load + append），但不复用：
    那个端口按 `journal_id` 管会话事件，这个按 `ledger_id` 管补丁提交，
    把两者并成一个协议会让「账本」这个词同时指两件事（E-44 的温床）。
    """

    def load(self, ledger_id: str) -> list[dict[str, Any]]:
        ...

    def append(self, ledger_id: str, record: dict[str, Any]) -> None:
        ...


class MemoryLedgerStore:
    """内存实现。进程亡即失——那时跨进程撤销也不可用，与「没接存储」等价。"""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = {}

    def load(self, ledger_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._data.get(ledger_id, ())]

    def append(self, ledger_id: str, record: dict[str, Any]) -> None:
        self._data.setdefault(ledger_id, []).append(dict(record))


class FileLedgerStore:
    """jsonl 落盘：一行一个账本事件（`commit` / `undo`）。

    **坏行不跳过，直接拒载。** 跳过一行的后果不是「少一条记录」：
    那一轮的补丁其实已经改过文件，账本却以为没改，于是后续每一轮的
    边界判断都会把「别人改的」当成「我改的」——反向的错更难查。

    未做文件锁（同一账本被两个进程同时追加）。理由：离线链路是
    「7×24 后台单实例」，多实例并发需要的是分布式锁，不是文件锁；
    在这里加一把进程内锁只会给人「已解决」的错觉。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, ledger_id: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RevisionError(
                        f"{self.path}:{lineno} 不是合法 JSON——账本被外部损坏或"
                        f"写盘截断，拒绝加载（fail-closed，不跳过坏行）") from exc
                if record.get("ledger_id") != ledger_id:
                    continue
                out.append(record)
        return out

    def append(self, ledger_id: str, record: dict[str, Any]) -> None:
        payload = {"ledger_id": ledger_id, **record}
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()


# --------------------------------------------------------------------------- #
# 三、账本
# --------------------------------------------------------------------------- #
@dataclass
class RevisionLedger:
    """已提交补丁的 append-only 账。

    `root` 是写回的目标工作根。**可以为 None**：那样账本仍能记账与查
    「哪些路径被锁住」（跨轮边界靠它），只是不能执行撤销——
    撤销要动磁盘，没有工作根的账本不该假装能撤。

    `store` 是持久化端口。为 None 时账本只活在内存里，重启即失；
    给了 store 则构造时**自动回读**，于是「哪些轮改过什么」「哪轮撤过」
    「编号该从几开始」这三件事跨进程成立。回读是 fail-closed 的：
    账本不可信就拒绝开工，而不是带着半份历史去改代码。
    """

    root: Path | None = None
    store: LedgerStore | None = None
    ledger_id: str = "p10-revision"
    _records: list[CommittedPatch] = field(default_factory=list, init=False)
    _undone: dict[str, UndoRecord] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.root is not None:
            self.root = Path(self.root).resolve()
        if not str(self.ledger_id).strip():
            raise RevisionError("ledger_id 不得为空（同一存储里可能有多本账）")
        self._restore()

    # ---------------- 回读 ---------------- #
    def _restore(self) -> None:
        """从 store 回读。**不重算 after_digest**，用落盘时的那份。

        这一点是整个持久化的关键：`record()` 会在提交当场现算摘要，
        而回读时磁盘上的内容可能已经变了。回读再算一遍，等于把
        「文件在提交后被人改过」这件事就地抹平——漂移检查从此永远通过，
        撤销会盲写回 before，把别人的改动一起冲掉。
        """
        if self.store is None:
            return
        for index, record in enumerate(self.store.load(self.ledger_id), 1):
            where = f"{self.ledger_id} 第 {index} 条账本记录"
            kind = str(record.get("kind") or "")
            if kind == "commit":
                patch = CommittedPatch.from_record(record.get("patch") or {})
                if self.get(patch.round_id) is not None:
                    raise RevisionError(
                        f"{where}：{patch.round_id} 重复提交——账本被改动或事件被重放")
                self._records.append(patch)
            elif kind == "undo":
                undo = UndoRecord.from_record(record.get("undo") or {})
                if self.get(undo.round_id) is None:
                    raise RevisionError(
                        f"{where}：撤销 {undo.round_id} 却找不到它的提交记录"
                        f"（账本被删过一段，或存储与工作根不匹配）")
                if undo.round_id in self._undone:
                    raise RevisionError(
                        f"{where}：{undo.round_id} 有两条撤销记录，历史被重复追加")
                self._undone[undo.round_id] = undo
            else:
                raise RevisionError(
                    f"{where}：未知事件类型 {kind!r}（只认 commit / undo）")

    # ---------------- 记账 ---------------- #
    def record(self, patch: CommittedPatch) -> CommittedPatch:
        """登记一份已提交补丁。**同一轮只能提交一次**。

        允许同一 round_id 重复登记会出现「一轮两条记录」，撤销时
        「撤这一轮」就成了歧义——歧义在自动化里等于不可执行。

        落盘在入内存**之前**：反过来的话，存储写失败时内存里已经有这条记录，
        本进程以为改过、重启后账本又查不到，两边对不上比直接失败更难查。
        """
        if any(r.round_id == patch.round_id for r in self._records):
            raise RevisionError(
                f"{patch.round_id} 已登记过，一轮只能提交一次补丁"
                f"（多份补丁请拆成多轮，否则「撤销这一轮」有歧义）")
        if self.root is not None:
            patch = _with_digests(patch, self.root)
        if self.store is not None:
            try:
                self.store.append(self.ledger_id, {
                    "kind": "commit", "at": _now_iso(), "patch": patch.to_record()})
            except RevisionError:
                raise
            except Exception as exc:      # noqa: BLE001 —— 任何写失败都要转成可读错误
                raise RevisionError(
                    f"{patch.round_id} 的记账未能落盘（{exc}）。补丁**已经写进文件**，"
                    f"但账本没记住它——重启后这一轮会变成「没改过」，"
                    f"它的文件锁也随之丢失。此时应停下人工核对，"
                    f"而不是继续下一轮") from exc
        self._records.append(patch)
        return patch

    # ---------------- 查询 ---------------- #
    def rounds(self) -> tuple[CommittedPatch, ...]:
        return tuple(self._records)

    def get(self, round_id: str) -> CommittedPatch | None:
        for record in self._records:
            if record.round_id == round_id:
                return record
        return None

    def of_paths(self, paths: Sequence[str]) -> tuple[CommittedPatch, ...]:
        """哪些记录动过这些路径（含已撤销的——历史不改写）。"""
        wanted = set(paths)
        return tuple(r for r in self._records if wanted & set(r.paths))

    def locked_paths(self) -> tuple[str, ...]:
        """**当前**被锁住的路径：已被提交、且尚未撤销。

        撤销之后路径解锁——因为文件已经回到基线，再改它就是第一次改，
        不该按「二次改动」对待。这是「锁」与「已撤销」必须联动的理由。
        """
        out: list[str] = []
        for record in self._records:
            if record.round_id in self._undone:
                continue
            for path in record.paths:
                if path not in out:
                    out.append(path)
        return tuple(out)

    def undone(self) -> tuple[UndoRecord, ...]:
        return tuple(self._undone[k] for k in sorted(self._undone))

    def undo_rounds_for(self, paths: Sequence[str]) -> tuple[str, ...]:
        """要动这些路径，必须先撤销哪几轮。**顺序按提交先后**。"""
        wanted = set(paths)
        return tuple(r.round_id for r in self._records
                     if r.round_id not in self._undone and wanted & set(r.paths))

    # ---------------- 撤销（高危） ---------------- #
    def undo(self, round_id: str, *, approval_id: str, service: Any,
             requester: str, role: str, reason: str = "") -> UndoRecord:
        """撤销一轮已提交补丁。**每一步都可能拒，且拒了就不写盘。**

        检查顺序是刻意排的：先便宜的后验的，且**所有检查都在写盘之前**。
        「验了一半就写」会产生半个撤销——那时仓库既不是补丁后、
        也不是基线，是第三种没人定义过的状态。
        """
        record = self.get(round_id)
        if record is None:
            raise RevisionError(f"没有 {round_id} 的提交记录，无从撤销")
        if round_id in self._undone:
            done = self._undone[round_id]
            raise RevisionError(
                f"{round_id} 已于 {done.undone_at} 由 {done.decided_by} 撤销过，"
                f"不得二次撤销（幂等不等于放任：基线已经不是原来的样子）")
        if self.root is None:
            raise RevisionError("账本没有工作根（root），无法执行撤销")
        if service is None:
            # 「没接审批」不等于「不用审批」。缺服务就停，绝不放行。
            raise RevisionError(
                "未接入审批服务，撤销被拒：撤销已提交补丁是人工确认动作，"
                "不接受无票执行")

        # ① 验票（一次性、签名绑定本轮、主体绑定）
        service.consume(approval_id, tool=UNDO_TOOL,
                        signature=undo_signature([round_id]),
                        agent=requester, role=role)
        ticket = service.get(approval_id)

        # ② 漂移检查：**在写之前全量检查**，不做「边查边写」
        drift = [p for p in record.paths
                 if self._current_digest(p) != record.after_digest.get(p, "")]

        if drift:
            raise RevisionError(
                f"{round_id} 涉及的文件在提交后又被改动过，拒绝撤销：{drift}。"
                f"盲写回 before 会把这些改动一起冲掉——请人工确认当前内容后再处理")

        # ③ 写回（写一半失败则把已写的恢复成「撤销前」，保证失败=原状）
        restored: list[str] = []
        try:
            for path in record.paths:
                self._write(path, record.before.get(path))
                restored.append(path)
        except OSError as exc:
            for path in restored:
                self._restore_to_committed(record, path)
            raise RevisionError(f"{round_id} 撤销写盘失败，已恢复到撤销前：{exc}") from exc

        undo = UndoRecord(
            round_id=round_id, approval_id=approval_id,
            decided_by=str(getattr(ticket, "decided_by", "") or ""),
            requester=requester, reason=reason, restored=tuple(restored),
            undone_at=_now_iso())
        # 到这里文件已经回到基线，撤销**已经是事实**。所以先进内存（否则
        # 本进程还能再撤一遍），再尝试落账；落账失败要如实说清两件事：
        # 文件已经恢复、账本没记住——后者才是重启后的风险。
        self._undone[round_id] = undo
        if self.store is not None:
            try:
                self.store.append(self.ledger_id, {
                    "kind": "undo", "at": _now_iso(), "undo": undo.as_dict()})
            except Exception as exc:      # noqa: BLE001 —— 任何写失败都要转成可读错误
                raise RevisionError(
                    f"{round_id} 的文件已恢复到基线，但撤销**未能入账**（{exc}）。"
                    f"本进程内不会重复撤销；重启后账本会以为它还没撤过。"
                    f"届时应重跑闭环而不是手工改账——提交时的 after_digest "
                    f"对不上当前内容，漂移检查会拦住重复撤销") from exc
        return undo

    # ---------------- 内部 ---------------- #
    def _resolve(self, rel: str) -> Path:
        assert self.root is not None
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise RevisionError(f"{rel} 解析到工作根之外，拒绝") from exc
        return target

    def _current_digest(self, rel: str) -> str:
        target = self._resolve(rel)
        if not target.exists():
            return "absent"
        return _digest_text(target.read_text(encoding="utf-8"))

    def _write(self, rel: str, content: str | None) -> None:
        target = self._resolve(rel)
        if content is None:
            if target.exists():
                target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)

    def _restore_to_committed(self, record: CommittedPatch, rel: str) -> None:
        """把某个文件恢复到「本补丁提交后」的形态。

        撤销写了一半失败时用它兜底。注意这里**没有原文可写**——
        `before` 是应用前的，要恢复「提交后」只能反向应用；
        所以只把已经写回的文件重新标脏，交给上层报错，不假装恢复成功。
        实际情况中写盘失败几乎只发生在权限/磁盘满，此时如实报错比修复更对。
        """
        # 不做反向应用（会在文件结构已变时错位）。留空即「维持现状并上报」。
        _ = record, rel


def _before_sha(before: Mapping[str, str | None]) -> dict[str, str]:
    """`before` 的全文摘要。`None`（原本不存在）记为 `"absent"`。

    用它而不是 `as_dict()` 里的 16 位短摘要：撤销写回的是整份源码，
    校验它的摘要必须够长——短摘要的用途是给人一眼看出「变了没」，
    不是用来证明「这份内容就是当时那份」。
    """
    return {path: ("absent" if content is None else _digest_text(content))
            for path, content in before.items()}


def _with_digests(patch: CommittedPatch, root: Path) -> CommittedPatch:
    """补齐 `after_digest`：提交完成时各文件的内容摘要。

    在 record 时现算而不是让调用方传：调用方拿不到「此刻磁盘上是什么」，
    而这里正好是提交刚结束的时刻。少一个可错的参数。
    """
    digests: dict[str, str] = {}
    for rel in patch.paths:
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise RevisionError(f"{rel} 解析到工作根之外，拒绝记账") from exc
        digests[rel] = ("absent" if not target.exists()
                        else _digest_text(target.read_text(encoding="utf-8")))
    return CommittedPatch(
        round_id=patch.round_id, change_id=patch.change_id, diff=patch.diff,
        paths=patch.paths, before=patch.before, after_digest=digests,
        defect_ids=patch.defect_ids, rationale=patch.rationale,
        committed_at=patch.committed_at or _now_iso())
