"""离线闭环的排程单元（U85）。

计划书依据
----------
- 三、P10 触发时机「**7×24 小时后台离线自动运行**」

为什么不是「写一个定时器」
--------------------------
计划书只写到「7×24 后台离线自动运行」，**没有定义调度语义**：是 cron 表达式、
固定间隔，还是事件触发？按项目铁律，未定义项不自行发明（登记决策台账）。
在库里起一个后台线程做轮询，是最容易写、也最容易出问题的做法：

- 线程的生命周期没有宿主管理，测试里要专门清理，进程退出时可能卡住；
- 两套调度（宿主一套、库一套）同时跑，同一批用例会被评两遍，
  而第二遍读的是第一遍改完的代码——同一时刻的两个结论互相矛盾；
- 「上次什么时候跑的」存在内存里，重启就丢，等于每次启动都立刻跑一遍。

所以本模块**只提供调度单元，不提供调度器**：

| 谁 | 做什么 |
|---|---|
| 外部调度器（宿主的 cron / 容器编排 / 运维平台） | 周期性调用 `due(now)`，为真就 `run()` |
| `RepairJob`（本模块） | 记住上次跑到哪、现在该不该跑、跑出了什么 |

这三件事看起来琐碎，但每一件都容易写错，尤其是最后一件：跑完把结论
覆盖到上一次上，于是「上一轮改坏了」这件事在监控里从此看不见。

三条纪律
--------
1. **失败也算跑过**（`last_run_at` 照样推进）。不推进会导致调度器每次
   轮询都重试，一个坏掉的依赖会变成每分钟一次的模型调用与全量评测。
   失败信息进 `last_error`，不吞。
2. **不允许重入**。同一个 job 被两个调度器同时驱动时，第二次直接抛错：
   两次并发运行会同时改同一批文件，而版本账只认「一轮一次提交」。
3. **时钟回拨不装死**。`now` 早于上次运行时间（NTP 校正、容器迁移）时
   判为 due：宁可多跑一次，也不要因为时间回拨永久停摆。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from ai_butler.agents.repair_loop import RepairLoop, RepairOutcome

__all__ = ["RepairJob", "build_repair_job"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RepairJob:
    """一次离线闭环运行的调度单元。**本进程不起线程。**

    `cases` 是 State 形态的用例 dict；`traces` 通常不给——首轮痕迹由
    `loop.source`（U84）从会话账本取，这正是「离线运行时读已经发生过的会话」
    的意思。给了 `traces` 就按调用方给的来（回溯重放某一天的会话时用得上）。
    """

    loop: RepairLoop
    #: 两次运行之间的最小间隔（秒）。必须为正：
    #: 0 或负数会让 `due()` 永远为真，等于自己实现了一个忙等循环。
    interval_seconds: float
    cases: tuple[Mapping[str, Any], ...] = ()
    traces: tuple[Mapping[str, Any], ...] = ()
    #: 时钟可注入（测试与「按业务时区判定」的宿主）。
    clock: Callable[[], datetime] = _utcnow
    #: 上一轮需要人工处理的结论（审批票 / 边界冲突）。调度器可以据此告警，
    #: 但**不该自动批准**——那会把人工确认变成形式。
    last_outcome: RepairOutcome | None = None
    last_run_at: datetime | None = None
    last_error: str = ""
    runs: int = 0
    _running: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds 必须为正，实际 {self.interval_seconds}："
                f"0 或负数会让「是否该跑」永远为真")
        if not self.cases:
            # 没有用例时闭环会停在 `no_cases`——那是正确的，但让它作为
            # **构造期错误**更早暴露：一个永远跑不出结论的定时任务
            # 在监控上看起来像「一直正常」。
            raise ValueError("RepairJob 没有用例：空任务不会产出任何结论")

    # ------------------------------------------------------------------ #
    def due(self, now: datetime | None = None) -> bool:
        """现在该不该跑。**没跑过就为真**（首轮不该等一个间隔）。"""
        moment = self._moment(now)
        if self.last_run_at is None:
            return True
        if moment < self.last_run_at:
            # 时钟回拨：判为 due 而不是「再等一个间隔」。
            # 装死的代价是永久停摆，而多跑一次的代价是一次多余的评测。
            return True
        return moment - self.last_run_at >= timedelta(seconds=self.interval_seconds)

    async def run(self, *, now: datetime | None = None,
                  undo_approval: str = "",
                  undo_rounds: Sequence[str] = ()) -> RepairOutcome:
        """跑一次。**不论成败都推进 `last_run_at`。**

        `undo_approval` / `undo_rounds` 透传给闭环：人工批准的撤销票
        要在下一次运行里用掉（票一次性，所以只能传一次）。
        """
        if self._running:
            raise RuntimeError(
                "这个 RepairJob 正在运行：两名调度器同时驱动会并发改同一批文件，"
                "而版本账只认「一轮一次提交」——并发下两份补丁会互相覆盖")
        self._running = True
        try:
            outcome = await self.loop.run(
                cases=list(self.cases), traces=list(self.traces),
                undo_approval=undo_approval, undo_rounds=undo_rounds)
        except Exception as exc:            # noqa: BLE001 —— 失败也要记时间戳
            self.last_run_at = self._moment(now)
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.runs += 1
            raise
        else:
            self.last_run_at = self._moment(now)
            self.last_error = ""
            self.last_outcome = outcome
            self.runs += 1
            return outcome
        finally:
            self._running = False

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        """给监控用的可 JSON 化状态。**不含源码、不含用例原文**。"""
        outcome = self.last_outcome
        return {
            "runs": self.runs,
            "interval_seconds": self.interval_seconds,
            "last_run_at": (self.last_run_at.isoformat()
                            if self.last_run_at else None),
            "last_error": self.last_error,
            "last": (None if outcome is None else {
                "verdict": outcome.verdict or None,
                "clean": outcome.clean,
                "requires_manual": outcome.requires_manual,
                "requires_approval": outcome.requires_approval,
                "rounds_run": outcome.rounds_run,
                "defect_count": outcome.defect_count,
                "reason": outcome.reason}),
        }

    def _moment(self, now: datetime | None) -> datetime:
        moment = now if now is not None else self.clock()
        if moment.tzinfo is None:
            # naive 时间按 UTC 解释：两种时间混着比大小会算错，
            # 而算错的方向恰好是「少跑」或「多跑」，都不会报错。
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)


def build_repair_job(loop: RepairLoop, *, interval_seconds: float = 24 * 3600,
                     cases: Sequence[Mapping[str, Any]],
                     traces: Sequence[Mapping[str, Any]] = (),
                     clock: Callable[[], datetime] | None = None) -> RepairJob:
    """装配一个调度单元。**默认间隔 1 天**（实现参数，计划书未给刻度）。

    取一天的理由：这条链路一次运行会跑完「评测 → 模型改代码 → 重跑评测」，
    分钟级间隔会让它永远处在运行中，而两次运行之间根本没有新的会话痕迹
    可评——「7×24 后台运行」不等于「不停运行」，是「不用人守着」。
    """
    return RepairJob(loop=loop, interval_seconds=interval_seconds,
                     cases=tuple(dict(c) for c in cases),
                     traces=tuple(dict(t) for t in traces),
                     clock=clock or _utcnow)
