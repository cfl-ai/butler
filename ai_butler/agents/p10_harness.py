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

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_READ
from ai_butler.agents.phase import PhaseAgent
from ai_butler.evaluation.defects import batch_from_dict
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.coder import CodeProposal, SnippetReader
from ai_butler.harness.context import ContextItem
from ai_butler.harness.hooks import HookChain
from ai_butler.harness.intake import (
    IntakeResult,
    RepairIntake,
    files_from_hint,
    qa_verdict_from_batch,
)
from ai_butler.harness.loop import (
    STAGE_CORRECT,
    AgenticLoop,
    LoopResult,
    Proposal,
)
from ai_butler.harness.memory import MemoryEntry, from_qa_report
from ai_butler.harness.patch import ALLOWED_SCOPE_BY_KIND, PatchApplier, PatchGuard
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

    # ---- U78：代码创作与补丁落盘 ----
    #: 代码创作端口（`harness.coder.CodeWriter`）。**未注入就不改代码**：
    #: 只跑验证命令并如实标注「需要补丁」，不假装修过。
    coder: Any = None
    #: 补丁落盘的工作根。缺省时取 `state["patch_root"]`。
    #: 与 `sandbox` 的 root 应当一致，否则「改的地方」和「测的地方」不是一个目录。
    patch_root: str | None = None
    #: 补丁应用器（可注入替身用于测试）；缺省按 `patch_root` 现建
    applier: Any = None
    #: 一轮里最多向模型要几次补丁（实现参数）。缺陷多时若每张工单都要一次，
    #: 成本与归因都不受控：「改坏了」到底是哪一张的功劳，说不清。
    #: 未拿到补丁的工单保持原样，下轮继续（不是丢弃）。
    max_coder_calls: int = 2

    #: 本轮缺陷受理结果（U76）。**不是状态缓存，是本轮的可审计产物**：
    #: 为什么某条缺陷没被受理，必须在这一轮里说清楚（拒收原因在 rejected 里）
    intake_result: IntakeResult | None = field(init=False, default=None)
    intake_error: str = field(init=False, default="")
    defect_batch: Any = field(init=False, default=None)

    #: 本轮代码创作结果（U78）。失败也要留痕：为什么没产出补丁，
    #: 与「产出了但被拒绝」是不同的两件事。
    code_results: list[CodeProposal] = field(init=False, default_factory=list)
    #: 最近一次尝试的 diff（无论是否应用成功）——复评要审计它
    attempted_diff: str = field(init=False, default="")

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

    # ------------------------------------------------------------------ #
    # U81：跨轮边界
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scope(state: Mapping[str, Any]) -> dict[str, Any]:
        """本轮边界（由闭环驱动 `agents/repair_loop.py` 计算并放进 State）。

        缺省为空 = 没有跨轮约束（首轮，或调用方没接闭环驱动）。
        **这里的默认值不承担安全责任**：真正的越界防线是 `PatchGuard`
        与 `audit_change`，它们不依赖 State 里有没有这个键。
        """
        raw = state.get("patch_scope")
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _locked_paths(self, state: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(str(p) for p in (self._scope(state).get("locked_paths") or ()))

    # ------------------------------------------------------------------ #
    # U78：向大模型要补丁
    # ------------------------------------------------------------------ #
    def _patch_root(self, state: Mapping[str, Any]) -> str:
        root = self.patch_root or state.get("patch_root") or ""
        return str(root) if root else ""

    def _ticket_for(self, change: Change) -> Any:
        """按 `change_id` 找回工单。变更 id 的构造式在 `intake._change_for` 里。"""
        if self.intake_result is None:
            return None
        for ticket in self.intake_result.tickets:
            if ticket.proposal.change is not None and \
                    ticket.proposal.change.change_id == change.change_id:
                return ticket
        return None

    def _defect_for(self, change: Change) -> Any:
        ticket = self._ticket_for(change)
        return ticket.defect if ticket is not None else None

    @staticmethod
    def _candidate_files(change: Change, defect: Any) -> tuple[str, ...]:
        files = tuple(change.files) or files_from_hint(defect.target_hint)
        if files:
            return files
        # 兜底：用该类别的范围前缀里**看起来像文件**的项。
        # 一个文件都给不出来时宁可不产补丁——没有源码上下文，
        # 模型只能凭空编，编出来的 diff 必然对不上。
        return tuple(p for p in ALLOWED_SCOPE_BY_KIND.get(
            defect.kind.value, ()) if p.endswith(".py"))

    @staticmethod
    def _keywords(defect: Any) -> tuple[str, ...]:
        """从缺陷证据里挑标识符，用于在大文件里定位片段。

        只取形如 `lower_snake_case` / `CamelCase` 的英文标识符，
        中文词太泛（「检索」「阈值」几乎每行都有），定位反而失准。
        """
        import re
        text = f"{defect.evidence} {defect.target_hint}"
        found = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text)
        seen: list[str] = []
        for token in found:
            if token.lower() in {"with", "from", "that", "this", "none", "true",
                                 "false", "return", "pytest", "http"}:
                continue
            if token not in seen:
                seen.append(token)
        return tuple(seen[:12])

    async def _prepare_patches(self, proposals: Sequence[Proposal],
                               state: Mapping[str, Any]) -> list[Proposal]:
        """给缺 diff 的提案向大模型要补丁。**不改提案顺序与优先级。**

        产出的是**候选**补丁：范围是否越界由 `PatchGuard` 判，
        能不能应用由 `PatchApplier` 判，修没修好由复评判。
        本方法一个都不代判。
        """
        if self.coder is None:
            return list(proposals)
        root = self._patch_root(state)
        if not root:
            return list(proposals)

        reader = SnippetReader(Path(root))
        out: list[Proposal] = []
        budget = int(state.get("max_coder_calls") or self.max_coder_calls)
        locked = self._locked_paths(state)

        for proposal in proposals:
            change = proposal.change
            if change is None or change.diff.strip():
                out.append(proposal)
                continue
            defect = self._defect_for(change)
            if defect is None:
                # 不是从缺陷单来的提案（来源无法定位），不替它编补丁
                out.append(proposal)
                continue

            # ---- 跨轮边界（U81）：被前几轮改过的文件不进上下文 ----
            # 剔除而不是「给了再拦」：模型看不到那些文件，就不会去改。
            # 「给了再靠校验拦住」多烧一次调用，还会让溯源里出现
            # 「它本来想改 X 但被拒了」这种与执行无关的噪声。
            candidates = self._candidate_files(change, defect)
            if locked:
                kept = tuple(p for p in candidates if p not in locked)
                if not kept:
                    self.code_results.append(CodeProposal(
                        False, skipped=True,
                        rationale=(f"待改文件 {list(candidates)} 都已被前几轮提交的补丁"
                                   f"改动过；在未撤销上一轮的前提下不再改动，"
                                   f"以免两份自动补丁叠在同一处、责任无法分割")))
                    out.append(proposal)
                    continue
                candidates = kept

            if budget <= 0:
                # 预算用完：**保持原样**，让它在下一轮继续，不是丢弃
                out.append(proposal)
                continue
            budget -= 1

            ticket = self._ticket_for(change)
            verify_target = (ticket.verify_target if ticket is not None
                             else "pytest -q")
            snippets = reader.read(candidates,
                                   kind=defect.kind.value,
                                   keywords=self._keywords(defect))
            try:
                code = await self.coder.propose(
                    defect=defect, snippets=snippets,
                    verify_target=verify_target)
            except Exception as exc:                     # noqa: BLE001
                # 端口实现自己出异常：如实记下，不让整轮崩在一处
                self.code_results.append(CodeProposal(
                    False, error=f"{type(exc).__name__}: {exc}"))
                out.append(proposal)
                continue

            self.code_results.append(code)
            if code.ok and locked and code.patches is not None:
                # 边界约束的最后一道：候选里剔掉了，模型仍可能凭记忆写出别的文件。
                # 用 `PatchGuard.frozen_hits` 而不是在这里手比路径——
                # 「哪些路径算锁」必须只有一处定义，否则两份判据迟早分叉。
                hit = PatchGuard.for_kind(
                    defect.kind.value, frozen_paths=locked).frozen_hits(code.patches)
                if hit:
                    self.code_results[-1] = CodeProposal(
                        False, diff=code.diff, rationale=code.rationale,
                        attempts=code.attempts,
                        rejected=code.rejected + (
                            f"补丁触碰已被前几轮改动过的文件 {list(hit)}："
                            f"同一文件叠两份自动补丁会让责任无法分割",),
                        error=f"越过跨轮边界：{list(hit)}")
                    out.append(proposal)
                    continue
            if code.ok:
                # 只有**会被应用**的补丁才留作复评的审计对象。
                # 被范围校验拒掉或模型主动弃权的那些，不构成「改动」，
                # 拿去审计只会让复评报一条重复的越界发现。
                self.attempted_diff = code.diff
            if not code.ok:
                # 没产出合规补丁：保留原提案（无 diff），循环会如实验证并转人工
                out.append(proposal)
                continue

            out.append(replace(
                proposal,
                change=replace(
                    change, diff=code.diff, files=code.paths,
                    extra={**dict(change.extra), "diff_pending": False,
                           "coder_attempts": code.attempts,
                           "coder_rationale": code.rationale}),
                summary=(f"{proposal.summary}｜代码创作层已产出补丁："
                         f"{'、'.join(code.paths)}（{code.patches.touched} 行）"
                         if code.patches else proposal.summary)))
        return out

    @staticmethod
    def _verified_patch(history: Sequence[Any]) -> bool:
        """上一轮是否「应用了补丁且验证全绿」。

        **只看最近的 STAGE_CORRECT**：更早的轮次状态已被后续轮次覆盖，
        拿历史里任意一次成功来确认达标，会出现「第二轮改坏了，
        但第一轮成功过所以判定达标」。
        """
        for record in reversed(list(history)):
            if record.stage != STAGE_CORRECT:
                continue
            meta = record.meta or {}
            return bool(meta.get("verified") and meta.get("patch_applied"))
        return False

    def _no_patch_reason(self, result: LoopResult) -> str:
        """`applied=False` 时**必须说清为什么**。四种原因，处置完全不同。"""
        if self.coder is None:
            return ("未注入代码创作端口（coder）：本轮只跑验证命令，不改代码。"
                    "落地需注入 CodeWriter（见 harness/coder.py）后重跑")
        if not self.attempted_diff:
            failures = [c for c in self.code_results if not c.ok]
            if failures:
                last = failures[-1]
                if last.skipped:
                    # 弃权是**正确的**处置，理由要原文带出来：
                    # 「模型说上下文不够」和「模型写不出来」要能区分。
                    return f"模型主动判断不该改代码（未臆造补丁）：{last.rationale}"
                detail = last.error or "；".join(last.rejected[-2:])
                return f"代码创作层未产出合规补丁：{detail}"
            return "代码创作层未产出补丁（无可改的提案，或缺源码上下文）"
        for outcome in reversed(result.patch_results):
            if not getattr(outcome, "ok", False):
                return f"补丁未能应用：{outcome.reason[:300]}"
        return "补丁未保留：验证未通过已回滚，或被 Hook 拦截"

    def _committed_patch(self, result: LoopResult) -> dict[str, Any] | None:
        """本轮**成功提交**的那份补丁的撤销凭据（U81）。

        只给最后一份成功的。一个 run 里 `_verified_patch` 会在成功后收敛，
        实际最多提交一次；给「全部」会让撤销出现「撤哪一份」的歧义，
        而歧义在自动化里等于不可执行。

        `before` 是源码正文，**刻意不进 `PatchResult.as_dict()`**：
        它只供记账与撤销，不该顺着 State 到处流。这里单独给一个口子，
        是因为撤销发生在很久之后（`PatchApplier` 的备份早已清空）。
        """
        for outcome in reversed(result.patch_results):
            if not getattr(outcome, "ok", False):
                continue
            before = getattr(outcome, "before", None)
            if not before:
                continue
            return {
                "paths": list(outcome.paths),
                "before": dict(before),
                "touched": outcome.touched,
                "diff": self.attempted_diff,
                "rationale": (self.code_results[-1].rationale
                              if self.code_results else ""),
                # 本轮**受理**的缺陷（通常只有一条即精确对应；多条时如实标注为
                # 「本轮受理集合」，不冒充「这份补丁专门修的那条」）
                "defect_ids": self._intake_defect_ids(),
            }
        return None

    def _intake_defect_ids(self) -> list[str]:
        if self.intake_result is None:
            return []
        ids: list[str] = []
        for ticket in self.intake_result.tickets:
            if ticket.defect.defect_id not in ids:
                ids.append(ticket.defect.defect_id)
        return ids

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        now = _now()
        redactor = Redactor(analyzer=self.analyzer)

        # ---- 清掉上一轮的残留（U81）----
        # 跨轮重试会**复用同一个 Agent 实例**，而这些字段是「本轮的产物」。
        # 不清的后果不是崩，是**说错话**：
        #   - `code_results` 攒着第一轮的记录 → `_no_patch_reason` 读到第一轮的
        #     成功补丁，说出「补丁未保留：验证未通过已回滚」这种与事实相反的话；
        #   - `attempted_diff` 还是第一轮的 diff → 送进复评审计的是**上一轮**
        #     的改动，而这一轮可能一个字都没改。
        # 单轮跑一万次都不会暴露，多轮第一次就中（E-79 那类串味）。
        self.code_results = []
        self.attempted_diff = ""
        self.intake_result = None
        self.intake_error = ""
        self.defect_batch = None

        if self.sandbox is None:
            return {"p10_harness": {
                "user_id": uid, "phase": self.phase_code, "ran": False,
                "reason": "未接入沙箱（裁定 U34：沙箱是 AI Infra 的能力，由运行时注入）"}}

        proposals = self._proposals(state)
        # U78：向大模型要补丁。**在 Hook 之前**——补丁是待审对象，
        # 先应用再判风险等于让高危变更先落地（loop 里顺序同理）。
        proposals = await self._prepare_patches(proposals, state)
        pending = list(proposals)

        async def _decide(_context, _history) -> Proposal:
            # 「上一轮补丁已应用且验证全绿」才由决策层确认达标。
            # 注意这不等于「缺陷修好了」：沙箱测试通过只证明没弄坏现有测试，
            # 真正的结论要等评测中台复评（`evaluation/recheck.py`）。
            if self._verified_patch(_history):
                return Proposal(
                    summary=("上一轮补丁已应用且验证命令全绿，确认本轮变更"
                             "（是否真修好由评测中台复评判定，本循环不代判）"),
                    done=True)
            if pending:
                return pending.pop(0)
            # **提案用尽 ≠ 达标**：早先这里写 done=True，导致循环在什么都没做的情况下
            # 报告「已达标，循环收敛」——虚假成功。改为 done=False，
            # 由无进展规则收尾并如实标注「转人工」。
            return Proposal(summary="决策层未给出新的提案（提案已用尽）", done=False)

        root = self._patch_root(state)
        applier = self.applier
        if applier is None and root:
            applier = PatchApplier(root=Path(root))

        loop = AgenticLoop(
            decide=_decide,
            # 质检结论在上一步 `_proposals` 里已确定（缺陷单即结论），
            # 必须**在这里**取，不能等循环里再取——Hook 是在执行前判的
            hooks=HookChain.default(qa_verdict=self._qa_verdict(state)),
            sandbox=self.sandbox,
            # 无 coder 或无 root 时 patcher 为 None，循环退化为「只跑命令」
            patcher=applier,
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

        # 「本轮真的改动了代码吗」只算一次，供下面两个字段共用
        patch_applied = any(bool(getattr(r, "ok", False))
                            for r in result.patch_results)

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
            # ---- 补丁（U78）：本轮到底改了什么、验没验、保没保留 ----
            # 这是评审要读的那一段：`applied=False` 时下面的 `reason` 必须能解释
            # 为什么没改（没 coder / 模型没产出 / 补丁越界 / 应用失败）。
            "patch": {
                "applied": patch_applied,
                "root": root,
                "results": [r.as_dict() for r in result.patch_results],
                # 复评要用它做改动审计。**即使是应用失败的 diff 也要给**：
                # 审计要看的是「它想改什么」，不是「它改成了什么」。
                "diff": self.attempted_diff,
                "code": [c.as_dict(redactor=redactor) for c in self.code_results],
                "coder_wired": self.coder is not None,
                # 本轮边界（U81）：驱动算的规矩，落在这里供溯源与审计对照
                "scope": self._scope(state),
                # 撤销凭据（U81）：只有真正 commit 过的补丁才有
                "committed": self._committed_patch(result),
            },
            "patch_not_applied": {
                "applied": patch_applied,
                "reason": "" if patch_applied else self._no_patch_reason(result),
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
