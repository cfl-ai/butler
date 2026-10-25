"""P10 Harness 自进化闭环。

计划书依据
----------
三、P10 十项开发步骤 + 触发时机「7×24 小时后台离线自动运行，**不占用业务算力**；
**离线链路绝对不能访问原始未脱敏用户私有数据**」

**与业务链路完全隔离**（核心定位）：本 Agent 的输入只有脱敏后的采集数据，
输出只有变更提案与待审批单；它**不写业务库、不碰私有 Vault**。
"""

from __future__ import annotations

# 直接运行本文件（python xxx.py / PyCharm 右键）时，sys.path[0] 是本文件所在目录
# 而非项目根，`import ai_butler` 会直接 ModuleNotFoundError。此处把项目根补进去；
# 作为包被 import 时 __package__ 非空，这段不执行。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    del _sys, _Path

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_READ
from ai_butler.agents.phase import PhaseAgent
from ai_butler.evaluation.defects import batch_from_dict
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.context import ContextItem
from ai_butler.harness.hooks import HookChain
from ai_butler.harness.intake import IntakeResult, RepairIntake, qa_verdict_from_batch
from ai_butler.harness.loop import AgenticLoop, LoopResult, Proposal
from ai_butler.harness.memory import ENTRY_PREFIX, MemoryEntry, from_qa_report
from ai_butler.harness.risk import Change
from ai_butler.harness.sandbox import DEFAULT_TIMEOUT, SandboxPort
from ai_butler.harness.skills import SkillRegistry, default_registry


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class HarnessAgent(PhaseAgent):
    """P10 阶段 Agent：跑四阶循环，产出版本变更单与待审批单。"""

    phase_code: ClassVar[str] = "P10"
    writes: ClassVar[tuple[str, ...]] = ("p10_harness",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_WIKI_READ, CAP_RETRIEVAL)

    sandbox: SandboxPort | None = None
    skills: SkillRegistry = field(default_factory=default_registry)
    #: 模型摘要器（端口注入；未注入则只做结构化裁剪，裁定 U36）
    summarizer: Any = None
    analyzer: Any = None
    max_iterations: int = 8
    no_progress_limit: int = 2

    #: 本轮缺陷受理结果（U76）。**不是状态缓存，是本轮的可审计产物**：
    #: 为什么某条缺陷没被受理，必须在这一轮里说清楚（拒收原因在 rejected 里）
    intake_result: IntakeResult | None = field(init=False, default=None)
    intake_error: str = field(init=False, default="")
    defect_batch: Any = field(init=False, default=None)

    def _context_items(self, state: Mapping[str, Any]) -> list[ContextItem]:
        out: list[ContextItem] = []
        for row in (state.get("harness_context") or []):
            row = row or {}
            out.append(ContextItem(
                item_id=str(row.get("item_id") or ""),
                text=str(row.get("text") or ""),
                source=str(row.get("source") or ""),
                relevance=float(row.get("relevance") or 0.0),
                occurred_at=str(row.get("occurred_at") or ""),
                scope=str(row.get("scope") or "public")))
        return out

    def _proposals(self, state: Mapping[str, Any]) -> list[Proposal]:
        """提案来源有二，**优先级明确**：

        1. `harness_proposals`（显式给定）——外部决策层直接下发的提案，最高优先。
        2. `eval_platform["defects"]`（U76）——评测中台产出的缺陷单，
           经 `RepairIntake` 受理成提案。**只有在没有显式提案时才用**：
           显式提案代表有人/有模型已经做了判断，缺陷单只是「发现」，
           拿「发现」去覆盖「判断」等于把决定权从决策层夺走。
        """
        explicit: list[Proposal] = []
        for row in (state.get("harness_proposals") or []):
            row = row or {}
            change = None
            raw_change = row.get("change")
            if isinstance(raw_change, Mapping):
                change = Change(
                    change_id=str(raw_change.get("change_id") or ""),
                    description=str(raw_change.get("description") or ""),
                    files=tuple(raw_change.get("files") or ()),
                    diff=str(raw_change.get("diff") or ""),
                    wiki_scopes=tuple(raw_change.get("wiki_scopes") or ()),
                    redacted=bool(raw_change.get("redacted", True)),
                    modifies_jev_template=bool(
                        raw_change.get("modifies_jev_template")),
                    wiki_pages_written=int(raw_change.get("wiki_pages_written") or 0))
            explicit.append(Proposal(
                summary=str(row.get("summary") or ""), change=change,
                commands=tuple(row.get("commands") or ()),
                done=bool(row.get("done"))))
        if explicit:
            return explicit

        # 兜底：从评测缺陷单生成提案（不编提案，没有缺陷就没有提案）
        self.intake_result = self._intake_from_defects(state)
        return list(self.intake_result.proposals()) if self.intake_result else []

    def _intake_from_defects(self, state: Mapping[str, Any]) -> IntakeResult | None:
        """把 `eval_platform["defects"]` 受理成工单。**无缺陷单就返回 None**。

        全过（或根本没评）时返回 None，循环随后以「没有新提案」收尾并如实标注
        转人工——不因为「没缺陷」就报「已达标」。
        """
        raw = (state.get("eval_platform") or {}).get("defects")
        if not isinstance(raw, Mapping) or not raw.get("defects"):
            return None
        try:
            batch = batch_from_dict(raw)
        except Exception as error:                       # noqa: BLE001
            # 缺陷单形态不对：不猜、不跳过，让整轮以失败收尾并说明原因
            self.intake_error = f"{type(error).__name__}: {error}"
            return None
        self.defect_batch = batch
        return RepairIntake(max_tickets=int(state.get("max_tickets") or 8)).intake(batch)

    def _qa_verdict(self, state: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """质检结论：显式 `qa_verdict` 优先，否则由缺陷单推导（三、P10 步骤3）。

        没有这一步，`HookChain` 的 `qa_link` 会因为「未收到质检结论」
        把每一张缺陷工单都判成 needs_approval，闭环第一步就走不动。
        评测中台本身就是质检方，它的缺陷单即结论，不另算一份。
        """
        explicit = state.get("qa_verdict")
        if isinstance(explicit, Mapping):
            return explicit
        if self.defect_batch is not None:
            return qa_verdict_from_batch(self.defect_batch)
        return None

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        now = _now()
        redactor = Redactor(analyzer=self.analyzer)

        if self.sandbox is None:
            return {"p10_harness": {
                "user_id": uid, "phase": self.phase_code, "ran": False,
                "reason": "未接入沙箱（裁定 U34：沙箱是 AI Infra 的能力，由运行时注入）"}}

        proposals = self._proposals(state)
        pending = list(proposals)

        async def _decide(_context, _history) -> Proposal:
            if pending:
                return pending.pop(0)
            # **提案用尽 ≠ 达标**：早先这里写 done=True，导致循环在什么都没做的情况下
            # 报告「已达标，循环收敛」——虚假成功。改为 done=False，
            # 由无进展规则收尾并如实标注「转人工」。
            return Proposal(summary="决策层未给出新的提案（提案已用尽）", done=False)

        loop = AgenticLoop(
            decide=_decide,
            # 质检结论在上一步 `_proposals` 里已确定（缺陷单即结论），
            # 必须**在这里**取，不能等循环里再取——Hook 是在执行前判的
            hooks=HookChain.default(qa_verdict=self._qa_verdict(state)),
            sandbox=self.sandbox,
            max_iterations=self.max_iterations,
            no_progress_limit=self.no_progress_limit,
            timeout=int(state.get("sandbox_timeout") or DEFAULT_TIMEOUT))

        result: LoopResult = await loop.run(
            goal=str(state.get("harness_goal") or "缺陷修复与迭代"),
            items=self._context_items(state), redactor=redactor,
            summarizer=self.summarizer,
            max_items=int(state.get("max_context_items") or 20))

        # ---- 达标后沉淀记忆（计划书步骤9）----
        entries: list[MemoryEntry] = list(result.memory_entries)
        if result.converged:
            for entry in from_qa_report(
                    report=dict(state.get("p9_report") or {}),
                    recorded_at=now.isoformat()):
                entries.append(entry)

        return {"p10_harness": {
            "user_id": uid, "phase": self.phase_code, "ran": True,
            "goal": str(state.get("harness_goal") or ""),
            "result": result.as_dict(),
            "converged": result.converged,
            "requires_approval": result.pending_approval is not None,
            # ---- 缺陷受理（U76）：从评测中台拿问题 → 受理成工单 ----
            "intake": (self.intake_result.as_dict(redactor)
                       if self.intake_result is not None else None),
            "intake_error": self.intake_error,
            # **未实现项如实登记**：本轮只到「变更单」，没有补丁应用器。
            # 声称能自动改源码而实际没有，比不做更坏（声明≠能力）。
            "patch_not_applied": {
                "applied": False,
                "reason": "本轮未实现补丁应用器：缺陷单的 diff 为空并标注 "
                          "diff_pending，落地需人工或 LLM 决策层注入补丁后重跑",
            },
            # ---- 计划书步骤10：评测与版本管理 ----
            "change_note": {
                "generated_at": now.isoformat(),
                "iterations": result.iterations,
                "converged": result.converged,
                "memory_keys": [e.key for e in entries],
            },
            "memory_entries": [e.render(redactor) for e in entries],
            "skills": list(self.skills.names()),
            "offline_isolation": {
                "touches_business_db": False,
                "touches_private_vault": False,
                "note": "离线链路严禁访问未脱敏私有 Vault（三、P10 触发时机）",
            },
        }}

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
