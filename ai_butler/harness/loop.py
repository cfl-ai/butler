"""P10 Agentic Loop 主循环（裁定 U08 + U32）。

计划书依据
----------
- 三、P10 核心架构「采用标准 Agentic Loop **四阶循环**：
  **上下文收集 → LLM 决策 → 工具执行 → 结果自纠错**」
- 三、P10 步骤2「实现完整 Claude Code 风格 Agentic Loop 主循环」
- 三、P10 离线链路「代码迭代 Agent 分析问题、生成优化方案 → Hook + 质检风险校验 →
  沙箱执行代码修改与自测 → 评测迭代效果，**未达标持续循环修复** →
  达标后沉淀记忆、生成版本变更单 → 人工审批后台审核 → 审批通过自动版本升级，驳回终止迭代」

裁定 U08：**不引入 Claude Code 本体**，仅借用其设计思维与循环逻辑，自研 Agentic Loop。
裁定 U32：~{max_iterations = 8}~；连续 2 轮无进展提前终止；达上限归档为「未收敛」转人工。

为什么循环必须有界
------------------
离线链路会自动改代码。一个没有硬上限的自纠错循环，出错时的代价是**持续改坏仓库**，
而不是「多跑一会儿」。因此上限与无进展检测都是**硬停**，不是建议。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ai_butler.core.event_journal import EV_CHECKPOINT, EV_STAGE, EventJournal
from ai_butler.evaluation.redaction import Redactor
from ai_butler.harness.context import CompressedContext, ContextItem, compress
from ai_butler.harness.hooks import (
    HOOK_ALLOW,
    HOOK_DENY,
    HOOK_NEEDS_APPROVAL,
    HookChain,
    HookVerdict,
)
from ai_butler.harness.memory import MemoryEntry
from ai_butler.harness.risk import Change
from ai_butler.harness.sandbox import DEFAULT_TIMEOUT, SandboxPort, SandboxResult

#: 四阶循环（计划书逐字）
STAGE_COLLECT = "context_collection"
STAGE_DECIDE = "llm_decision"
STAGE_EXECUTE = "tool_execution"
STAGE_CORRECT = "self_correction"
LOOP_STAGES: tuple[str, ...] = (STAGE_COLLECT, STAGE_DECIDE, STAGE_EXECUTE,
                                STAGE_CORRECT)

#: 裁定 U32
MAX_ITERATIONS = 8
NO_PROGRESS_LIMIT = 2


class LoopError(RuntimeError):
    pass


@dataclass(frozen=True)
class Proposal:
    """LLM 决策阶段产出的一次提案。"""

    summary: str
    change: Change | None = None
    commands: tuple[str, ...] = ()
    done: bool = False


@dataclass(frozen=True)
class StageRecord:
    iteration: int
    stage: str
    detail: str
    outcome: str = ""
    #: 结构化附注（U78）。`detail` 给人读，`meta` 给代码判。
    #: 让下游去 `startswith("应用补丁")` 匹配中文，就是 E-44 那类漂移隐患；
    #: 判据必须是字段，不是句子。
    meta: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"iteration": self.iteration, "stage": self.stage,
                "detail": self.detail, "outcome": self.outcome,
                "meta": dict(self.meta)}


@dataclass
class LoopResult:
    converged: bool
    iterations: int
    reason: str
    records: list[StageRecord] = field(default_factory=list)
    sandbox_results: list[SandboxResult] = field(default_factory=list)
    #: 终局的 Hook 判定（**deny 与 needs_approval 都要记**）。
    #: 原实现只在 needs_approval 时记录，导致 blocked 在真正被拦截时恒为 False。
    verdict: HookVerdict | None = None
    pending_approval: HookVerdict | None = None
    memory_entries: list[MemoryEntry] = field(default_factory=list)
    context: CompressedContext | None = None
    #: 补丁应用结果（U78）。空表示本轮从未动过代码。
    patch_results: list[Any] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict is not None and self.verdict.blocked

    def as_dict(self) -> dict[str, Any]:
        return {"converged": self.converged, "iterations": self.iterations,
                "reason": self.reason, "stages": [r.as_dict() for r in self.records],
                "sandbox": [r.as_dict() for r in self.sandbox_results],
                "patches": [p.as_dict() for p in self.patch_results],
                "blocked": self.blocked,
                "verdict": self.verdict.as_dict() if self.verdict else None,
                "pending_approval": (self.pending_approval.as_dict()
                                     if self.pending_approval else None),
                "memory": [e.key for e in self.memory_entries]}


#: 决策函数：拿上下文与本轮历史，产出提案
Decider = Callable[[CompressedContext, Sequence[StageRecord]], Awaitable[Proposal]]


@dataclass
class AgenticLoop:
    """四阶 Agentic Loop。

    `patcher`（U78）是**补丁落盘端口**，可缺省。它的位置被刻意定死在
    「Hook 之后、命令之前」：

        Hook 判风险（不看代码就跑） → 应用补丁 → 跑验证命令 → 绿了保留 / 红了回滚

    顺序不能调换。先应用再判 Hook，等于让高危变更先落地再被拦；
    先跑命令再应用，则命令验证的是**没改过的代码**，全部作废。
    """

    decide: Decider
    hooks: HookChain
    sandbox: SandboxPort
    patcher: Any = None          # PatchPort（harness.patch.PatchApplier）
    max_iterations: int = MAX_ITERATIONS
    no_progress_limit: int = NO_PROGRESS_LIMIT
    timeout: int = DEFAULT_TIMEOUT

    async def run(self, *, goal: str, items: Sequence[ContextItem],
                  redactor: Redactor, summarizer: Callable[[str], str] | None = None,
                  max_items: int = 20,
                  journal: EventJournal | None = None) -> LoopResult:
        """跑四阶循环。

        `journal`（U57）传入即**实时**落账：每条 StageRecord 与终局快照都追加到
        append-only 账本，进程崩溃后可从账本回放恢复，不依赖本方法的返回值。
        """
        result = LoopResult(converged=False, iterations=0, reason="")

        def _emit(record: StageRecord) -> None:
            result.records.append(record)
            if journal is not None:
                journal.append(EV_STAGE, record.as_dict())

        try:
            return await self._run_inner(goal=goal, items=items, redactor=redactor,
                                         summarizer=summarizer, max_items=max_items,
                                         result=result, emit=_emit, journal=journal)
        finally:
            # 兜底回滚（U78）：任何未 commit 的补丁都不得留在仓库里。
            # 覆盖本方法的 4 处 return（Hook 拦截 / 审批暂停 / 无进展 / 上限归档）
            # 以及 decide 抛异常的情形——「改了代码但循环异常退出」
            # 是最容易留下半成品改动的路径。
            self._rollback_pending()
            # 终局快照入账：4 处 return（含 Hook 拦截/审批暂停/上限归档）都覆盖。
            # 崩溃恢复 = 重放此 checkpoint 及其后事件。
            if journal is not None:
                journal.append(EV_CHECKPOINT, {"loop": result.as_dict()})

    def _rollback_pending(self) -> tuple[str, ...]:
        """回滚尚未 commit 的补丁。**幂等**，无 patcher 时返回空。"""
        if self.patcher is None:
            return ()
        pending = getattr(self.patcher, "pending", ())
        if not pending:
            return ()
        return tuple(self.patcher.rollback())

    async def _run_inner(self, *, goal: str, items: Sequence[ContextItem],
                         redactor: Redactor,
                         summarizer: Callable[[str], str] | None,
                         max_items: int, result: LoopResult,
                         emit: Callable[[StageRecord], None],
                         journal: EventJournal | None = None) -> LoopResult:

        # ---- 阶段一：上下文收集（含脱敏与压缩）----
        # U58：journal 传入即把让位记录与分项计量落账（只让位不删除，
        # 崩溃恢复后知道压掉了什么、为什么）。
        context = compress(items, redactor=redactor, max_items=max_items,
                           summarizer=summarizer, where="Harness 上下文",
                           journal=journal)
        result.context = context
        emit(StageRecord(
            0, STAGE_COLLECT,
            f"收集 {len(context.items)} 条上下文（丢弃 {context.dropped} 条），目标：{goal}"))

        no_progress_streak = 0
        for iteration in range(1, self.max_iterations + 1):
            result.iterations = iteration

            # ---- 阶段二：LLM 决策 ----
            proposal = await self.decide(context, tuple(result.records))
            emit(StageRecord(
                iteration, STAGE_DECIDE, proposal.summary,
                "done" if proposal.done else "continue"))

            # ---- 阶段三：工具执行（**执行前必须过 Hook**）----
            if proposal.change is not None:
                verdict = self.hooks.evaluate(proposal.change)
                emit(StageRecord(
                    iteration, STAGE_EXECUTE,
                    f"Hook 结论 {verdict.outcome}（风险 {verdict.risk.level}）",
                    verdict.outcome))
                if verdict.outcome == HOOK_DENY:
                    result.verdict = verdict
                    result.reason = "Hook 拦截，终止迭代：" + "；".join(
                        f.detail for f in verdict.findings)
                    return result
                if verdict.outcome == HOOK_NEEDS_APPROVAL:
                    result.verdict = verdict
                    result.pending_approval = verdict
                    result.reason = "待人工审批，迭代暂停：" + "；".join(
                        f.detail for f in verdict.findings)
                    return result

            executed: list[SandboxResult] = []
            # ---- 补丁落盘：**Hook 已放行，现在才允许动代码**（U78）----
            applied: tuple[str, ...] = ()
            apply_failed = ""
            if (self.patcher is not None and proposal.change is not None
                    and proposal.change.diff.strip()):
                outcome = self.patcher.apply(proposal.change)
                result.patch_results.append(outcome)
                if outcome.ok:
                    applied = tuple(outcome.paths)
                    emit(StageRecord(
                        iteration, STAGE_EXECUTE,
                        f"应用补丁 {'、'.join(outcome.paths)}（{outcome.touched} 行）",
                        "ok", {"patch_applied": True,
                               "patch_paths": list(outcome.paths),
                               "patch_touched": outcome.touched}))
                else:
                    apply_failed = outcome.reason
                    emit(StageRecord(
                        iteration, STAGE_EXECUTE,
                        f"补丁未能应用（未改动任何文件）：{outcome.reason[:240]}",
                        "fail", {"patch_applied": False,
                                 "patch_error": outcome.reason}))

            if not apply_failed:
                for command in proposal.commands:
                    outcome = self.sandbox.run(command, timeout=self.timeout)
                    executed.append(outcome)
                    result.sandbox_results.append(outcome)
                    emit(StageRecord(
                        iteration, STAGE_EXECUTE,
                        f"执行 {command} → exit={outcome.exit_code}",
                        "ok" if outcome.ok else "fail"))

            # ---- 阶段四：结果自纠错 ----
            # `verified` 只有一种取真方式：**跑了命令且全绿**。
            # 「没跑命令」不等于「没问题」——空命令集一律视为未验证。
            verified = bool(executed) and all(r.ok for r in executed)

            if applied:
                if verified:
                    # 验证通过才保留。commit 之后此轮改动才不再可回滚，
                    # 置后到这一步是刻意的：先 commit 再验证等于没有回滚能力。
                    self.patcher.commit()
                else:
                    restored = self.patcher.rollback()
                    emit(StageRecord(
                        iteration, STAGE_CORRECT,
                        f"验证未通过，已回滚 {'、'.join(restored)}："
                        f"未验证的改动留在仓库里是这条链路最坏的失败模式",
                        "rollback", {"rolled_back": True,
                                     "restored": list(restored)}))

            made_progress = verified and not apply_failed
            if not executed:
                made_progress = False

            if proposal.done and (not executed or made_progress):
                result.converged = True
                result.reason = "已达标，循环收敛"
                emit(StageRecord(iteration, STAGE_CORRECT,
                                 "判定达标", "converged",
                                 {"verified": verified,
                                  "patch_applied": bool(applied)}))
                return result

            no_progress_streak = 0 if made_progress else no_progress_streak + 1
            emit(StageRecord(
                iteration, STAGE_CORRECT,
                f"未达标（{'有进展' if made_progress else f'连续 {no_progress_streak} 轮无进展'}）",
                "progress" if made_progress else "stalled",
                # 下游据这两个字段判断「这一轮到底改没改、验没验」，
                # 而不是匹配 detail 里的中文
                {"verified": verified, "patch_applied": bool(applied),
                 "rolled_back": bool(applied and not verified)}))
            if no_progress_streak >= self.no_progress_limit:
                result.reason = (
                    f"连续 {no_progress_streak} 轮无实质进展，提前终止（U32），转人工")
                return result

        result.reason = f"达到迭代上限 {self.max_iterations} 轮仍未收敛，归档转人工（U32）"
        return result
