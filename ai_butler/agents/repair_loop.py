"""评测中台 × Harness 的离线修复闭环 —— 装配点与驱动入口（U80）。

计划书依据
----------
- 三、P9「评测中台对全链路做客观评价」——评完要能指出问题
- 三、P10「评测迭代效果，**未达标持续循环修复**」——指出问题后要有人修、
  修完要再评一轮；这条链路不是三个类的集合，是一段**有固定顺序的流程**
- 用户指令（2026-10-25）：「评测中台和 harness 是两个 agent，但是可以在场景下
  配合使用……修改好后评测总台再进行一轮评测确保 harness 没有出错和胡乱修改」

为什么必须有这个模块（E-77 第 N 次复发）
--------------------------------------
U78/U79 之后，`EvaluationPlatformAgent`、`HarnessAgent`、`StreamTraceProducer`
三件东西各自都有实现、都有测试，但**全项目没有一个地方把它们装在一起**：
`ButlerAgent.enroll()` 只收带 `phase_code` 的阶段 Agent（评测中台是横切 Agent，
没有 `phase_code`，被直接拒收），`StreamTraceProducer` 一次都没被构造过。
结论：闭环只在测试和一次性脚本里存在过。

「类可用」和「链路在用」是两件事。本模块给这条链路一个**可导入、可断言的
装配点**（`build_repair_loop`）和一个**唯一的驱动入口**（`RepairLoop.run`），
于是「有没有插电」能像 `app.build_chat_orchestrator` 那样被一条断言钉住。

三道不许省的纪律
----------------
1. **顺序固定：评 → 修 → 复评。** 不把顺序交给调用方自觉。反过来（先修再评）
   就等于让没有任何基线的东西自证有用。
2. **不采信被评方自评。** harness 的 `converged` / `done` / `patch_applied`
   只作为记录写进结论，**不作结论本身**。唯一从它那里取的是「它想改的 diff」，
   而那是送去审计的对象。
3. **能停在半路，且停得有理有据。** 三种情况**不往下走**，每一种都写明原因：
   首轮全绿（无需修）、存在未解决的安全缺陷（U76 整批阻断）、
   harness 没产出可用补丁（没改就没有「改动效果」可评）。
   硬走下去的后果是一样的：把「没改」评成「白改」，或者把「没测」评成「通过」。

本模块**不实现**的三件事（如实登记，不在这里假装）
--------------------------------------------------
- **首轮痕迹的来源**：调用方给（线上日志 / U57 事件账本回放）。本模块不生产
  第一轮的痕迹，只负责「修完再产一次」。
- **跨轮重试**：一轮没修好就停在该结论上。第二轮要在**已提交补丁**的基础上
  再改，而「撤销一个已提交的补丁」本身是独立的高危能力（`PatchApplier` 的
  `rollback` 在 `commit` 之后即失效，这是刻意的）。没有撤销就重试，
  是在一个可能已经改坏的基线上叠加。
- **自动挂到定时任务**：本模块只提供装配点与驱动；排程属运行时集成。
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ai_butler.agents.capabilities import grant
from ai_butler.agents.eval_platform import EvaluationPlatformAgent
from ai_butler.agents.p10_harness import HarnessAgent
from ai_butler.agents.state import new_state
from ai_butler.evaluation.producer import COLLECTOR_USER_ID, StreamTraceProducer

__all__ = [
    "CHAT_PIPELINE_MODULES",
    "REPAIR_USER_ID",
    "PipelineHolder",
    "RepairLoop",
    "RepairOutcome",
    "RepairStage",
    "build_pipeline_producer",
    "build_repair_loop",
    "reload_targets",
]

#: 闭环运行时的系统身份。理由同 `coder.CODER_USER_ID`：离线链路没有登录用户，
#: 不借用真实用户的话务与配额。
REPAIR_USER_ID = "system:p10-repair"

#: 对话管道的**直接依赖链**，顺序是「被依赖者在前」。
#:
#: `chat.py` 里写的是 `from ai_butler.retrieval.planner import plan as plan_query`：
#: 名字在 **import 时**就绑进了 chat 的模块全局。所以只 reload planner 不够——
#: chat 仍指向旧函数；必须再把 chat 也 reload 一遍，它才会重新 import。
#: 漏一个不会报错，只会安静地评旧代码（E-83）。
CHAT_PIPELINE_MODULES: tuple[str, ...] = (
    "ai_butler.retrieval.planner",
    "ai_butler.material_center.verification",
    "ai_butler.retrieval.chat",
)


# --------------------------------------------------------------------------- #
# 一、管道持有者：补丁落盘后重建被测对象
# --------------------------------------------------------------------------- #
@dataclass
class PipelineHolder:
    """持有「当前这条管道」，可在补丁落盘后重载模块并重建。

    `builder` 是**延迟导入**的：它在函数体内 `from ... import ChatOrchestrator`，
    而不是在模块顶部 import 好再传进来。写成顶部导入就白 reload 了——
    顶部那个名字指向旧类，重建出来的还是旧实例。
    """

    builder: Callable[[], Any]
    base_modules: Sequence[str] = CHAT_PIPELINE_MODULES
    user_id: str = COLLECTOR_USER_ID
    orchestrator: Any = field(default=None, init=False)
    refreshes: int = field(default=0, init=False)
    #: 本次重建实际 reload 成功的模块（观测用：一个都没 reload 过就是没刷新）
    reloaded: list[str] = field(default_factory=list, init=False)
    #: 由驱动按本次补丁触碰的模块补进来的额外重载目标（排在 base 之前）
    extra_modules: list[str] = field(default_factory=list, init=False)

    def refresh(self) -> None:
        """按「被依赖者在前」的顺序 reload，然后重建管道。"""
        targets = tuple(dict.fromkeys([*self.extra_modules, *self.base_modules]))
        self.reloaded = [name for name in targets if _reload_module(name)]
        self.orchestrator = self.builder()
        self.refreshes += 1

    def __call__(self, query: str) -> Any:
        if self.orchestrator is None:
            self.refresh()
        return self.orchestrator.stream(user_id=self.user_id, query=query)


def _reload_module(name: str) -> bool:
    """reload 一个已加载的模块。**没加载过就跳过**（没什么可刷新的）。

    主路径是 `importlib.reload`。它按模块名重新找 spec，所以从任意路径
    加载的模块（`spec_from_file_location`，测试里的管道替身就是这么进来的）
    会 `ModuleNotFoundError`；那种情况退回「用原 loader 重执行」——
    效果等价（同一次模块对象上重跑一遍源码），只是不刷新 `__spec__`。
    """
    module = sys.modules.get(name)
    if module is None:
        return False
    # spec 必须在 reload **之前**取：失败的那次 reload 会把它置成 None
    spec = getattr(module, "__spec__", None)
    loader = getattr(spec, "loader", None)
    try:
        importlib.reload(module)
        return True
    except ImportError:
        if loader is None or not hasattr(loader, "exec_module"):
            raise
        loader.exec_module(module)
        module.__spec__ = spec
        return True


def build_pipeline_producer(*, builder: Callable[[], Any],
                            runs: int = 2,
                            modules: Sequence[str] = CHAT_PIPELINE_MODULES,
                            user_id: str = COLLECTOR_USER_ID,
                            ) -> StreamTraceProducer:
    """装配**真实对话管道**的痕迹生产端。

    装配期就建一次管道：`builder` 里的导入错误当场暴露，
    而不是等复评跑到一半才炸（那时会被记成「复评失败」，很难指向真正的原因）。
    """
    holder = PipelineHolder(builder=builder, base_modules=tuple(modules),
                            user_id=user_id)
    holder.refresh()
    # `StreamTraceProducer.refreshes` 与 `holder.refreshes` 是两套计数：
    # 前者由 producer 自己加（它只知道「我调过 before_run」），
    # 后者是 holder 真实重建的次数。两者应当恒等，不等就是接线接错了。
    return StreamTraceProducer(stream_factory=holder, runs=runs,
                               before_run=holder.refresh)


def reload_targets(diff: str, *,
                   base: Sequence[str] = CHAT_PIPELINE_MODULES) -> tuple[str, ...]:
    """本次补丁碰到的模块 + 管道自身，按「被依赖者在前」排好序。

    补丁可能改在管道依赖链之外（例如 `scheduler/router.py`，chat 从它那里
    import `IntentRouter`）。那种模块必须在管道之前 reload，否则管道重新
    import 到的还是旧符号。解析失败时**只回 base**：宁可多 reload，
    不可漏——漏了就是安静地评旧代码。
    """
    touched: list[str] = []
    try:
        from ai_butler.harness.patch import parse_unified_diff
        patch = parse_unified_diff(diff)
    except Exception:                                   # noqa: BLE001
        return tuple(base)
    for file_patch in patch.files:
        path = str(file_patch.path or "").strip()
        if not path.endswith(".py"):
            continue
        module = path[:-3].replace("/", ".").replace("\\", ".")
        if module and module not in touched:
            touched.append(module)
    extra = [m for m in touched if m not in base]
    return tuple(dict.fromkeys([*extra, *base]))


# --------------------------------------------------------------------------- #
# 二、闭环的产物
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RepairStage:
    """闭环里的一段。**三段都留痕**：停在哪一段，就是结论的全部。"""

    name: str                     # evaluate / repair / recheck
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class RepairOutcome:
    """一次闭环运行的全部结论。

    `verdict` 为空字符串表示**没走到复评**——那不是一种复评结论，
    是「这一轮不需要/不允许复评」。把它硬填成 `accepted` 或
    `rejected_*` 都会骗人，所以留空并让 `reason` 说清楚为什么。
    """

    user_id: str
    stages: tuple[RepairStage, ...] = ()
    verdict: str = ""
    accepted: bool = False
    #: 首轮评测就没有缺陷（不是「修好了」，是「不用修」）
    clean: bool = False
    #: 停在半路，需要人接手
    requires_manual: bool = False
    reason: str = ""
    case_count: int = 0
    defect_count: int = 0
    #: harness 段的摘要（applied / diff_chars / coder_wired）：**只作记录**
    repair: Mapping[str, Any] = field(default_factory=dict)
    #: 复评段的摘要（fixed / regressed / after_defects / new_kinds）
    recheck: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "verdict": self.verdict or None,
            "accepted": self.accepted,
            "clean": self.clean,
            "requires_manual": self.requires_manual,
            "reason": self.reason,
            "case_count": self.case_count,
            "defect_count": self.defect_count,
            "stages": [s.as_dict() for s in self.stages],
            "repair": dict(self.repair),
            "recheck": dict(self.recheck),
        }


# --------------------------------------------------------------------------- #
# 三、唯一装配点
# --------------------------------------------------------------------------- #
def build_repair_loop(*, builder: Callable[[], Any] | None = None,
                      runs: int = 2,
                      modules: Sequence[str] = CHAT_PIPELINE_MODULES,
                      sandbox: Any = None,
                      coder: Any = None,
                      patch_root: str | None = None,
                      judge: Any = None,
                      analyzer: Any = None,
                      producer: Any = None,
                      user_id: str = REPAIR_USER_ID) -> RepairLoop:
    """装配「评测中台 → harness → 复评」这条闭环。**唯一装配点。**

    `builder`（重建对话管道的工厂）与 `producer`（痕迹生产端）二选一：

    - 给了 `builder`：装配真实管道生产端，复评会**真跑**用例；
    - 只给 `producer`：用调用方给的生产端（测试替身 / 别的管道）；
    - 都不给：`producer` 为 None。这时复评只能给 `UNVERIFIED`——
      **这是刻意的默认**，不是忘了接：拿「补丁已应用」推断「修好了」
      正是这条链路最容易撒的谎。
    """
    if producer is not None and builder is not None:
        # 两个都给：不安静地挑一个。挑哪个都可能是错的，而错的方式是
        # 「复评跑的是另一条管道」——结论看起来完全正常，只是不对。
        raise ValueError(
            "producer 与 builder 只能给一个：前者是现成的痕迹生产端，"
            "后者用来装配真实管道的生产端。同时给无法判断复评该信哪条管道。")
    if producer is None and builder is not None:
        producer = build_pipeline_producer(builder=builder, runs=runs, modules=modules)
    evaluator = EvaluationPlatformAgent(
        tools=grant(EvaluationPlatformAgent, {}),
        judge=judge, analyzer=analyzer, producer=producer)
    harness = HarnessAgent(
        tools=grant(HarnessAgent, {}), sandbox=sandbox, coder=coder,
        patch_root=patch_root)
    return RepairLoop(evaluator=evaluator, harness=harness, user_id=user_id)


# --------------------------------------------------------------------------- #
# 四、唯一驱动入口
# --------------------------------------------------------------------------- #
@dataclass
class RepairLoop:
    """把两个 Agent 按固定顺序驱动一遍。

    `evaluator` / `harness` 允许注入替身（测试用），但**顺序不留参数**：
    它是这条链路的安全属性，不是可配置项。
    """

    evaluator: Any
    harness: Any
    user_id: str = REPAIR_USER_ID

    async def run(self, *, cases: Sequence[Mapping[str, Any]],
                  traces: Sequence[Mapping[str, Any]],
                  extra: Mapping[str, Any] | None = None) -> RepairOutcome:
        """跑一遍闭环。`cases` / `traces` 是 State 形态的 dict（可 JSON 化）。"""
        case_list = [dict(c) for c in cases]
        trace_list = [dict(t) for t in traces]

        state: dict[str, Any] = new_state(user_id=self.user_id,
                                          session_id="repair-loop")
        # State 只放可 JSON 化的东西：评测中台自己 `from_dict` 回来，
        # 形态不对就在那里报错（半套痕迹评出来的结论比没有结论更危险）。
        state["eval_cases"] = case_list
        state["agent_traces"] = trace_list
        state.update(dict(extra or {}))

        stages: list[RepairStage] = []

        # ---- 第一段：评 ----
        state.update(self.evaluator.run(state))
        section = _section(state, "eval_platform")
        summary = section.get("summary") or {}
        case_count = int(section.get("case_count") or 0)
        defects = section.get("defects") or {}
        defect_count = len(defects.get("defects") or ())
        stages.append(RepairStage(
            "evaluate", True,
            f"{case_count} 个用例，{summary.get('passed', 0)} 个通过，"
            f"{defect_count} 条缺陷，均分 {summary.get('mean_score', 0.0)}"))

        if case_count == 0:
            # 「没测」不等于「通过」：0 个用例时缺陷数必然是 0，看着最干净
            return self._outcome(stages, case_count=0, defect_count=0,
                                 requires_manual=True,
                                 reason="没有任何用例：没测不等于通过，闭环不予启动")
        if defect_count == 0:
            return self._outcome(stages, case_count=case_count, defect_count=0,
                                 clean=True,
                                 reason="首轮评测全部通过，无需修复")
        if bool(defects.get("blocked_by_security")):
            return self._outcome(
                stages, case_count=case_count, defect_count=defect_count,
                requires_manual=True,
                reason=("存在未解决的安全缺陷：按 U76 整批阻断，不自动改代码。"
                        "先人工处理安全问题——在会越权的系统上优化别的指标没有意义"))

        # ---- 第二段：修 ----
        state.update(await self.harness.run(state))
        section = _section(state, "p10_harness")
        patch = section.get("patch") if isinstance(section.get("patch"),
                                                   Mapping) else {}
        applied = bool((patch or {}).get("applied"))
        diff = str((patch or {}).get("diff") or "")
        repair = {"applied": applied, "diff_chars": len(diff),
                  "coder_wired": bool((patch or {}).get("coder_wired")),
                  "converged": bool(section.get("converged"))}

        if not bool(section.get("ran", True)):
            # 沙箱没接：harness 根本没跑，不是「跑了没改」
            reason = str(section.get("reason") or "harness 未运行")
            stages.append(RepairStage("repair", False, f"未运行：{reason}"))
            return self._outcome(stages, case_count=case_count,
                                 defect_count=defect_count, repair=repair,
                                 requires_manual=True, reason=f"harness 未运行：{reason}")

        stages.append(RepairStage(
            "repair", applied,
            f"补丁应用={applied}，改动 {len(diff)} 字符，"
            f"循环{'' if repair['converged'] else '未'}收敛（自评不作为结论）"))
        if not applied:
            reason = str((section.get("patch_not_applied") or {}).get("reason") or "")
            return self._outcome(
                stages, case_count=case_count, defect_count=defect_count,
                repair=repair, requires_manual=True,
                reason=(f"harness 未产出可用补丁，没有改动就没有「改动效果」可评：{reason}"
                        if reason else "harness 未产出可用补丁，无改动可评"))

        # ---- 第三段：复评 ----
        self._focus_on(diff)
        state.update(await self.evaluator.recheck(state))
        section = _section(state, "eval_platform_recheck")
        verdict = str(section.get("verdict") or "")
        accepted = bool(section.get("accepted"))
        stages.append(RepairStage("recheck", accepted,
                                  f"{verdict}：{section.get('reason', '')}"))
        recheck = {k: section.get(k) for k in
                   ("verdict", "fixed", "regressed", "still_broken",
                    "before_defects", "after_defects", "new_kinds")
                   if k in section}
        return self._outcome(
            stages, case_count=case_count, defect_count=defect_count,
            repair=repair, recheck=recheck, verdict=verdict, accepted=accepted,
            requires_manual=not accepted,
            reason=str(section.get("reason") or ""))

    # ------------------------------------------------------------------ #
    def _focus_on(self, diff: str) -> None:
        """按本次补丁触碰的模块，收窄下一轮的重载目标。

        不改这里的后果：补丁改在 `scheduler/router.py` 这类管道**依赖**上时，
        只 reload 管道本身仍会拿到旧的被导入符号——复评安静地评旧代码（E-83）。

        只对 `PipelineHolder`（真实管道）生效。别的生产端自己负责刷新：
        本模块不知道它是怎么造出来的，替它猜只会把「没刷新」藏得更深。
        """
        producer = getattr(self.evaluator, "producer", None)
        holder = getattr(producer, "stream_factory", None)
        if isinstance(holder, PipelineHolder):
            holder.extra_modules = list(reload_targets(diff))

    def _outcome(self, stages: Sequence[RepairStage], **kwargs: Any) -> RepairOutcome:
        return RepairOutcome(user_id=self.user_id, stages=tuple(stages), **kwargs)


def _section(state: Mapping[str, Any], key: str) -> dict[str, Any]:
    raw = state.get(key)
    return dict(raw) if isinstance(raw, Mapping) else {}
