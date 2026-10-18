"""P10 Harness 自进化闭环。

计划书依据
----------
三、P10 十项开发步骤 + 触发时机「7×24 小时后台离线自动运行，**不占用业务算力**；
**离线链路绝对不能访问原始未脱敏用户私有数据**」

**与业务链路完全隔离**（核心定位）：本 Agent 的输入只有脱敏后的采集数据，
输出只有变更提案与待审批单；它**不写业务库、不碰私有 Vault**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_READ
from ai_butler.agents.phase import PhaseAgent
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.context import ContextItem
from ai_butler.harness.hooks import HookChain
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
        """把外部（LLM 决策层）给出的提案转成内部结构。

        这里**不内置任何模型调用**：决策层是端口，由运行时注入。
        没有提案就等于没有决策，循环直接以「未收敛」收尾，而不是自己编一个。
        """
        out: list[Proposal] = []
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
            out.append(Proposal(
                summary=str(row.get("summary") or ""), change=change,
                commands=tuple(row.get("commands") or ()),
                done=bool(row.get("done"))))
        return out

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
            hooks=HookChain.default(qa_verdict=state.get("qa_verdict")),
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
