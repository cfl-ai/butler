"""Agent 独立运行入口 —— 脱离会话也能单独跑一个阶段 Agent。

计划书依据
----------
- 三、P4「逐个开发、逐个单测、逐个上线；所有子 Agent 只读全局状态、仅写入自有字段」
- 三、P1「数据中台｜全系统唯一可信数据源」—— 结构化存储、增量学习、冲突校验
- 三、P3 必填 State 契约（user_id / session_id / login_status / jev_decision_result / wiki_digest）

裁定
----
计划书要求「逐个可测可上线」，但未规定**脱离会话**的独立调用入口形态。
本入口的形态为裁定 **U48**（来源与复用边界见 `core/decisions.py`）。

**「可独立跑动」不等于「可绕过风控」**
--------------------------------------
这是本模块唯一要守住的边界。独立入口省掉的是 **LangGraph 会话编排**，
**不是**任何一道校验：

- ✅ 省掉：图节点调度、意图路由、多 Agent 编排
- ❌ 不省：user_id 必填、State 契约、写作用域、三层权限、治理判定、审计

具体说，数据中台单独加数据时，低置信的事实**照旧进 review 队列**
（`data_platform.governance` 的 REVIEW 分支），不会因为「是独立调用」就直接写库 ——
「会话中根据实际情况调整画像，但不会瞎存」这句话，靠的就是这条不旁路。

用途举例
--------
- 后台批量导入画像数据
- 定时脚本单独调某个阶段做增量
- 单测 / 冒烟里直接跑一个 Agent，不必搭整条会话链路
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ai_butler.agents.state import REQUIRED_STATE_FIELDS, validate_state
from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    PermissionRequest,
    UserRole,
    evaluate,
)


class StandaloneError(RuntimeError):
    """独立入口的执行前校验失败。**一律抛出**，不做静默降级。"""


@dataclass(frozen=True)
class StandaloneContext:
    """一次独立调用的上下文。

    与会话态的区别：会话里这些值由 LangGraph 从 State 取，
    独立调用**没有 Graph**，所以必须由调用方**显式给出** —— 尤其是 user_id。
    """

    user_id: str
    role: UserRole = UserRole.MEMBER
    #: 本次调用涉及的数据密级
    sensitivity: DataSensitivity = DataSensitivity.L2_INTERNAL
    #: 本次调用所需的能力级（写数据 = L1，调外部工具 = L2）
    required: CapabilityLevel = CapabilityLevel.L1_WRITE_OWN
    #: session_id 在独立调用里也要有：审计要能追溯这批操作属于哪一次
    session_id: str = ""
    wiki_digest: Any = None
    jev_decision_result: Any = None

    def __post_init__(self) -> None:
        if not self.user_id or not str(self.user_id).strip():
            raise StandaloneError(
                "独立调用必须显式携带 user_id —— "
                "没有用户上下文的写入无法做行级隔离（计划书三、P0-1）"
            )
        if not self.session_id or not str(self.session_id).strip():
            raise StandaloneError(
                "独立调用必须显式携带 session_id —— 否则审计无法追溯这批操作属于哪一次"
            )


@dataclass(frozen=True)
class StandaloneResult:
    """一次独立调用的结果。**带审计字段**，不是只返回业务数据。"""

    agent_key: str
    user_id: str
    session_id: str
    #: Agent 返回的字段更新（已受 writes 约束）
    updates: dict[str, Any]
    elapsed_ms: int
    #: 实际生效的审批档。Tier1/Tier2 的调用**必须留下待审批标记**
    approval_tier: int
    #: 是否触碰了需要人工确认的动作
    needs_approval: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"agent_key": self.agent_key, "user_id": self.user_id,
                "session_id": self.session_id, "elapsed_ms": self.elapsed_ms,
                "approval_tier": self.approval_tier,
                "needs_approval": self.needs_approval,
                "updates": dict(self.updates)}


def build_state(ctx: StandaloneContext, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """按 P3 必填契约构造一份最小合法 State。

    独立调用没有 LangGraph，但 **State 契约照样要满足** ——
    否则 `SubAgent.__call__` 里的 `validate_state` 会把它挡下来，
    而这正是我们想要的：独立入口不得成为契约的旁路。
    """
    state: dict[str, Any] = {
        "user_id": ctx.user_id,
        "session_id": ctx.session_id,
        "login_status": True,          # 独立调用视为已鉴权，但鉴权在**入口**完成，不在这里
        "jev_decision_result": ctx.jev_decision_result,
        "wiki_digest": ctx.wiki_digest,
    }
    if extra:
        # 允许补充业务字段，但**不得覆盖**必填契约字段
        protected = set(REQUIRED_STATE_FIELDS)
        conflict = [k for k in extra if k in protected]
        if conflict:
            raise StandaloneError(
                f"extra 不得覆盖 State 必填字段 {conflict} —— "
                f"这些字段由本入口按契约填充，覆盖等于绕过隔离"
            )
        state.update(extra)
    validate_state(state)
    return state


def run_standalone(agent: Any, *, ctx: StandaloneContext,
                   extra: Mapping[str, Any] | None = None) -> StandaloneResult:
    """独立执行一个阶段 Agent。

    顺序是刻意的：**先验权限，再验 State，最后才跑业务逻辑**。
    把权限放在最前，是为了让「未授权」的调用连 State 都构造不出来。
    """
    agent_key = str(getattr(agent, "name", "") or type(agent).__name__)

    # ---- 1) 三层权限 ----
    verdict = evaluate(PermissionRequest(
        agent=AgentIdentity(key=agent_key, level=ctx.required),
        role=ctx.role, sensitivity=ctx.sensitivity, required=ctx.required))
    verdict.raise_if_denied(action=f"独立调用 {agent_key}")

    # ---- 2) State 契约 ----
    state = build_state(ctx, extra)

    # ---- 3) 能力绑定校验：声明了能力就必须有工具面 ----
    # 会话路径里 agent.tools 由 butler.enroll 装配；独立调用没有管家，
    # 若 Agent 声明了 capabilities 却拿不到工具面，必须在这里明确拒绝 ——
    # 否则 run() 里 `self.tools.require(...)` 会以 NoneType AttributeError 收场，
    # 那是「难看的崩溃」而不是「干净的拒绝」（U48：省编排不省校验）。
    # tools 已绑定但没发够能力的，由 AgentTools.require 自己抛 CapabilityError。
    declared = tuple(getattr(agent, "capabilities", ()) or ())
    if declared and getattr(agent, "tools", None) is None:
        raise StandaloneError(
            f"{agent_key} 声明了能力 {list(declared)}，但独立调用未提供任何能力实现"
            f"（tools 为 None）。请经 butler.enroll 装配后调用，或给实例显式绑定 "
            f"tools —— 声明了没接上，一律拒绝执行")

    # ---- 4) 业务逻辑（写作用域由 SubAgent.__call__ 内部守住）----
    started = time.perf_counter()
    updates = _drive(agent(state))
    elapsed = int((time.perf_counter() - started) * 1000)

    if not isinstance(updates, dict):
        # SubAgent.__call__ 对非 dict 返回值会抛 WriteScopeError；
        # 走到这里说明是鸭子类型的调用方，同样不许静默吞掉。
        raise StandaloneError(
            f"{agent_key} 执行结果不是 dict（实际 {type(updates).__name__}），"
            f"无法作为 State 更新")

    return StandaloneResult(
        agent_key=agent_key,
        user_id=ctx.user_id,
        session_id=ctx.session_id,
        updates=updates,
        elapsed_ms=elapsed,
        approval_tier=int(verdict.tier),
        needs_approval=int(verdict.tier) >= 1,
    )


def _drive(outcome: Any) -> Any:
    """把 Agent 的执行结果跑到完：同步给值，异步给**跑完的协程结果**。

    为什么必须有这个函数
    -------------------
    7 个 Agent 的 `run()` 是 async 的（P1/P5/P6/P8/P9/P10 + P4 集群全体成员）。
    `agent(state)` 对异步 run 只返回**协程对象**——创建了不 await，
    它就是一个从未执行的对象：`updates` 静默变成 {}，调用方却看到
    「已独立执行 xxx（0 ms）」。**这是最坏的失败形态：伪装成功**
    （E-21 同族：声明了没接上，还报告成功）。

    事件循环两种处境都覆盖：
    - 无活动循环（CLI / 同步测试）：asyncio.run 直接起一个；
    - 调用方已在循环里（异步框架、async 测试）：换工作线程起新循环，
      不劫持调用方自己的循环（本入口创建的连接也只在本循环内使用）。
    """
    if not inspect.isawaitable(outcome):
        return outcome
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(outcome)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, outcome).result()


@dataclass
class StandaloneAudit:
    """独立调用的审计记录。

    与会话链路的审计**合并统计**：不能因为「是后台脚本调的」就不留痕 ——
    恰恰相反，脱离了会话编排的操作更需要能追溯谁在什么时候改了什么。
    """

    records: list[StandaloneResult] = field(default_factory=list)

    def record(self, result: StandaloneResult) -> None:
        self.records.append(result)

    def pending_approvals(self) -> tuple[StandaloneResult, ...]:
        """需要人工确认但尚未确认的调用。"""
        return tuple(r for r in self.records if r.needs_approval)

    def by_user(self, user_id: str) -> tuple[StandaloneResult, ...]:
        return tuple(r for r in self.records if r.user_id == user_id)
