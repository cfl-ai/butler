"""缺陷受理 —— 把评测缺陷单变成 harness 能跑的修复工单。

定位
----
评测中台**判**，harness **改**。本模块是两者之间唯一的一道门：
上游是 `evaluation.defects.DefectBatch`，下游是 `harness.loop.AgenticLoop`
需要的 `Proposal`。没有这道门，`HarnessAgent` 的 `harness_proposals`
只能靠外部 JSON 喂——评测说「不行」，harness 不知道该做什么。

四道闸（缺一不可）
------------------
1. **只收 `AUTO` 类**。`MANUAL` 直接拒收并写明理由（安全缺陷、无痕迹、
   评测输入不全）。拒收不是失败，是正确处置——把不可自动修的缺陷塞进
   自动流水线，等于让流水线去改判据。
2. **安全缺陷存在时整批不受理**。在会越权泄露余额的系统上优化检索召回，
   是把力气花在错的地方（`DefectBatch.auto_repairable()` 已按此返回空）。
3. **命令只从登记表里取**，不接受调用方拼串。沙箱白名单是第一道，
   这里是第零道：连「允许的命令」都不给拼接的机会。
4. **工单绝不自行宣布修好**。`Proposal.done` 恒为 False——
   本模块只负责「把缺陷变成一次可执行的验证」，修没修好由
   沙箱结论与后续重评决定。早先在 loop 里把「提案用尽」写成 done=True，
   结果什么都没做却报「已达标」（E-80 同源的虚假成功），不重犯。

为什么验证命令是「跑子系统的现有测试」
--------------------------------------
工单要求的不是「证明修好了」，而是「先确认现有测试是绿的」：
- **绿** = 该子系统的测试没覆盖这个缺陷 → 必须补回归用例，测试覆盖有缺口；
- **红** = 已有用例本来就失败 → 直接拿到了定位点，不必再猜。
这两种结论都真实可行动，且不假装「跑了 pytest 就等于复现了线上缺陷」。
真正的线上复现要靠重放痕迹，那是 P9 数据源的事，不在本模块伪装。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ai_butler.evaluation.defects import (
    Defect,
    DefectBatch,
    DefectKind,
    DefectSeverity,
    Disposition,
)
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.loop import Proposal
from ai_butler.harness.risk import Change
from ai_butler.harness.sandbox import ALLOWED_COMMANDS

#: 缺陷类别 → 该类别所在子系统的回归用例文件。
#:
#: **未登记一律不受理**：允许调用方传命令进来，就等于允许任意命令进沙箱，
#: 沙箱白名单管得住「程序名」，管不住参数。这张表是唯一入口。
#:
#: 多个文件用空格分隔（一条 `pytest -q a b` 命令）。早先只登记一个文件，
#: 结果 `recall_gap` 指向评测平台的测试，**根本不覆盖检索侧的改动**——
#: 补丁改坏了 `planner.py` 而验证照样全绿。多登记一个的成本很低，
#: 漏验的代价是「绿着绿灯把功能改坏」。
_VERIFY_TARGETS: Mapping[DefectKind, str] = {
    DefectKind.RECALL_GAP: ("tests/test_retrieval_planner.py "
                            "tests/test_evaluation_platform.py"),
    DefectKind.MISSING_EVIDENCE: ("tests/test_retrieval_planner.py "
                                  "tests/test_evaluation_platform.py"),
    DefectKind.QUALITY_GAP: "tests/test_evaluation_platform.py",
    DefectKind.FALSE_TOOL_CALL: "tests/test_retrieval_planner.py",
    DefectKind.UNSTABLE: "tests/test_harness_p10.py",
    DefectKind.COST_REGRESSION: "tests/test_harness_evaluation.py",
}

#: 单批最多受理的工单数（实现参数）。缺陷多时按严重度取前 N 条，
#: **不是丢弃**——未受理的进 `deferred` 并如实计数，下轮继续。
DEFAULT_MAX_TICKETS = 8

#: 工单里对「收敛提示」的标注。**必须写明这是提示不是根因**，
#: 否则下游会把 target_hint 当成已定位结论去改。
HINT_DISCLAIMER = "以下为收敛提示（非根因断言），请自行判断"


class IntakeError(ValueError):
    """缺陷受理的输入不合法。"""


@dataclass(frozen=True)
class RepairTicket:
    """一张修复工单：缺陷 + 可执行提案 + 验证目标。"""

    ticket_id: str
    defect: Defect
    proposal: Proposal
    verify_target: str
    #: 是否需要人工补丁才能继续（本轮不实现补丁应用器，如实标注）
    requires_human_patch: bool = True

    @property
    def priority(self) -> str:
        return self.defect.priority

    def as_dict(self, redactor: Redactor | None = None) -> dict[str, Any]:
        red = redactor or Redactor()
        change = self.proposal.change
        payload = {
            "ticket_id": self.ticket_id,
            "priority": self.priority,
            "defect": self.defect.as_dict(red),
            "summary": self.proposal.summary,
            "commands": list(self.proposal.commands),
            "verify_target": self.verify_target,
            "requires_human_patch": self.requires_human_patch,
            "change": ({"change_id": change.change_id,
                        "files": list(change.files),
                        "description": change.description,
                        # 空 diff 必须显式说明，否则下游会以为「没改动=低风险可放行」
                        "diff_pending": bool(change.extra.get("diff_pending"))}
                       if change else None),
        }
        safe = red.redact_deep(payload)
        red.assert_deep_clean(safe, where=f"修复工单 {self.ticket_id}")
        return safe


@dataclass(frozen=True)
class Rejected:
    """未受理的缺陷 + 理由。**拒收必须说话**，静默丢弃等于假装没有缺陷。"""

    defect_id: str
    kind: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"defect_id": self.defect_id, "kind": self.kind, "reason": self.reason}


@dataclass
class IntakeResult:
    tickets: tuple[RepairTicket, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    #: 超出单批上限而未受理的缺陷（下轮继续，不是丢弃）
    deferred: tuple[str, ...] = ()

    @property
    def accepted(self) -> int:
        return len(self.tickets)

    def proposals(self) -> tuple[Proposal, ...]:
        """给 `HarnessAgent` 的提案序列（顺序即优先级）。"""
        return tuple(t.proposal for t in self.tickets)

    def as_dict(self, redactor: Redactor | None = None) -> dict[str, Any]:
        red = redactor or Redactor()
        return {
            "accepted": self.accepted,
            "rejected": len(self.rejected),
            "deferred": len(self.deferred),
            "tickets": [t.as_dict(red) for t in self.tickets],
            "rejected_detail": [r.as_dict() for r in self.rejected],
            "deferred_ids": list(self.deferred),
        }


#: 从自然语言里挑形如 `a/b/c.py` 的片段。**不能用 split**：
#: `target_hint` 写的是「`chat.py`、`planner.py`（候选数 top_k…）」这种句子，
#: 按空白切开后第二个路径的尾巴粘着中文括号，`endswith(".py")` 判不过，
#: 于是**静默漏掉一个文件**——而下游（候选文件、跨轮锁定）全靠这份列表。
#: 正则只认「看起来就是路径」的片段，宁可少认，不可认错。
_PATH_IN_TEXT = re.compile(r"[A-Za-z0-9_./\\-]+\.py(?![A-Za-z0-9_])")


def files_from_hint(hint: str) -> tuple[str, ...]:
    """从收敛提示里挑出**真正的文件路径**。

    `target_hint` 是人读的一句话（「retrieval/planner.py 的 CATEGORY_TOOLS 与……」
    或「core/permissions.py、agents/（越权路径的调用点）」）。直接塞进
    `Change.files` 会把描述文字当文件名——`risk.assess` 会拿它数文件个数、
    下游会拿它当改动范围，一个中文字符串混在路径列表里迟早出事。
    这里只保留形如 `xxx/yyy.py` 的片段，认不出就返回空（空比错好）。
    """
    text = str(hint or "").replace("\\", "/")
    out: list[str] = []
    for token in _PATH_IN_TEXT.findall(text):
        token = token.lstrip("./")
        if "/" in token and token not in out:
            out.append(token)
    return tuple(out)


def _change_for(defect: Defect) -> Change:
    """把缺陷转成一条**变更意图**。

    `diff` 刻意留空：本模块不能凭空生成补丁（谁生成的补丁谁负责），
    并由 `extra["diff_pending"]` 显式标注。留空的后果由受理口径兜——
    工单的 `requires_human_patch` 恒为 True，不把「没有补丁」当成「没有风险」。
    """
    return Change(
        change_id=f"fix:{defect.defect_id}",
        description=(f"修复 {defect.title}。{HINT_DISCLAIMER}：{defect.target_hint}；"
                     f"复现要点：{defect.repro}"),
        files=files_from_hint(defect.target_hint),
        diff="",
        redacted=True,
        modifies_jev_template=False,
        extra={"diff_pending": True, "defect_id": defect.defect_id,
               "dimension": defect.dimension, "kind": defect.kind.value,
               "target_hint": defect.target_hint})


class RepairIntake:
    """缺陷受理器。"""

    def __init__(self, *, max_tickets: int = DEFAULT_MAX_TICKETS) -> None:
        if max_tickets < 1:
            raise IntakeError("max_tickets 至少为 1")
        self.max_tickets = max_tickets

    # ------------------------------------------------------------------ #
    def intake(self, batch: DefectBatch) -> IntakeResult:
        tickets: list[RepairTicket] = []
        rejected: list[Rejected] = []
        deferred: list[str] = []

        blocked = batch.blocked_by_security
        for defect in batch.defects:
            if blocked and defect.kind is not DefectKind.SECURITY_VIOLATION:
                rejected.append(Rejected(
                    defect.defect_id, defect.kind.value,
                    "整批存在未解决的安全缺陷（P0），其余缺陷暂不受理："
                    "在安全不达标的前提下自动改代码，产出没有可信度"))
                continue

            if defect.disposition is Disposition.MANUAL:
                rejected.append(Rejected(
                    defect.defect_id, defect.kind.value,
                    defect.blocked_reason or "该缺陷不适用自动处置"))
                continue

            target = _VERIFY_TARGETS.get(defect.kind)
            if target is None:
                # 审批类或未登记验证目标的类别：不猜命令
                rejected.append(Rejected(
                    defect.defect_id, defect.kind.value,
                    f"缺陷类别 {defect.kind.value} 未登记验证目标，"
                    "不受理（不给拼接命令的机会）"))
                continue

            if len(tickets) >= self.max_tickets:
                deferred.append(defect.defect_id)
                continue

            tickets.append(self._accept(defect, target))

        return IntakeResult(tickets=tuple(tickets), rejected=tuple(rejected),
                            deferred=tuple(deferred))

    # ------------------------------------------------------------------ #
    def _accept(self, defect: Defect, target: str) -> RepairTicket:
        command = f"pytest -q {target}"
        program = command.split()[0]
        if program not in ALLOWED_COMMANDS:
            # 双保险：映射表写错了也不让它进沙箱
            raise IntakeError(
                f"验证命令 {program!r} 不在沙箱白名单 {list(ALLOWED_COMMANDS)} 内")

        proposal = Proposal(
            summary=(f"[{defect.priority}] {defect.title}｜先跑 {target} 确认覆盖缺口"
                     f"（绿=测试没覆盖该缺陷，需补用例；红=已有用例失败，直接定位）"),
            change=_change_for(defect),
            commands=(command,),
            # **恒为 False**：受理 ≠ 修好。谁把这里改成 True，
            # 谁就是在制造「什么都没做却报已达标」的假成功。
            done=False)

        return RepairTicket(
            ticket_id=f"T-{defect.defect_id}",
            defect=defect,
            proposal=proposal,
            verify_target=target,
            requires_human_patch=True)


def qa_verdict_from_batch(batch: DefectBatch) -> dict[str, Any]:
    """把缺陷单转成「质检结论」，供 `HookChain` 联动（三、P10 步骤3）。

    为什么由评测中台出这个结论：计划书要求 Hook「联动质检 Agent 完成风险
    分级校验」，而缺陷单本身就是质检结论——再让另一个组件重新算一遍，
    只会得到两份可能不一致的结论（E-44 同一教训）。
    """
    if batch.blocked_by_security:
        alerts = [d.evidence for d in batch.defects
                  if d.kind is DefectKind.SECURITY_VIOLATION]
        return {"action": "block", "alerts": alerts}
    return {"action": "pass", "alerts": []}
