"""P3 场景调度 Agent —— LangGraph 主状态机。

计划书依据
----------
- 三、P3「基于 **LangGraph 搭建主状态机**，管理全流程状态流转」
- 三、P3「开发全局状态监听机制；**会话失效直接终止 Graph 流程，返回重新登录**」
- 三、P3「继承全部 Agent 回退约束：**全局最大回退次数 = 2 次**，
  **禁止把任务再次分发给刚刚回退的 Agent**，杜绝循环甩锅」
- 三、P3「开发多子 Agent 自动分发路由；下发任务携带 user_id、wiki_digest」
- 三、P3 触发时机「用户发起任意请求触发主状态机；**路由结果决定后续 Agent 调用链**」
- 三、P3「全链路降级与回滚机制：任一环节失败自动降级，不阻断主流程」
- 三、P3「全链路可观测：Trace ID 贯穿、关键节点埋点、耗时统计」
- 三、P4「所有子 Agent 只读全局状态、仅写入自有字段、无跨模块越权调用」
- 三、P4「会话失效则跳过执行」
- 三、P4「Agent 之间不得直接互相传递任务，必须走场景调度 Agent 路由层」

状态机结构
----------
```text
START → route ─┬─(必须澄清)─→ clarify ──→ END
               ├─(已定意图)─→ dispatch → aggregate → END
               └─(无法识别)─→ terminate → END
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from typing import TypedDict

from ai_butler.agents.base import SubAgent, resolve
from ai_butler.agents.state import (
    REQUIRED_STATE_FIELDS,
    GraphState,
    new_state,
    validate_state,
)
from ai_butler.core.event_journal import EventJournal, journal_observer
from ai_butler.scheduler.observe import Observer, Span, Trace, trace_scope
from ai_butler.scheduler.router import IntentRouter, RouteRequest
from ai_butler.core.input_guard import InputGuardError
from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    ToolPermission,
    UserRole,
)
from ai_butler.scheduler.throttle import ResourceThrottle
from ai_butler.scheduler.tools import ToolDispatcher, ToolRegistry
from ai_butler.scheduler.vertical import VerticalDispatcher

try:                                            # LangGraph 0.2+ 提供 START 常量
    from langgraph.graph import END, START, StateGraph
except ImportError:                             # pragma: no cover
    from langgraph.graph import END, StateGraph  # type: ignore[assignment]
    START = "__start__"                          # type: ignore[assignment]

#: 主状态机节点（计划书三、P3 全流程状态流转）
NODE_GUARD = "guard"                     # 全局状态监听：会话有效性闸门
NODE_RELOGIN = "require_relogin"         # 会话失效：终止流程并返回重新登录
NODE_ROUTE = "route"
NODE_CLARIFY = "clarify"
NODE_DISPATCH = "dispatch"
NODE_AGGREGATE = "aggregate"
NODE_TERMINATE = "terminate"
NODES: tuple[str, ...] = (NODE_GUARD, NODE_RELOGIN, NODE_ROUTE, NODE_CLARIFY,
                          NODE_DISPATCH, NODE_AGGREGATE, NODE_TERMINATE)

#: 计划书三、P3 写死的数字：全局最大回退次数 = 2 次
MAX_ROLLBACKS = 2

#: 会话失效时的终止码（移动端据此弹窗引导重新登录，见三、P7）
LOGIN_REQUIRED = "LOGIN_REQUIRED"

#: 子 Agent 绝对不得覆盖的字段（计划书三、P4「只读全局状态」）
#: 受保护字段 = 计划书点名的必填字段 + 风控结论。
#: 
#: input_untrusted 必须受保护：若允许子 Agent 改写，就能把「不可信」洗成「可信」，
#: 台账 U41「不得沉淀为事实」这条约束随即失效。
PROTECTED_STATE_FIELDS: tuple[str, ...] = REQUIRED_STATE_FIELDS + ("input_untrusted",)

BRANCH_CLARIFY = "clarify"
BRANCH_DISPATCH = "dispatch"
BRANCH_TERMINATE = "terminate"
BRANCH_RELOGIN = "relogin"
BRANCH_CONTINUE = "continue"


class SchedulerState(GraphState, total=False):
    """主状态机的 State schema。

    **必须是 TypedDict**：LangGraph 按 schema 里的键建立通道并做按键合并；
    若直接用 `StateGraph(dict)`，节点返回的部分更新会**整体替换** state，
    上游节点（如 route）的结果根本传不到下游节点。
    这些流转字段是调度层管道，故不塞进 agents.state 的全局契约。
    """

    labels: tuple[str, ...]
    trace_id: str
    prev_layer: str
    dispatched: list[str]
    skipped: list[str]
    failed: list[str]
    merged: dict[str, Any]
    clarifying_question: str
    clarification_category: str
    #: 已回退过的 Agent：禁止把任务再次分发给它（防循环甩锅）
    rolled_back: list[str]
    #: 会话失效时的终止码
    login_required: str
    #: 输入接通层结论（U114）。**未启用时不写入** —— 空壳 dict 与「没跑过」
    #: 在下游看不出区别，那正是「声明了没接上」的形状（E-86）。
    intake: dict[str, Any]


class SchedulerError(RuntimeError):
    pass


@dataclass(frozen=True)
class DispatchPlan:
    """意图 → 子 Agent 调用链。计划书三、P3「路由结果决定后续 Agent 调用链」。"""

    chains: Mapping[str, Sequence[str]]

    def agents_for(self, intent: str | None) -> tuple[str, ...]:
        if not intent:
            return ()
        return tuple(self.chains.get(intent, ()))


@dataclass
class SchedulerResult:
    trace_id: str
    user_id: str
    session_id: str
    intent: str | None = None
    layer: str = ""
    confidence: float = 0.0
    require_clarification: bool = False
    clarifying_question: str = ""
    candidate_labels: tuple[str, ...] = ()
    dispatched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    agent_results: dict[str, Any] = field(default_factory=dict)
    rollback_count: int = 0
    rolled_back: list[str] = field(default_factory=list)
    login_required: str = ""
    degraded: bool = False
    degradation: list[str] = field(default_factory=list)
    #: 风控结论（台账 U41）：输入是否被标为不可信
    input_untrusted: bool = False
    #: 工具统一调度的统计（调用次数、重复拦截数、是否无增量）
    tool_calls: dict[str, Any] = field(default_factory=dict)
    spans: list[dict[str, Any]] = field(default_factory=list)
    #: 输入接通层结论（U114）。**开关关闭/未装网关时为空 dict** 表示这一层没跑；
    #: 判空必须用「有没有 units」，不要用「是不是 dict」—— 两者都能进 as_dict。
    intake: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return bool(self.intent) and not self.require_clarification

    @property
    def session_terminated(self) -> bool:
        """会话失效导致 Graph 被直接终止（三、P3），移动端据此引导重新登录。"""
        return self.login_required == LOGIN_REQUIRED

    def as_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "user_id": self.user_id,
                "session_id": self.session_id, "intent": self.intent, "layer": self.layer,
                "confidence": self.confidence,
                "require_clarification": self.require_clarification,
                "clarifying_question": self.clarifying_question,
                "candidate_labels": list(self.candidate_labels),
                "dispatched": list(self.dispatched), "skipped": list(self.skipped),
                "failed": list(self.failed), "rollback_count": self.rollback_count,
                "rolled_back": list(self.rolled_back),
                "login_required": self.login_required,
                "degraded": self.degraded, "degradation": list(self.degradation),
            "input_untrusted": self.input_untrusted,
                "tool_calls": dict(self.tool_calls),
                "agent_results": {k: v for k, v in self.agent_results.items()},
                "intake": dict(self.intake)}


class SceneScheduler:
    """场景调度 Agent：全流程总控，也是**唯一**的任务分发者。"""

    def __init__(self, *, router: IntentRouter, agents: Mapping[str, SubAgent],
                 plan: DispatchPlan, observer: Observer | None = None,
                 tool_registry: ToolRegistry | None = None,
                 throttle: ResourceThrottle | None = None,
                 journal: EventJournal | None = None,
                 verticals: VerticalDispatcher | None = None,
                 gateway: Any | None = None,
                 role: UserRole = UserRole.MEMBER) -> None:
        self.router = router
        self.agents = dict(agents)
        self.plan = plan
        #: 垂类派发器（U110）。**为 None 时垂类能力整体不存在** —— 不是「接了但没命中」，
        #: 而是这一轴根本没装，L4 即便给得出垂类键也没有执行者。默认 None，
        #: 因此既有调用方行为零变化；装配它的地方才有垂类能力。
        self.verticals = verticals
        #: 大模型网关（U112/U114）。**为 None 时输入接通层整体不存在**：
        #: 没有网关就没有 function calling 回路可跑，此时 `intake` 通道不写入，
        #: 而不是写个空壳冒充"处理过"。默认 None，既有调用方行为零变化。
        self.gateway = gateway
        self.observer = observer
        #: append-only 会话事件账本（U57）。传入即把本任务的 Trace 埋点
        #: 桥接落账（journal_observer 包装 observer 语义）；不传则维持
        #: 内存级 Trace 原行为——**没有账本时不冒充已持久化**。
        self.journal = journal
        #: 计划书三、P3「工具统一调度」：全系统唯一的工具入口
        self.dispatcher = ToolDispatcher(tool_registry or ToolRegistry(),
                                         throttle=throttle)
        #: 权限上下文（U43–U45）。角色缺省 MEMBER（日常使用）——
        #: 会话链路尚未携带角色（U42：/api/chat/stream 未实现），
        #: 在角色可达之前按最保守的已登录身份处理，不给「大概是个 admin」留默认值。
        #: 调度器自身按 L1 绑定：**编排者不直接触发进程外动作**，
        #: 需要 L2 及以上的调用方必须用自己的 AgentIdentity 求值
        #: （dispatch(..., agent=...)），U51 权限不可传递。
        self.role = role
        self.dispatcher.bind_permission(ToolPermission(
            agent=AgentIdentity(key="p3_scene_scheduler",
                                level=CapabilityLevel.L1_WRITE_OWN),
            role=role))
        missing = {n for chain in plan.chains.values() for n in chain} - set(self.agents)
        if missing:
            raise SchedulerError(f"分发计划引用了未注册的子 Agent：{sorted(missing)}")
        self._graph = self.build()

    # ------------------------------------------------------------------ #
    def build(self):
        graph = StateGraph(SchedulerState)
        graph.add_node(NODE_GUARD, self._node_guard)
        graph.add_node(NODE_RELOGIN, self._node_relogin)
        graph.add_node(NODE_ROUTE, self._node_route)
        graph.add_node(NODE_CLARIFY, self._node_clarify)
        graph.add_node(NODE_DISPATCH, self._node_dispatch)
        graph.add_node(NODE_AGGREGATE, self._node_aggregate)
        graph.add_node(NODE_TERMINATE, self._node_terminate)
        graph.add_edge(START, NODE_GUARD)
        graph.add_conditional_edges(NODE_GUARD, self._guard_branch,
                                    {BRANCH_RELOGIN: NODE_RELOGIN,
                                     BRANCH_CONTINUE: NODE_ROUTE})
        graph.add_conditional_edges(NODE_ROUTE, self._branch,
                                    {BRANCH_CLARIFY: NODE_CLARIFY,
                                     BRANCH_DISPATCH: NODE_DISPATCH,
                                     BRANCH_TERMINATE: NODE_TERMINATE})
        graph.add_edge(NODE_RELOGIN, END)
        graph.add_edge(NODE_CLARIFY, END)
        graph.add_edge(NODE_DISPATCH, NODE_AGGREGATE)
        graph.add_edge(NODE_AGGREGATE, END)
        graph.add_edge(NODE_TERMINATE, END)
        return graph.compile()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _guard_branch(state: dict[str, Any]) -> str:
        return BRANCH_CONTINUE if state.get("login_status") else BRANCH_RELOGIN

    @staticmethod
    def _branch(state: dict[str, Any]) -> str:
        if state.get("clarification"):
            return BRANCH_CLARIFY
        if state.get("intent"):
            return BRANCH_DISPATCH
        return BRANCH_TERMINATE

    async def _node_guard(self, state: dict[str, Any]) -> dict[str, Any]:
        """计划书三、P3「开发全局状态监听机制」。"""
        trace = self._current_trace()
        with trace.span(NODE_GUARD, login_status=bool(state.get("login_status"))):
            return {}

    async def _node_relogin(self, state: dict[str, Any]) -> dict[str, Any]:
        """计划书三、P3「会话失效直接终止 Graph 流程，返回重新登录」。

        **不进入路由、不触发任何子 Agent**——与会话失效时的静默跳过不同，
        这里要显式终止并给出可被移动端识别的信号（三、P7 据此弹窗重登）。
        """
        trace = self._current_trace()
        with trace.span(NODE_RELOGIN):
            degradation = list(state.get("degradation") or [])
            degradation.append("会话失效：直接终止 Graph 流程，要求重新登录")
            return {"login_required": LOGIN_REQUIRED, "degradation": degradation,
                    "dispatched": [], "agent_results": {}}

    # ------------------------------ 节点 ------------------------------ #
    async def _node_route(self, state: dict[str, Any]) -> dict[str, Any]:
        trace = self._current_trace()
        with trace.span(NODE_ROUTE):
            try:
                decision = await self.router.route(RouteRequest(
                    user_id=state["user_id"], session_id=state["session_id"],
                    utterance=state.get("utterance", ""),
                    labels=tuple(state.get("labels") or ()),
                    trace_id=state.get("trace_id", "")))
            except InputGuardError as exc:
                # 风控拦截：**礼貌终止**，不是崩溃。
                # 与「会话失效直接终止」同一口径 —— 返回状态而不抛出去，
                # 让 _branch 因「没有 intent」自然走到 NODE_TERMINATE。
                degradation = list(state.get("degradation") or [])
                degradation.append(f"输入未通过风控：{exc}")
                return {"degradation": degradation, "dispatched": [], "agent_results": {}}
            updates: dict[str, Any] = {
                "route_decision": decision.as_dict(),
                "intent": decision.label,
                "prev_layer": decision.layer,
                "trace_id": decision.trace_id,
                # 风控结论落到 State：下游据此拒绝把输入沉淀为事实
                "input_untrusted": decision.input_untrusted,
            }
            if decision.require_clarification:
                # JEV-003：暂停分发，等待用户确认
                updates["clarification"] = {
                    "question": decision.clarifying_question,
                    "category": decision.clarification_category,
                    "candidates": list(decision.candidate_labels),
                }
                updates["degradation"] = list(decision.degradation_notes)
            return updates

    async def _node_clarify(self, state: dict[str, Any]) -> dict[str, Any]:
        trace = self._current_trace()
        with trace.span(NODE_CLARIFY, category=state["clarification"].get("category", "")):
            # 只记录，不选优先级、不分发（multi_intent_auto_resolve=False）
            return {}

    async def _node_dispatch(self, state: dict[str, Any]) -> dict[str, Any]:
        trace = self._current_trace()
        with trace.span(NODE_DISPATCH, intent=state.get("intent") or ""):
            intake_updates = await self._run_intake(state, trace)
            # 让本轮分发**看得到**接通结论（否则 intake 只是写给账本看的摆设）
            updates = await self._dispatch({**state, **intake_updates}, trace)
            # 接通是**加法**：只补 `intake` 通道，其余一律以分发结论为准。
            # 由谁执行仍然只由 路由 + DispatchPlan 决定（多意图自动分派为 False
            # 是计划书规则），离散化不能反过来改写一个确定性结论。
            return {**intake_updates, **updates}

    async def _run_intake(self, state: dict[str, Any], trace: Trace) -> dict[str, Any]:
        """输入接通层（U114 装配点）：分发前把用户输入离散化并跑裁剪过的回路。

        装配在**唯一的任务分发者**上，而不是只在测试里调 —— 只在测试里调过
        的链路等于没有链路（E-86）。

        三道闸都不会被跳过：
        1. 没装网关 → 整层不存在，不写 `intake`；
        2. 开关关闭 → 同上，行为与接通前逐字一致；
        3. 会话失效 → 不跑（与子 Agent 跳过执行同一口径）。

        回路里的工具调用走的是**同一个** `self.dispatcher`（权限三层求值、审批、
        重复拦截、无增量终止、超时全在里面），function calling 不是绕过闸门的后门。
        """
        from ai_butler.core.config import get_settings
        from ai_butler.scheduler.intake import run_units

        if self.gateway is None:
            return {}
        cfg = get_settings()
        if not getattr(cfg, "function_calling_enable", False):
            return {}
        if not state.get("login_status"):
            return {}
        try:
            with trace.span("intake"):
                report = await run_units(
                    gateway=self.gateway, dispatcher=self.dispatcher,
                    registry=self.dispatcher.registry, user_id=state.get("user_id", ""),
                    utterance=state.get("utterance", "") or "",
                    intent_label=state.get("intent") or "", settings=cfg)
        except Exception as exc:                  # noqa: BLE001 — 接通失败不阻断主流程
            # 如实留痕：写的是「接通失败」，不是「接通了但没结果」
            return {"intake": {"disabled": False, "error":
                               f"{type(exc).__name__}: {exc}", "units": [],
                               "ran_count": 0, "unassigned": []}}
        return {"intake": report.as_dict()}

    async def _dispatch_vertical(self, state: dict[str, Any], trace: Trace) -> dict[str, Any]:
        """把任务交给垂类 Agent（U110；表 11：垂类是**业务执行单元**）。

        垂类键（如 `"tour"`）不在 `DispatchPlan` 的检索分类链里，走阶段链等于空转，
        所以这里单独一支：命中即由垂类自己执行，执行完就结束本次分发。

        三种失败都**留痕且不阻断主流程**（三、P3 全链路降级）：装配失败、执行抛错、
        越权改写受保护字段 —— 分别记进 `failed` + `degradation`，不往上抛。
        """
        key = str(state.get("intent") or "")
        name = f"vertical:{key}"
        degradation: list[str] = list(state.get("degradation") or [])
        results: dict[str, Any] = {}
        dispatched: list[str] = []
        failed: list[str] = []

        def _done() -> dict[str, Any]:
            return {"agent_results": results, "dispatched": dispatched, "skipped": [],
                    "failed": failed, "degradation": degradation,
                    "rollback_count": int(state.get("rollback_count") or 0),
                    "rolled_back": list(state.get("rolled_back") or [])}

        if not state.get("login_status"):
            degradation.append("会话失效，垂类 Agent 跳过执行（三、P4）")
            return _done()

        with trace.span(name):
            agent = self.verticals.resolve(key)
            if agent is None:
                failed.append(name)
                degradation.append(
                    f"垂类 {key!r} 无可用执行者：{self.verticals.note_error(key)}")
                return _done()
            try:
                updates = await resolve(agent(dict(state)))
            except Exception as exc:             # noqa: BLE001 — 垂类失败不阻断主流程
                failed.append(name)
                degradation.append(
                    f"垂类 Agent {key!r} 执行失败：{type(exc).__name__}: {exc}")
                return _done()
            illegal = [k for k in updates if k in PROTECTED_STATE_FIELDS]
            if illegal:
                failed.append(name)
                degradation.append(
                    f"垂类 Agent {key!r} 试图改写受保护字段 {illegal}，已拒绝（三、P4）")
                return _done()
            results[name] = updates
            dispatched.append(name)
        return _done()

    async def _dispatch(self, state: dict[str, Any], trace: Trace) -> dict[str, Any]:
        # 垂类优先（U110）：L4 命中的是垂类键而非检索分类，交给垂类派发器执行。
        if self.verticals is not None and self.verticals.has(state.get("intent")):
            return await self._dispatch_vertical(state, trace)
        chain = self.plan.agents_for(state.get("intent"))
        results: dict[str, Any] = {}
        dispatched: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        degradation: list[str] = list(state.get("degradation") or [])
        rollback = int(state.get("rollback_count") or 0)
        # 计划书三、P3「禁止把任务再次分发给刚刚回退的 Agent，杜绝循环甩锅」
        rolled_back: set[str] = set(state.get("rolled_back") or [])

        if not state.get("login_status"):
            # 正常路径下 guard 节点已拦下；此处为纵深防御（计划书三、P4「会话失效则跳过执行」）
            skipped.extend(chain)
            degradation.append("会话失效，子 Agent 全部跳过执行")
            return {"agent_results": results, "dispatched": dispatched, "skipped": skipped,
                    "degradation": degradation, "rollback_count": rollback,
                    "failed": failed, "rolled_back": sorted(rolled_back)}

        # 计划书三、P3「无增量终止」：工具调用已判定原地打转即停止分发
        if self.dispatcher.no_progress:
            skipped.extend(chain)
            degradation.append(
                f"工具调用判定无增量（{self.dispatcher.blocked_reason}），"
                f"按计划书三、P3 终止分发")
            return {"agent_results": results, "dispatched": dispatched, "skipped": skipped,
                    "degradation": degradation, "rollback_count": rollback,
                    "failed": failed, "rolled_back": sorted(rolled_back)}

        # 计划书三、P3「全局最大回退次数 = 2 次」：已达上限即停止分发
        if rollback >= MAX_ROLLBACKS:
            skipped.extend(chain)
            degradation.append(
                f"已达全局最大回退次数 {MAX_ROLLBACKS} 次，停止本次分发")
            return {"agent_results": results, "dispatched": dispatched, "skipped": skipped,
                    "degradation": degradation, "rollback_count": rollback,
                    "failed": failed, "rolled_back": sorted(rolled_back)}

        for name in chain:
            if name in rolled_back:
                skipped.append(name)
                degradation.append(
                    f"子 Agent {name} 刚刚回退过，禁止原路回退（计划书三、P3 防甩锅）")
                continue

            agent = self.agents.get(name)
            if agent is None:
                skipped.append(name)
                degradation.append(f"未注册的子 Agent：{name}")
                continue

            with trace.span(f"agent:{name}"):
                try:
                    # 唯一下发入口；携带 user_id 与 wiki_digest。
                    # resolve 同时兼容同步 run 与异步 run（asyncpg 连接与事件循环绑定，
                    # 需要读数据中台的 Agent 只能是异步的）。
                    updates = await resolve(agent(dict(state)))
                except Exception as exc:             # noqa: BLE001 — 单 Agent 失败不阻断主流程
                    failed.append(name)
                    rolled_back.add(name)
                    rollback += 1
                    degradation.append(f"子 Agent {name} 执行失败并回滚：{type(exc).__name__}: {exc}")
                    if rollback >= MAX_ROLLBACKS:
                        degradation.append(
                            f"回退次数达到上限 {MAX_ROLLBACKS} 次，终止本次分发")
                        break
                    continue
                illegal = [k for k in updates if k in PROTECTED_STATE_FIELDS]
                if illegal:
                    failed.append(name)
                    rolled_back.add(name)
                    rollback += 1
                    degradation.append(
                        f"子 Agent {name} 试图改写受保护字段 {illegal}，已拒绝（计划书三、P4）")
                    if rollback >= MAX_ROLLBACKS:
                        degradation.append(
                            f"回退次数达到上限 {MAX_ROLLBACKS} 次，终止本次分发")
                        break
                    continue
                results[name] = updates
                dispatched.append(name)
        return {"agent_results": results, "dispatched": dispatched, "skipped": skipped,
                "failed": failed, "degradation": degradation, "rollback_count": rollback,
                "rolled_back": sorted(rolled_back)}

    async def _node_aggregate(self, state: dict[str, Any]) -> dict[str, Any]:
        trace = self._current_trace()
        merged: dict[str, Any] = {}
        for name, updates in (state.get("agent_results") or {}).items():
            if isinstance(updates, dict):
                merged.update(updates)
        with trace.span(NODE_AGGREGATE, agents=len(merged)):
            # 合并结果仍不得覆盖受保护字段
            safe = {k: v for k, v in merged.items() if k not in PROTECTED_STATE_FIELDS}
            return {"merged": safe, "tool_call_history": list(state.get("tool_call_history") or [])
                    + [{"node": NODE_DISPATCH, "intent": state.get("intent"),
                        "agents": sorted((state.get("agent_results") or {}).keys())}]}

    async def _node_terminate(self, state: dict[str, Any]) -> dict[str, Any]:
        trace = self._current_trace()
        with trace.span(NODE_TERMINATE):
            degradation = list(state.get("degradation") or [])
            degradation.append("五层路由均未给出可用意图，主流程安全终止")
            return {"degradation": degradation, "merged": {}}

    # ------------------------------ 入口 ------------------------------ #
    def _current_trace(self) -> Trace:
        from ai_butler.scheduler.observe import current_trace
        trace = current_trace()
        if trace is None:                            # pragma: no cover — run() 总会绑定
            raise SchedulerError("节点执行时没有绑定 Trace，请通过 SceneScheduler.run 调用")
        return trace

    async def run(self, *, user_id: str, session_id: str, utterance: str,
                  labels: Sequence[str], login_status: bool = True,
                  wiki_digest: Any = None, trace_id: str = "",
                  role: UserRole | None = None) -> SchedulerResult:
        """用户发起任意请求的入口（计划书三、P3 触发时机）。

        `role` 为本次任务的用户角色（会话鉴权可达时由上层传入）；
        缺省沿用构造时的角色。角色参与每一次工具调用的三层权限求值（U45）。
        """
        state = new_state(user_id=user_id, session_id=session_id, login_status=login_status,
                          jev_decision_result=None, wiki_digest=wiki_digest)
        state.update({"utterance": utterance, "labels": tuple(labels),
                      "trace_id": trace_id, "agent_results": {}, "clarification": None,
                      "degradation": [], "route_decision": None, "merged": {}, "input_untrusted": False,
                      "rolled_back": [], "login_required": "",
                      "dispatched": [], "skipped": [], "failed": []})
        validate_state(state)

        # 每次任务重开工具调度计数：重复拦截与无增量判定都以「本次任务」为单位
        self.dispatcher.reset()
        # 权限上下文按任务重绑：角色可能因账号/会话而异，
        # 且审计要能追溯到当次生效的角色（U45）。
        # 判空用 `is not None` 而非真值：UserRole.AUDITOR 的值为 0，
        # `role or self.role` 会把 auditor 静默降级成构造时角色 —— 真踩过。
        self.dispatcher.bind_permission(ToolPermission(
            agent=AgentIdentity(key="p3_scene_scheduler",
                                level=CapabilityLevel.L1_WRITE_OWN),
            role=role if role is not None else self.role))
        observer = self.observer
        if self.journal is not None:
            # 账本在场时，span 逐条桥接落账；trace 结束再落一条汇总
            observer = journal_observer(self.journal, session_fields={
                "user_id": user_id, "session_id": session_id, "trace_id": trace_id})
        trace = Trace(user_id=user_id, session_id=session_id, trace_id=trace_id,
                      observer=observer)
        with trace_scope(trace):
            final = await self._graph.ainvoke(state, {"recursion_limit": 24})
        summary = trace.finish()
        if self.journal is not None:
            # summary 已含 trace_id/user_id/session_id（trace.summary() 的字段）
            self.journal.session_end(**summary)

        decision = final.get("route_decision") or {}
        clarification = final.get("clarification") or {}
        return SchedulerResult(
            trace_id=trace.trace_id, user_id=user_id, session_id=session_id,
            intent=final.get("intent"), layer=decision.get("layer", ""),
            confidence=float(decision.get("confidence") or 0.0),
            require_clarification=bool(clarification),
            clarifying_question=clarification.get("question", ""),
            candidate_labels=tuple(clarification.get("candidates") or ()),
            dispatched=list(final.get("dispatched") or []),
            skipped=list(final.get("skipped") or []),
            failed=list(final.get("failed") or []),
            agent_results=dict(final.get("agent_results") or {}),
            rollback_count=int(final.get("rollback_count") or 0),
            rolled_back=list(final.get("rolled_back") or []),
            login_required=str(final.get("login_required") or ""),
            degraded=bool(final.get("degradation")),
            input_untrusted=bool(final.get("input_untrusted")),
            degradation=list(final.get("degradation") or []),
            tool_calls=self.dispatcher.snapshot(),
            spans=[s.as_dict() for s in trace.spans],
            intake=dict(final.get("intake") or {}),
        )
