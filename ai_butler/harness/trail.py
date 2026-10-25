"""轮次溯源账 —— 每一轮「问题在哪、改了什么、为什么这样改、判成什么样」。

计划书依据
----------
- 三、P10 步骤10「评测与版本管理：生成版本变更单、记录迭代日志」
- 三、P10 离线链路「达标后沉淀记忆、**生成版本变更单** → 人工审批后台审核」
- 用户指令（2026-10-25）：「每轮痕迹要有迹可循，能溯源知道**问题出现在哪、
  改了什么、为什么要这样改**」

为什么不能只靠 `p10_harness` / `eval_platform_recheck` 这两段 State
------------------------------------------------------------------
State 是**当前值**，不是历史。第二轮会覆盖第一轮的 `p10_harness`；
基线报告被晋升后，第一轮的现场就没了。于是最常见的问题变成了：

> 「这条缺陷是哪一轮改的？为什么当时那样改？后来为什么说它好了？」

要回答它，必须有一个**只追加、不覆盖**的账本，且每条记录的字段不是
「操作日志」而是**能自证的证据链**：

    缺陷（case + 维度 + 证据 + 定位提示）
      → 本轮允许改的板块与被锁的文件（边界）
        → 实际 diff + 命中的文件 + 行数
          → 模型给的理由（它为什么这样改）
            → 跑了什么命令、exit 多少（验了什么）
              → 复评结论与逐条缺陷的去留（判了什么）
                → 本轮之后错误信息变了没（有没有推进）

六段缺任何一段，「溯源」都会退化成「翻聊天记录」。

三条纪律
--------
1. **只追加**。历史不改写：某轮后来被撤销，也保留那一轮，另记撤销凭证。
   抹掉「改坏过」的那一版，等于把最有价值的教训一起删了。
2. **出口必过 `Redactor`**，并如实断言干净（P9「禁止展示明文用户信息」）。
   diff 里会带示例数据，源码不是「天然安全」的。
3. **不写「已修复」这种没有判据的词**。结论字段只放复评给出的取值
   （`accepted` / `partially_fixed` / `rejected_*` / `unverified` / 空）。
   空表示「没走到复评」——那也是一种结论，用它自己的字段说出来。
"""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ai_butler.core.filelock import FileLock, null_lock
from ai_butler.evaluation.redaction import Redactor

__all__ = ["DefectTrace", "RepairTrail", "RoundRecord", "TrailError"]


class TrailError(RuntimeError):
    """溯源账记不下去了（结构错误，不是「没找到」）。

    与「查不到这一轮」分开：查不到是数据还没来，记不下去是账要坏了。
    """


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


# --------------------------------------------------------------------------- #
# 一轮的完整记录
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RoundRecord:
    """闭环里的一轮。**字段顺序就是溯源顺序**，不是随意排的。"""

    round_id: str
    started_at: str = ""

    # ① 问题在哪
    defects: tuple[Mapping[str, Any], ...] = ()
    fingerprint: str = ""
    fingerprint_detail: Mapping[str, Any] = field(default_factory=dict)

    # ② 边界：本轮只许动什么、什么被锁住
    scope: Mapping[str, Any] = field(default_factory=dict)

    # ③ 改了什么
    patch: Mapping[str, Any] = field(default_factory=dict)

    # ④ 为什么这样改（模型给的理由；弃权时是它弃权的原话）
    rationale: str = ""

    # ⑤ 验了什么
    verification: tuple[str, ...] = ()

    # ⑥ 判了什么
    verdict: str = ""
    verdict_reason: str = ""
    resolved: tuple[str, ...] = ()
    new_defects: tuple[str, ...] = ()
    fingerprint_after: str = ""
    fingerprint_diff: Mapping[str, Any] = field(default_factory=dict)

    # 停在哪、交给谁
    stopped: str = ""
    handoff: str = ""

    @property
    def moved(self) -> bool:
        """本轮有没有让错误信息发生变化。**这是「继续下一轮」的唯一依据。**"""
        return bool((self.fingerprint_diff or {}).get("moved"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "started_at": self.started_at,
            "defects": [dict(d) for d in self.defects],
            "fingerprint": self.fingerprint,
            "fingerprint_detail": dict(self.fingerprint_detail),
            "scope": dict(self.scope),
            "patch": dict(self.patch),
            "rationale": self.rationale,
            "verification": list(self.verification),
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "resolved": list(self.resolved),
            "new_defects": list(self.new_defects),
            "fingerprint_after": self.fingerprint_after,
            "fingerprint_diff": dict(self.fingerprint_diff),
            "stopped": self.stopped,
            "handoff": self.handoff,
            "moved": self.moved,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RoundRecord":
        return cls(
            round_id=str(payload.get("round_id") or ""),
            started_at=str(payload.get("started_at") or ""),
            defects=tuple(dict(d) for d in _as_tuple(payload.get("defects"))
                          if isinstance(d, Mapping)),
            fingerprint=str(payload.get("fingerprint") or ""),
            fingerprint_detail=dict(payload.get("fingerprint_detail") or {}),
            scope=dict(payload.get("scope") or {}),
            patch=dict(payload.get("patch") or {}),
            rationale=str(payload.get("rationale") or ""),
            verification=tuple(str(v) for v in _as_tuple(payload.get("verification"))),
            verdict=str(payload.get("verdict") or ""),
            verdict_reason=str(payload.get("verdict_reason") or ""),
            resolved=tuple(str(v) for v in _as_tuple(payload.get("resolved"))),
            new_defects=tuple(str(v) for v in _as_tuple(payload.get("new_defects"))),
            fingerprint_after=str(payload.get("fingerprint_after") or ""),
            fingerprint_diff=dict(payload.get("fingerprint_diff") or {}),
            stopped=str(payload.get("stopped") or ""),
            handoff=str(payload.get("handoff") or ""))


# --------------------------------------------------------------------------- #
# 「某条缺陷在各轮的遭遇」—— 溯源的具体输出
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DefectTrace:
    """一条缺陷在某一轮里的状态。"""

    round_id: str
    severity: str
    evidence: str
    target_hint: str
    #: 这一轮它还在不在
    present: bool
    #: 这一轮它的证据变了吗（None 表示本轮首次出现，无从比较）
    changed: bool | None
    #: 这一轮改动了哪些文件、为什么
    patch_paths: tuple[str, ...]
    rationale: str
    verdict: str
    verdict_reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"round_id": self.round_id, "severity": self.severity,
                "evidence": self.evidence, "target_hint": self.target_hint,
                "present": self.present, "changed": self.changed,
                "patch_paths": list(self.patch_paths),
                "rationale": self.rationale, "verdict": self.verdict,
                "verdict_reason": self.verdict_reason}

    def render(self) -> str:
        state = ("仍在" if self.present else "已消失")
        change = {True: "证据已变", False: "证据未变",
                  None: "首次出现"}[self.changed]
        where = "、".join(self.patch_paths) or "（本轮未改代码）"
        out = [f"[{self.round_id}] {state}｜{change}｜改了 {where}",
               f"    问题：{self.evidence[:160]}"]
        if self.target_hint:
            out.append(f"    定位提示：{self.target_hint[:160]}")
        if self.rationale:
            out.append(f"    为什么这样改：{self.rationale[:240]}")
        if self.verdict:
            out.append(f"    复评：{self.verdict}｜{self.verdict_reason[:200]}")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# 账本
# --------------------------------------------------------------------------- #
@dataclass
class RepairTrail:
    """轮次账。**只追加**，可落 JSONL，可跨进程回读。

    `lock` 是写互斥（U86）。为 None 时按 `path` 自动造一把
    （`<path>.lock`），没有 `path` 就是空实现——只活在内存的账本
    没有第二个写者，加锁只是形式。
    """

    #: 落盘路径。不给就只留内存（测试与一次性运行）。
    path: str | Path | None = None
    redactor: Redactor = field(default_factory=Redactor)
    lock: AbstractContextManager[Any] | None = None
    _records: list[RoundRecord] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)

    # ---------------- 互斥 ---------------- #
    def _guard(self) -> AbstractContextManager[Any]:
        if self.lock is not None:
            return self.lock
        if self.path is not None:
            return FileLock(f"{self.path}.lock", purpose="溯源账")
        return null_lock()

    def _sync(self) -> None:
        """锁内**强制**从磁盘重读，以磁盘为准。

        与 `load()` 的区别是这里不看内存：`load()` 的「已有记录优先」
        是为「反复访问不要重复载入」设计的，用在写之前恰好是错的——
        另一个进程刚追加的那一轮就在文件里，而内存里没有。
        """
        if self.path is None or not self.path.exists():
            return
        loaded: list[RoundRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                loaded.append(RoundRecord.from_dict(json.loads(line)))
        self._records = loaded

    # ---------------- 写 ---------------- #
    def append(self, record: RoundRecord) -> RoundRecord:
        """记录一轮。**出口脱敏并断言干净**，然后才落盘。

        断言放在落盘**之前**：先落盘再断言，一旦断言失败，
        带明文的那一行已经在磁盘上了，删掉也只是「我们从没写过」的假象。

        **查重在锁内、且查之前先重读**（U86）。两个进程各自从磁盘读到
        同一份历史、各自认为本轮号可用、各自追加一行同号记录——字节没交错，
        账本里却多了个影子（`get()` 只见第一条）。不重读的查重只在本进程内有效。

        落盘在入内存**之前**：反过来的话写失败时内存里已有记录，
        下一次 `_sync()` 会把它抹掉，而调用方以为记下了。
        """
        # 一轮只能有一条记录。同一个 `round_id` 记两次的后果不是「多一行」：
        # `get()` 只返回第一条，第二条成了**文件里有、谁读都读不到**的影子账，
        # 而同名的两条内容往往不一样（一次改坏了、一次在撤销后重跑）——
        # 于是「问题出在哪一轮」正好在这一刻失去唯一答案。
        # 需要第二次运行时，轮次号必须续接（`RepairLoop._next_round_no`）。
        with self._guard():
            self._sync()
            if any(r.round_id == record.round_id for r in self._records):
                raise TrailError(
                    f"{record.round_id} 已有记录，不能重复登记：同一轮两次运行必须"
                    f"续接轮次号，否则溯源账里会有两条不同的 {record.round_id}")
            safe = self.redactor.redact_deep(record.as_dict())
            self.redactor.assert_deep_clean(safe, where=f"溯源账 {record.round_id}")
            clean = RoundRecord.from_dict(safe)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # fsync：账本是崩溃后唯一的「当时到底改了什么」（U87）。
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(clean.as_dict(), ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._records.append(clean)
            return clean

    def load(self) -> tuple[RoundRecord, ...]:
        """从落盘文件回读。**已有内存记录优先**（同一次运行里不重复载入）。"""
        if self._records or self.path is None or not self.path.exists():
            return self.rounds()
        loaded: list[RoundRecord] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                loaded.append(RoundRecord.from_dict(json.loads(line)))
        self._records = loaded
        return self.rounds()

    # ---------------- 读 ---------------- #
    def rounds(self) -> tuple[RoundRecord, ...]:
        return tuple(self._records)

    def get(self, round_id: str) -> RoundRecord | None:
        for record in self._records:
            if record.round_id == round_id:
                return record
        return None

    def last(self) -> RoundRecord | None:
        return self._records[-1] if self._records else None

    # ---------------- 溯源 ---------------- #
    def trace_defect(self, defect_id: str) -> tuple[DefectTrace, ...]:
        """一条缺陷在各轮的完整遭遇。**这就是「有迹可循」的答案。**

        输出按轮次顺序，每轮回答四件事：还在不在、证据变没变、
        这一轮改了哪里、为什么说好了/没好。
        """
        out: list[DefectTrace] = []
        for index, record in enumerate(self._records):
            present = next((d for d in record.defects
                            if str(d.get("defect_id") or "") == defect_id), None)
            changed: bool | None = None
            if index > 0:
                diff = record.fingerprint_diff or {}
                if defect_id in set(diff.get("resolved") or ()):
                    changed = True
                elif defect_id in {m.get("defect_id") for m in
                                   (diff.get("mutated") or ())}:
                    changed = True
                elif defect_id in set(diff.get("carried") or ()):
                    changed = False
            if present is None and changed is None:
                # 本轮既没出现、也不在变化列表里：说明这一轮它已经不存在，
                # 且不是本轮才消失的（更早消失）。跳过，避免噪声。
                continue
            out.append(DefectTrace(
                round_id=record.round_id,
                severity=str((present or {}).get("severity") or ""),
                evidence=str((present or {}).get("evidence") or ""),
                target_hint=str((present or {}).get("target_hint") or ""),
                present=present is not None,
                changed=changed,
                patch_paths=tuple(str(p) for p in
                                  _as_tuple((record.patch or {}).get("paths"))),
                rationale=record.rationale,
                verdict=record.verdict,
                verdict_reason=record.verdict_reason))
        return tuple(out)

    def explain(self, round_id: str) -> str:
        """一轮的人读说明。给「审批前先看懂这一轮干了什么」用。"""
        record = self.get(round_id)
        if record is None:
            return f"没有 {round_id} 的记录"
        patch = record.patch or {}
        lines = [f"=== {record.round_id}（{record.started_at}）===",
                 f"问题：{len(record.defects)} 条缺陷"]
        for defect in record.defects:
            lines.append(f"  - [{defect.get('severity')}] {defect.get('defect_id')}"
                         f" {defect.get('kind')}｜{str(defect.get('evidence') or '')[:160]}")
        if record.scope:
            allowed = record.scope.get("allowed_prefixes") or []
            locked = record.scope.get("locked_paths") or []
            lines.append(f"边界：允许改 {list(allowed)}"
                         + (f"；锁定（前轮已改，需先撤销）{list(locked)}" if locked else ""))
        if patch:
            lines.append(f"改动：{patch.get('applied')} "
                         f"{list(patch.get('paths') or [])}"
                         f"（{patch.get('touched', 0)} 行）")
            if patch.get("undo_of"):
                lines.append(f"本轮的基线上撤销了：{patch['undo_of']}")
        if record.rationale:
            lines.append(f"为什么这样改：{record.rationale[:400]}")
        if record.verification:
            lines.append("验证：" + "；".join(record.verification))
        lines.append(f"复评：{record.verdict or '（未复评）'}｜{record.verdict_reason[:300]}")
        if record.fingerprint_diff:
            lines.append(f"错误信息：{record.fingerprint_diff.get('summary', '')}")
        if record.handoff:
            lines.append(f"移交：{record.handoff}")
        return "\n".join(lines)
