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

本模块**不实现**的两件事（如实登记，不在这里假装）
--------------------------------------------------
- **首轮痕迹的来源**：调用方给（线上日志 / U57 事件账本回放）。本模块不生产
  第一轮的痕迹，只负责「修完再产一次」。
- **自动挂到定时任务**：本模块只提供装配点与驱动；排程属运行时集成。

跨轮重试（U81）
---------------
上一轮把这里登记为「不实现」，理由是撤销一个**已提交**补丁是独立的高危能力。
本轮把它补上，但不是把 `rollback()` 改成可重复调用——那是把高危能力
混进高频路径。分层的做法：

| 动作 | 触发者 | 门槛 |
|---|---|---|
| `PatchApplier.rollback()` | 机械层，验证失败当场 | 无（改动尚未生效） |
| `RevisionLedger.undo()` | 驱动，跨轮要重改同一文件时 | **人工审批票**，签名绑定轮次，漂移即拒 |

四道约束（用户 2026-10-25 明确要求，缺一条这轮就不该叫「能重试」）
------------------------------------------------------------------
1. **边界**：本轮只许动本轮缺陷所属的板块（`RoundScope.allowed_prefixes`），
   且不许动前几轮已经改过的文件（`locked_paths`）。
   同一文件叠两份自动补丁，出问题时无法说清是哪一轮造成的——
   责任不可分割的自动化改动，比改错更难处理。
2. **错误信息必须已更改**：每轮复评后用 `evaluation/fingerprint.py` 比对
   缺陷证据形态。一字未变就停（`no_progress`）：跑满 N 轮却从未推进，
   只是把同一个动作重复了 N 次，代价是 N 次模型调用 + N 次全量评测。
3. **撤销已提交补丁需人工确认**：冲突文件对应的补丁必须先撤，
   撤销要一张一次性审批票；没有票就停在 `requires_approval`，
   把「撤哪一轮、为什么」写清楚交出去。
4. **每轮可溯源**：`harness/trail.py` 的 `RoundRecord` 覆盖六段——
   问题在哪、本轮边界、改了什么、为什么这样改、验了什么、判成什么样。
   `RepairLoop.explain()` 与 `RepairTrail.trace_defect()` 是它的两个读口。
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ai_butler.agents.capabilities import grant
from ai_butler.agents.eval_platform import EvaluationPlatformAgent
from ai_butler.agents.p10_harness import HarnessAgent
from ai_butler.agents.state import new_state
from ai_butler.core.approval import ApprovalError, ApprovalService
from ai_butler.evaluation.defects import DefectBatch, batch_from_dict, defects_from_reports
from ai_butler.evaluation.fingerprint import DefectFingerprint, fingerprint_of
from ai_butler.evaluation.platform import PlatformReport
from ai_butler.evaluation.producer import COLLECTOR_USER_ID, StreamTraceProducer
from ai_butler.evaluation.recheck import RecheckVerdict
from ai_butler.harness.patch import ALLOWED_SCOPE_BY_KIND, FORBIDDEN_PATHS
from ai_butler.harness.intake import files_from_hint
from ai_butler.harness.revision import (
    UNDO_TOOL,
    CommittedPatch,
    RevisionError,
    RevisionLedger,
    undo_signature,
)
from ai_butler.harness.trail import RepairTrail, RoundRecord

__all__ = [
    "CHAT_PIPELINE_MODULES",
    "DEFAULT_MAX_ROUNDS",
    "REPAIR_USER_ID",
    "PipelineHolder",
    "RepairLoop",
    "RepairOutcome",
    "RepairStage",
    "RoundScope",
    "build_pipeline_producer",
    "build_repair_loop",
    "reload_targets",
]

#: 闭环运行时的系统身份。理由同 `coder.CODER_USER_ID`：离线链路没有登录用户，
#: 不借用真实用户的话务与配额。
REPAIR_USER_ID = "system:p10-repair"

#: 一轮闭环最多跑几轮（实现参数，计划书未给刻度）。
#:
#: 取 3 而非更大的理由不是成本，是**归因能力**：第 4 轮开始时，第一轮改动
#: 的上下文已经被两轮补丁盖住，人类复核时无法判断「现在这行是谁写的」。
#: 超过 3 轮该退回人工重新定位，而不是让自动链路继续叠加。
DEFAULT_MAX_ROUNDS = 3

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
    """闭环里的一段。**三段都留痕**：停在哪一段，就是结论的全部。

    `round_id` 单独一列而不是拼进 `name`：多轮时会出现多个同名段
    （`evaluate` / `recheck` 每轮各一次），把轮次拼进名字会让
    「按 stage 名统计」立刻失效——那是下游看板正在做的事。
    """

    name: str                     # evaluate / repair / recheck / fingerprint / boundary
    ok: bool
    detail: str
    round_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "ok": self.ok, "detail": self.detail,
                "round_id": self.round_id}


@dataclass(frozen=True)
class RoundScope:
    """一轮的**边界**：这一轮只许动什么、什么被锁住（U81）。

    用户的要求是「保障修改的代码只负责自己的板块」。落到判据上是三条：

    - `allowed_prefixes`：本轮受理的缺陷类别所对应的子系统（并集）。
      多个缺陷时取并集而不是交集——交集会让「同时有两个板块的缺陷」变成
      谁都改不了，那不是收紧，是卡死。
    - `locked_paths`：前几轮**已提交**补丁动过的文件。本轮不可再改。
    - `conflicts`：本轮缺陷**直接指向**、却已被锁住的文件。
      非空 = 要修就必须先撤销，而撤销要人签字。

    `conflicts` 之所以必须单独列出来而不是「碰巧被 locked 拦下就好」：
    两者的处置相反——前者是「等你签字我就能干」，后者是「这不是你该碰的」。
    混成一个「越界」会让审批的人不知道该批什么。
    """

    round_id: str
    allowed_prefixes: tuple[str, ...] = ()
    locked_paths: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    #: 撤销这些轮次才能解锁（按提交先后）。空 = 不需要撤销。
    undo_rounds: tuple[str, ...] = ()

    @property
    def needs_undo(self) -> bool:
        return bool(self.conflicts and self.undo_rounds)

    def as_dict(self) -> dict[str, Any]:
        return {"round_id": self.round_id,
                "allowed_prefixes": list(self.allowed_prefixes),
                "locked_paths": list(self.locked_paths),
                "conflicts": list(self.conflicts),
                "undo_rounds": list(self.undo_rounds),
                "needs_undo": self.needs_undo}


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
    #: 停在一张审批票上（撤销已提交补丁需要人工确认，U81）
    requires_approval: bool = False
    #: 需要人工批准的那张票（`requires_approval=True` 时非空）
    approval_request: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    case_count: int = 0
    defect_count: int = 0
    rounds_run: int = 0
    #: harness 段的摘要（applied / diff_chars / coder_wired）：**只作记录**
    repair: Mapping[str, Any] = field(default_factory=dict)
    #: 复评段的摘要（fixed / regressed / after_defects / new_kinds / remaining）
    recheck: Mapping[str, Any] = field(default_factory=dict)
    #: 每轮的溯源摘要（U81）。全文在 `RepairTrail` 里，这里只放能直接读的部分。
    rounds: tuple[Mapping[str, Any], ...] = ()
    #: 本轮实际执行的撤销（撤销成功后才有）
    undone: tuple[Mapping[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "verdict": self.verdict or None,
            "accepted": self.accepted,
            "clean": self.clean,
            "requires_manual": self.requires_manual,
            "requires_approval": self.requires_approval,
            "approval_request": dict(self.approval_request),
            "reason": self.reason,
            "case_count": self.case_count,
            "defect_count": self.defect_count,
            "rounds_run": self.rounds_run,
            "stages": [s.as_dict() for s in self.stages],
            "repair": dict(self.repair),
            "recheck": dict(self.recheck),
            "rounds": [dict(r) for r in self.rounds],
            "undone": [dict(u) for u in self.undone],
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
                      max_rounds: int = DEFAULT_MAX_ROUNDS,
                      ledger: RevisionLedger | None = None,
                      trail: RepairTrail | None = None,
                      approvals: ApprovalService | None = None,
                      user_id: str = REPAIR_USER_ID) -> RepairLoop:
    """装配「评测中台 → harness → 复评」这条闭环。**唯一装配点。**

    `builder`（重建对话管道的工厂）与 `producer`（痕迹生产端）二选一：

    - 给了 `builder`：装配真实管道生产端，复评会**真跑**用例；
    - 只给 `producer`：用调用方给的生产端（测试替身 / 别的管道）；
    - 都不给：`producer` 为 None。这时复评只能给 `UNVERIFIED`——
      **这是刻意的默认**，不是忘了接：拿「补丁已应用」推断「修好了」
      正是这条链路最容易撒的谎。

    跨轮设施（U81）也在这里装配，且**默认值是「能记但撤不了」**：

    - `ledger` 缺省按 `patch_root` 建（有 root 就能撤销，因为撤销要写盘）；
    - `trail` 缺省只留内存（不给路径就不落盘，测试与一次性运行不该留垃圾）；
    - `approvals` 缺省为 None。此时若有冲突文件需要撤销，闭环**停**并说明
      「需要人工确认但没有审批服务」——不给捷径。
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
    if ledger is None and patch_root:
        # 有工作根才建带 root 的账本：账本的价值一半是「知道改过什么」，
        # 另一半是「能撤回去」。没有 root 的那一半如实缺席，不假装。
        ledger = RevisionLedger(root=Path(patch_root))
    return RepairLoop(evaluator=evaluator, harness=harness, user_id=user_id,
                      max_rounds=max_rounds, ledger=ledger, trail=trail,
                      approvals=approvals)


# --------------------------------------------------------------------------- #
# 四、唯一驱动入口
# --------------------------------------------------------------------------- #
@dataclass
class RepairLoop:
    """把两个 Agent 按固定顺序驱动，失败就再来一轮（有界）。

    `evaluator` / `harness` 允许注入替身（测试用），但**顺序不留参数**：
    它是这条链路的安全属性，不是可配置项。

    `max_rounds=1` 时行为与 U80 完全一致（单轮）。这不是兼容性妥协，
    是为了让「多轮」这件事可被**对照测试**：同一批输入，一轮与三轮的差别
    必须只来自轮次本身。
    """

    evaluator: Any
    harness: Any
    user_id: str = REPAIR_USER_ID
    #: 最多跑几轮。1 = 单轮（与 U80 行为完全一致）。
    max_rounds: int = DEFAULT_MAX_ROUNDS
    #: 已提交补丁的版本账（U81）。为 None 时「能记但撤不了」——
    #: 本轮若出现需要撤销的冲突，闭环会停并说明缺什么。
    ledger: RevisionLedger | None = None
    #: 轮次溯源账。为 None 时按需建一个内存账本（`explain` 仍然可用）。
    trail: RepairTrail | None = None
    #: 人工审批服务。撤销已提交补丁**必须**经它签票（U59/U81）。
    approvals: ApprovalService | None = None
    #: 撤销票的申请主体（票与 agent/role 绑定，U51 不可传递）
    approval_agent: str = REPAIR_USER_ID
    approval_role: str = "p10-harness"
    #: 撤销票有效期（秒）。无人应答即拒（U59），不设「永久有效」。
    approval_ttl: float = 24 * 3600

    # ------------------------------------------------------------------ #
    def explain(self, round_id: str) -> str:
        """一轮的人读说明。审批前先看这个，比翻 State 快。"""
        return self._trail().explain(round_id)

    def trace_defect(self, defect_id: str) -> tuple[Any, ...]:
        """某条缺陷在各轮的遭遇（`harness.trail.DefectTrace` 序列）。"""
        return self._trail().trace_defect(defect_id)

    def _trail(self) -> RepairTrail:
        if self.trail is None:
            self.trail = RepairTrail()
        return self.trail

    def _next_round_no(self) -> int:
        """本次运行从第几轮开始编号：接在账本里已有轮次之后。

        只看 `R<n>` 形态并取最大者 + 1：账本里可能有别人写进来的其它前缀，
        那些不该影响编号；而缺口（R1、R3）不影响——重号才是问题，跳号不是。
        """
        highest = 0
        for record in self._trail().rounds():
            suffix = record.round_id[1:]
            if record.round_id.startswith("R") and suffix.isdigit():
                highest = max(highest, int(suffix))
        return highest + 1

    # ------------------------------------------------------------------ #
    async def run(self, *, cases: Sequence[Mapping[str, Any]],
                  traces: Sequence[Mapping[str, Any]],
                  extra: Mapping[str, Any] | None = None,
                  undo_approval: str = "",
                  undo_rounds: Sequence[str] = ()) -> RepairOutcome:
        """跑闭环。每一轮都完整走「评 → 修 → 复评」，最多 `max_rounds` 轮。

        `cases` / `traces` 是 State 形态的 dict（可 JSON 化）。

        `undo_approval` / `undo_rounds` 是**人工已批准**的撤销：只有
        「本轮要改的文件被前几轮锁住」或「上一轮改坏了要回到基线」时才用得上。
        一张票只撤一次（U59），且签名绑定轮次——拿 R1 的票撤 R2 一律拒。
        """
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
        rounds: list[Mapping[str, Any]] = []
        undone: list[Mapping[str, Any]] = []
        prev_fp: DefectFingerprint | None = None
        carried: tuple[PlatformReport, ...] = ()
        total_rounds = max(1, int(self.max_rounds))
        # 轮次号**跨次运行续接**：同一个闭环实例第二次 `run()` 不能又从 R1 开始。
        # 撞号的后果两处都是硬的——溯源账里两条不同的 R1（`get()` 只看得见第一条），
        # 版本账那边更直接：`record()` 拒绝同一轮二次提交，第二次根本提交不了补丁。
        base = self._next_round_no()

        for round_no in range(1, total_rounds + 1):
            tag = f"R{base + round_no - 1}"
            started_at = _now_iso()

            # ---------------- 第一段：评（或晋升上一轮的复评报告） ----------------
            if round_no == 1:
                state.update(self.evaluator.run(state))
            else:
                # 第二轮的基线**必须**是上一轮复评重跑产出的报告：
                # 用第一轮那份（修复前）当基线，会把「早就修好的」再算一次修好。
                state["eval_platform"] = self._promote(carried, len(case_list))
            section = _section(state, "eval_platform")
            summary = section.get("summary") or {}
            case_count = int(section.get("case_count") or 0)
            batch = _batch(section)
            defect_count = len(batch.defects)
            fp = fingerprint_of(batch.defects)

            stages.append(RepairStage(
                "evaluate", True,
                f"{tag}｜{case_count} 个用例，{summary.get('passed', 0)} 个通过，"
                f"{defect_count} 条缺陷，均分 {summary.get('mean_score', 0.0)}",
                tag))

            if case_count == 0:
                # 「没测」不等于「通过」：0 个用例时缺陷数必然是 0，看着最干净
                rounds.append(self._close_round(
                    round_id=tag, started_at=started_at, batch=batch, fp=fp,
                    scope=None, patch={}, rationale="", verification=(),
                    stopped="no_cases",
                    handoff="没有任何用例：没测不等于通过"))
                return self._finish(
                    stages, rounds=rounds, case_count=0, defect_count=0,
                    rounds_run=round_no, requires_manual=True,
                    reason="没有任何用例：没测不等于通过，闭环不予启动")

            if defect_count == 0:
                rounds.append(self._close_round(
                    round_id=tag, started_at=started_at, batch=batch, fp=fp,
                    scope=None, patch={}, rationale="", verification=(),
                    stopped="clean",
                    handoff="全部通过，无需修复"))
                return self._finish(
                    stages, rounds=rounds, case_count=case_count,
                    defect_count=0, rounds_run=round_no,
                    clean=(round_no == 1),
                    reason=("首轮评测全部通过，无需修复" if round_no == 1 else
                            "复评后已无缺陷"))

            if batch.blocked_by_security:
                rounds.append(self._close_round(
                    round_id=tag, started_at=started_at, batch=batch, fp=fp,
                    scope=None, patch={}, rationale="", verification=(),
                    stopped="security_block",
                    handoff="按 U76 整批阻断：先人工处理安全问题"))
                return self._finish(
                    stages, rounds=rounds, case_count=case_count,
                    defect_count=defect_count, rounds_run=round_no,
                    requires_manual=True,
                    reason=("存在未解决的安全缺陷：按 U76 整批阻断，不自动改代码。"
                            "先人工处理安全问题——在会越权的系统上优化别的指标"
                            "没有意义"))

            # ---------------- 第二道检查：基线有没有真的更新 ----------------
            # 防御「晋升出错」。正常情况下第 1 轮之后这里不会触发
            # （复评后的指纹与基线相同时，上一轮就该停了），
            # 但驱动被外部复用、或复评返回了旧报告时会撞上。
            if prev_fp is not None and fp.stable == prev_fp.stable:
                stages.append(RepairStage(
                    "fingerprint", False,
                    f"{tag}｜缺陷指纹与上一轮完全一致（{fp.stable[:12]}）："
                    f"基线没有更新，继续跑只会重复消耗", tag))
                rounds.append(self._close_round(
                    round_id=tag, started_at=started_at, batch=batch, fp=fp,
                    scope=None, patch={}, rationale="", verification=(),
                    stopped="stale_baseline",
                    handoff="基线未更新，无法判断本轮改动是否有效"))
                return self._finish(
                    stages, rounds=rounds, case_count=case_count,
                    defect_count=defect_count, rounds_run=round_no,
                    requires_manual=True,
                    reason=("本轮基线与上一轮完全相同：说明复评没有产出新报告，"
                            "继续只会重复消耗预算"))

            # ---------------- 边界：本轮只许动自己的板块 ----------------
            scope = self._scope(tag, batch)

            # 人工已批准的撤销：本轮开工前先回基线。
            # 撤销目标由人指定（`undo_rounds`），不由我们猜——
            # 猜错撤轮次等于把别人还在用的改动抹掉。
            if undo_approval and undo_rounds:
                done, failure = self._apply_undo(undo_rounds, scope,
                                                 approval_id=undo_approval)
                if failure:
                    rounds.append(self._close_round(
                        round_id=tag, started_at=started_at, batch=batch, fp=fp,
                        scope=scope, patch={}, rationale="", verification=(),
                        stopped="undo_failed", handoff=failure))
                    return self._finish(
                        stages, rounds=rounds, undone=tuple(undone),
                        case_count=case_count, defect_count=defect_count,
                        rounds_run=round_no, requires_manual=True,
                        reason=f"撤销未执行：{failure}")
                undone.extend(u.as_dict() for u in done)
                scope = self._scope(tag, batch)      # 撤销后重新算锁

            stages.append(RepairStage(
                "boundary", not scope.conflicts,
                f"{tag}｜允许改 {list(scope.allowed_prefixes) or '（无）'}；"
                f"锁定 {len(scope.locked_paths)} 个前轮已改文件"
                + (f"；本轮定位提示与锁定文件重合 {list(scope.conflicts)}"
                   f"（改不下去时需要先撤销）" if scope.conflicts else ""),
                tag))

            # ---------------- 第二段：修 ----------------
            state["patch_scope"] = scope.as_dict()
            state.update(await self.harness.run(state))
            harness_section = _section(state, "p10_harness")
            patch = (harness_section.get("patch")
                     if isinstance(harness_section.get("patch"), Mapping) else {})
            applied = bool((patch or {}).get("applied"))
            diff = str((patch or {}).get("diff") or "")
            code = (patch or {}).get("code") or []
            rationale = str((code[-1] or {}).get("rationale") or "") if code else ""
            if not rationale and code:
                rationale = str((code[-1] or {}).get("error") or "")
            repair = {"applied": applied, "diff_chars": len(diff),
                      "paths": list(_patch_view(patch).get("paths") or ()),
                      "coder_wired": bool((patch or {}).get("coder_wired")),
                      "converged": bool(harness_section.get("converged"))}

            if not bool(harness_section.get("ran", True)):
                # 沙箱没接：harness 根本没跑，不是「跑了没改」
                why = str(harness_section.get("reason") or "harness 未运行")
                stages.append(RepairStage("repair", False, f"{tag}｜未运行：{why}", tag))
                rounds.append(self._close_round(
                    round_id=tag, started_at=started_at, batch=batch, fp=fp,
                    scope=scope, patch={}, rationale="", verification=(),
                    stopped="harness_not_run", handoff=why))
                return self._finish(
                    stages, rounds=rounds, undone=tuple(undone),
                    case_count=case_count,
                    defect_count=defect_count, repair=repair,
                    rounds_run=round_no, requires_manual=True,
                    reason=f"harness 未运行：{why}")

            verification = tuple(
                f"{s.get('command', '?')} → exit={s.get('exit_code', '?')}"
                for s in (harness_section.get("result", {}) or {}).get("sandbox", ())
                if isinstance(s, Mapping))
            stages.append(RepairStage(
                "repair", applied,
                f"{tag}｜补丁应用={applied}，改动 {len(diff)} 字符，"
                f"验证 {len(verification)} 条（harness 自评不作为结论）", tag))

            if not applied:
                why = str((harness_section.get("patch_not_applied") or {}).get("reason")
                          or "")
                if scope.conflicts:
                    # **改不下去**且定位提示正指向被锁文件 → 只能撤销才能继续。
                    # 这是「撤销需人工确认」唯一真正需要发生的场合：
                    # 只要本轮还有别的板块可改，就不该来打扰人。
                    return self._ask_undo(
                        stages, rounds, scope, tag, round_no, case_count,
                        defect_count, started_at, batch, fp,
                        undone=undone,
                        patch=patch, rationale=rationale,
                        verification=verification, why=why)
                rounds.append(self._close_round(
                    round_id=tag, started_at=started_at, batch=batch, fp=fp,
                    scope=scope, patch=_patch_view(patch or {}),
                    rationale=rationale,
                    verification=verification, stopped="no_patch",
                    handoff=why or "harness 未产出可用补丁"))
                return self._finish(
                    stages, rounds=rounds, undone=tuple(undone),
                    case_count=case_count,
                    defect_count=defect_count, repair=repair,
                    rounds_run=round_no, requires_manual=True,
                    reason=(f"harness 未产出可用补丁，没有改动就没有「改动效果」可评：{why}"
                            if why else "harness 未产出可用补丁，无改动可评"))

            # ---------------- 记账：已提交补丁进版本账 ----------------
            note = self._record_commit(tag, patch, diff, rationale)

            # ---------------- 第三段：复评 ----------------
            self._focus_on(diff)
            state.update(await self.evaluator.recheck(state))
            rsection = _section(state, "eval_platform_recheck")
            verdict = str(rsection.get("verdict") or "")
            accepted = bool(rsection.get("accepted"))
            why = str(rsection.get("reason") or "")
            carried = tuple(_reports(rsection))
            after_batch = (defects_from_reports(list(carried)) if carried
                           else DefectBatch())
            after_fp = fingerprint_of(after_batch.defects)
            fp_diff = after_fp.diff_against(fp)

            stages.append(RepairStage("recheck", accepted,
                                      f"{tag}｜{verdict}：{why}", tag))
            recheck = {k: rsection.get(k) for k in
                       ("verdict", "fixed", "regressed", "still_broken",
                        "before_defects", "after_defects", "new_kinds",
                        "remaining", "errored")
                       if k in rsection}
            rounds.append(self._close_round(
                round_id=tag, started_at=started_at, batch=batch, fp=fp,
                scope=scope, patch={**_patch_view(patch or {}), "ledger": note},
                rationale=rationale, verification=verification,
                verdict=verdict, verdict_reason=why,
                resolved=fp_diff.resolved, new_defects=fp_diff.appeared,
                fp_diff=fp_diff.as_dict(), fp_after=after_fp.stable,
                stopped="" if accepted else "redone",
                handoff=""))

            if accepted:
                return self._finish(
                    stages, rounds=rounds, undone=tuple(undone),
                    case_count=case_count, defect_count=defect_count,
                    repair=repair, recheck=recheck, verdict=verdict,
                    accepted=True, rounds_run=round_no, reason=why)

            # ---- 以下都是「不能/不该继续」的四种情形，各有其名 ----
            stop = self._stop_reason(verdict, fp_diff, carried, round_no,
                                     total_rounds)
            if stop is not None:
                return await self._finish_stop(
                    stop, stages, rounds, undone, rsection, repair, recheck,
                    verdict, why, round_no, case_count, defect_count, tag)

            prev_fp = fp

        # 轮次用完：**不叫失败，也不叫成功**——它是一个需要人接手的中间态
        return self._finish(
            stages, rounds=rounds, undone=tuple(undone),
            case_count=case_count, defect_count=defect_count,
            repair=repair, recheck=recheck, verdict=verdict,
            rounds_run=total_rounds, requires_manual=True,
            reason=(f"已跑满 {total_rounds} 轮仍未达标。继续叠加会让改动无法归因，"
                    f"转人工重新定位（每轮都改过什么见 rounds 与 explain()）"))

    # ------------------------------------------------------------------ #
    # 停止判定：四种情形，退回值的第一个元素是原因码
    # ------------------------------------------------------------------ #
    def _stop_reason(self, verdict: str, fp_diff: Any,
                     carried: Sequence[PlatformReport], round_no: int,
                     total_rounds: int) -> tuple[str, str] | None:
        if verdict == RecheckVerdict.UNVERIFIED.value:
            return ("unverified",
                    "复评无法判定（没有可用的重跑能力或全部用例报错）。"
                    "在「不知道有没有效果」的状态下继续叠加改动，"
                    "等于把未经验证的基线当成已验证的")
        if verdict == RecheckVerdict.REJECTED_AUDIT.value:
            return ("rejected_audit",
                    "改动审计未通过：这是「胡乱修改」，不能靠再改一轮盖过去，"
                    "必须人工看这份 diff 并决定是否撤销")
        if verdict == RecheckVerdict.REJECTED_REGRESSION.value:
            return ("rejected_regression",
                    "改坏了原本通过的用例。本轮补丁应先撤销再重来——"
                    "在已改坏的基线上继续叠加，会把新问题埋在旧问题里")
        if fp_diff.identical:
            # 用户明确要求的那一条：**跨轮重试必须查验错误信息已更改**
            return ("no_progress",
                    f"错误信息一字未变（{fp_diff.after_count} 条缺陷原样保留）："
                    f"本轮改动没有推进任何一条。再跑一轮只会重复同一个动作，"
                    f"代价却是又一次模型调用 + 一次全量评测")
        if not carried:
            return ("no_baseline",
                    "复评没有产出新的基线报告，无法构成下一轮的输入。"
                    "没有基线就没有「还剩什么没修」，硬晋升会把「没验」变成「全好了」")
        if round_no >= total_rounds:
            return ("rounds_exhausted",
                    f"已跑满 {total_rounds} 轮仍未达标。继续叠加会让改动无法归因，"
                    f"转人工重新定位")
        return None

    async def _finish_stop(self, stop: tuple[str, str], stages: list[RepairStage],
                           rounds: list[Mapping[str, Any]],
                           undone: list[Mapping[str, Any]],
                           rsection: Mapping[str, Any], repair: Mapping[str, Any],
                           recheck: Mapping[str, Any], verdict: str, why: str,
                           round_no: int, case_count: int,
                           defect_count: int, tag: str) -> RepairOutcome:
        code, message = stop
        stages.append(RepairStage("stop", False, f"{tag}｜{code}：{message}", tag))
        # 「改坏了 / 乱改」的下一步是撤销本轮：**申请票**，让人批。
        request: dict[str, Any] = {}
        if code in ("rejected_regression", "rejected_audit"):
            request = self._request_undo([tag], reason=f"{tag} {code}：{why}")
        return self._finish(
            stages, rounds=rounds, undone=tuple(undone),
            case_count=case_count, defect_count=defect_count,
            repair=repair, recheck=recheck, verdict=verdict,
            rounds_run=round_no, requires_manual=True,
            requires_approval=bool(request), approval_request=request,
            reason=f"[{code}] {message}（本轮复评：{why}）")

    # ------------------------------------------------------------------ #
    # 撤销（高危，必须有票）
    # ------------------------------------------------------------------ #
    def _ask_undo(self, stages: list[RepairStage],
                  rounds: list[Mapping[str, Any]], scope: RoundScope,
                  tag: str, round_no: int, case_count: int,
                  defect_count: int,
                  started_at: str, batch: DefectBatch,
                  fp: DefectFingerprint, *,
                  undone: Sequence[Mapping[str, Any]] = (),
                  patch: Mapping[str, Any] | None = None,
                  rationale: str = "", verification: Sequence[str] = (),
                  why: str = "") -> RepairOutcome:
        """改不下去、又必须动被锁文件 → 申请撤销票并停下。

        **只有到这一步才打扰人**：只要本轮别的板块还能改，就不该来要审批。
        这也是「边界」与「撤销审批」两件事的分界——前者是自动化自己能守的，
        后者必须有人签字。

        `undone` 是**本次运行前面几轮已经撤掉的**：从这里提前返回时也要带上，
        否则「撤销生效了」这件事会随早退一起消失，人只能看到文件变了却查不到
        是谁撤的。`tag` 与 `round_no` 分开收：前者是账本上的唯一编号（跨次
        运行续接），后者是本次运行跑了几轮。
        """
        request = self._request_undo(list(scope.undo_rounds),
                                     reason=(f"{list(scope.conflicts)} 已被 "
                                             f"{list(scope.undo_rounds)} 改动过，"
                                             f"要修必须先撤销。harness 未能绕开：{why}"))
        detail = (f"本轮定位提示指向 {list(scope.conflicts)}，"
                  f"已被 {list(scope.undo_rounds)} 的补丁改动过；"
                  f"harness {'未能产出可用补丁' if why else '无法绕开这些文件'}")
        if not request:
            # 没有审批服务：不给捷径，也不是「撤销成功」
            stages.append(RepairStage("boundary", False,
                                      f"{tag}｜{detail}；未接入审批服务，无法撤销", tag))
            rounds.append(self._close_round(
                round_id=tag, started_at=started_at, batch=batch, fp=fp,
                scope=scope, patch=_patch_view(patch), rationale=rationale,
                verification=verification, stopped="undo_needs_approval",
                handoff="撤销已提交补丁需要人工确认，但未接入审批服务"))
            return self._finish(
                stages, rounds=rounds, undone=tuple(undone),
                case_count=case_count,
                defect_count=defect_count, rounds_run=round_no,
                requires_manual=True,
                reason=(f"[undo_needs_approval] {detail}。撤销已提交补丁是"
                        f"人工确认动作（U81），未接入审批服务时不执行——"
                        f"无票撤销与无审批改代码是同一类问题"))
        stages.append(RepairStage(
            "boundary", True,
            f"{tag}｜{detail}；已申请撤销票 {request.get('approval_id')}，"
            f"等待人工批准", tag))
        rounds.append(self._close_round(
            round_id=tag, started_at=started_at, batch=batch, fp=fp,
            scope=scope, patch=_patch_view(patch), rationale=rationale,
            verification=verification, stopped="undo_needs_approval",
            handoff=f"等待审批票 {request.get('approval_id')}"))
        return self._finish(
            stages, rounds=rounds, undone=tuple(undone),
            case_count=case_count,
            defect_count=defect_count, rounds_run=round_no,
            requires_manual=True, requires_approval=True,
            approval_request=request,
            reason=(f"[undo_needs_approval] {detail}。撤销已提交补丁需人工确认："
                    f"批准后把 approval_id 与 round_ids={list(scope.undo_rounds)} "
                    f"带回重跑"))

    def _request_undo(self, round_ids: Sequence[str],
                      *, reason: str) -> dict[str, Any]:
        """申请一张撤销票。**没有审批服务就返回空**（不是「已申请」）。"""
        if self.approvals is None or not round_ids:
            return {}
        ticket = self.approvals.request(
            tool=UNDO_TOOL, signature=undo_signature(round_ids),
            agent=self.approval_agent, role=self.approval_role,
            tier=2, reason=reason)
        return {"approval_id": ticket.approval_id, "tool": ticket.tool,
                "round_ids": list(round_ids), "reason": reason,
                "expires_at": ticket.expires_at,
                "signature": ticket.signature}

    def _apply_undo(self, round_ids: Sequence[str], scope: RoundScope, *,
                    approval_id: str) -> tuple[tuple[Any, ...], str]:
        """按票撤销。**先全部成功再返回**，任何一份失败都不继续。

        返回 `(撤销凭证, 失败原因)`。失败原因为空字符串表示全部成功。
        """
        if self.ledger is None:
            return (), "没有版本账（ledger），无从撤销"
        if not round_ids:
            return (), "没有指定要撤销的轮次"
        done: list[Any] = []
        for round_id in round_ids:
            try:
                done.append(self.ledger.undo(
                    round_id, approval_id=approval_id, service=self.approvals,
                    requester=self.approval_agent, role=self.approval_role,
                    reason=f"为解除 {list(scope.conflicts) or '已改坏的基线'} 的跨轮锁定"))
            except (RevisionError, ApprovalError) as error:
                return tuple(done), f"{type(error).__name__}: {error}"
        return tuple(done), ""

    # ------------------------------------------------------------------ #
    # 记账 / 基线 / 边界
    # ------------------------------------------------------------------ #
    def _record_commit(self, round_id: str, patch: Mapping[str, Any],
                       diff: str, rationale: str) -> dict[str, Any]:
        """把本轮提交的补丁记进版本账。返回记账摘要（空 = 没记账）。"""
        committed = patch.get("committed") if isinstance(patch, Mapping) else None
        if self.ledger is None or not isinstance(committed, Mapping):
            return {}
        before = committed.get("before")
        record = CommittedPatch(
            round_id=round_id,
            change_id=f"round:{round_id}",
            diff=diff,
            paths=tuple(str(p) for p in (committed.get("paths") or ())),
            before=dict(before) if isinstance(before, Mapping) else {},
            defect_ids=tuple(str(d) for d in (committed.get("defect_ids") or ())),
            rationale=str(committed.get("rationale") or rationale))
        try:
            return self.ledger.record(record).as_dict()
        except RevisionError:
            # 同一轮重复记账（驱动被重入）：如实跳过，不抛。
            # 抛出去会让「跑完一轮但记账失败」变成整次运行失败，
            # 而补丁其实已经落盘——那时更需要的是继续跑完并让人知道。
            return {"skipped": "本轮已记账，重复登记被忽略"}

    def _scope(self, round_id: str, batch: DefectBatch) -> RoundScope:
        """算本轮边界。**只看可自动处置的缺陷**：人工处置的不构成改动范围。"""
        allowed: list[str] = []
        targets: list[str] = []
        for defect in batch.auto_repairable():
            for prefix in ALLOWED_SCOPE_BY_KIND.get(defect.kind.value, ()):
                if prefix not in allowed:
                    allowed.append(prefix)
            for path in files_from_hint(defect.target_hint):
                if path not in targets:
                    targets.append(path)

        locked = self.ledger.locked_paths() if self.ledger is not None else ()
        # 全局禁区不进 conflicts：它们**永远**不可改，不是「等撤销就会解锁」。
        conflicts = tuple(sorted(
            p for p in targets
            if p in locked
            and not any(p == f.rstrip("/") or p.startswith(f)
                        for f in FORBIDDEN_PATHS)))
        undo_rounds = (self.ledger.undo_rounds_for(conflicts)
                       if (self.ledger is not None and conflicts) else ())
        return RoundScope(round_id=round_id, allowed_prefixes=tuple(allowed),
                          locked_paths=tuple(locked), conflicts=conflicts,
                          undo_rounds=undo_rounds)

    def _promote(self, reports: Sequence[PlatformReport],
                 case_count: int) -> dict[str, Any]:
        """把上一轮复评的报告晋升为本轮基线。

        用 `EvaluationPlatformAgent.section_from_reports` 而不是自己拼：
        两处拼同一段结构，迟早分叉（E-44）。
        """
        builder = getattr(self.evaluator, "section_from_reports", None)
        if builder is None:
            raise TypeError(
                "evaluator 缺少 section_from_reports，无法把复评报告晋升为基线："
                "替身实现必须提供它，否则多轮的基线没有唯一形态")
        return builder(uid=self.user_id, reports=tuple(reports),
                       case_count=case_count)

    def _close_round(self, *, round_id: str, started_at: str,
                     batch: DefectBatch, fp: DefectFingerprint,
                     scope: RoundScope | None, patch: Mapping[str, Any],
                     rationale: str, verification: Sequence[str],
                     verdict: str = "", verdict_reason: str = "",
                     resolved: Sequence[str] = (),
                     new_defects: Sequence[str] = (),
                     fp_diff: Mapping[str, Any] | None = None,
                     fp_after: str = "", stopped: str = "",
                     handoff: str = "") -> Mapping[str, Any]:
        """落一条轮次记录，返回它的摘要（全文在 trail 里，不进 outcome）。"""
        record = RoundRecord(
            round_id=round_id, started_at=started_at,
            defects=tuple(d.as_dict() for d in batch.defects),
            fingerprint=fp.stable, fingerprint_detail=fp.as_dict(),
            scope=scope.as_dict() if scope is not None else {},
            patch=_patch_view(patch),
            rationale=rationale, verification=tuple(verification),
            verdict=verdict, verdict_reason=verdict_reason,
            resolved=tuple(resolved), new_defects=tuple(new_defects),
            fingerprint_after=fp_after, fingerprint_diff=dict(fp_diff or {}),
            stopped=stopped, handoff=handoff)
        safe = self._trail().append(record)
        return {"round_id": safe.round_id, "defect_count": len(safe.defects),
                "patch_paths": list((safe.patch or {}).get("paths") or ()),
                "applied": bool((safe.patch or {}).get("applied")),
                "verdict": safe.verdict, "moved": safe.moved,
                "stopped": safe.stopped, "handoff": safe.handoff,
                "error_change": (safe.fingerprint_diff or {}).get("summary", "")}

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

    def _finish(self, stages: Sequence[RepairStage],
                **kwargs: Any) -> RepairOutcome:
        return RepairOutcome(user_id=self.user_id, stages=tuple(stages), **kwargs)

    def _outcome(self, stages: Sequence[RepairStage],
                 **kwargs: Any) -> RepairOutcome:
        """旧名保留：测试与既有调用方用它读结果。"""
        return self._finish(stages, **kwargs)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _section(state: Mapping[str, Any], key: str) -> dict[str, Any]:
    raw = state.get(key)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _patch_view(patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """溯源账里的补丁视图：**不含源码正文**（`committed.before`）。

    `before` 只在撤销时需要，由版本账（`harness/revision.py`）单独持有。
    溯源账是给人反复读的，塞进整份源码只会让人找不到重点，
    而且它会把账本体积放大到无法按轮翻阅。

    同时把 `paths` / `touched` 补齐：账要回答「改了哪些文件、改了几行」，
    这两个键必须有唯一来源。生产端（`agents/p10_harness.py`）给的是**成功
    应用结果的并集**；只给 `committed` 的替身在这里补齐，免得每个读账的
    地方各自兜底一次（E-44 的老毛病）。两处都缺就留空——没有凭据时
    **不猜文件名**，账宁可少写，不能编。
    """
    view = dict(patch or {})
    committed = view.get("committed")
    if isinstance(committed, Mapping):
        view["committed"] = {k: v for k, v in committed.items() if k != "before"}
        if not view.get("paths"):
            view["paths"] = list(committed.get("paths") or ())
        if not view.get("touched"):
            view["touched"] = int(committed.get("touched") or 0)
    return view


def _batch(section: Mapping[str, Any]) -> DefectBatch:
    """从 `eval_platform` 段还原缺陷批次。

    **形态不对就抛**，不吞成空批：空批会让「数据坏了」看起来像「全部通过」，
    而这条链路后面紧接着就是「自动改代码」。
    """
    raw = section.get("defects")
    if not isinstance(raw, Mapping):
        return DefectBatch()
    return batch_from_dict(raw)


def _reports(section: Mapping[str, Any]) -> list[PlatformReport]:
    """复评段里的 after 报告。**没有就返回空**——空表示「没有新基线」，
    与本轮「全好了」是两件事，调用方必须分开处理。"""
    raw = section.get("reports")
    if not isinstance(raw, (list, tuple)):
        return []
    return [PlatformReport.from_dict(dict(r)) for r in raw
            if isinstance(r, Mapping)]
