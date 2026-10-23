"""P3 工具统一调度。

计划书依据
----------
- 三、P3「实现**工具统一调度、重复调用拦截、无增量终止**；
  调用数据中台、RAG、Wiki **自动透传 state 中的 user_id**」
- 三、P3 核心能力「工具管控、循环熔断、资源节流」

三条约束如何落地
----------------
1. **统一调度**：所有工具调用必须经 ToolDispatcher；没有旁路。
2. **重复调用拦截**：同一 (工具名 + 规范化入参) 再次出现即直接返回上次结果，
   不再真正执行——这是循环空转最常见的形态。
3. **无增量终止**：连续若干次调用都没有产生新信息即判定为原地打转，
   置 no_progress，由调度层据此终止（配合全局最大回退 2 次）。
4. **user_id 自动透传**：声明 needs_user_id 的工具，其 user_id 一律由调度器注入，
   调用方**无从伪造**（传了也会被覆盖）。
5. **权限闸前置**（U43–U45 / U51）：任何 handler 执行之前，必须先过三层权限求值。
   此前权限只接在离线独立入口与 CLI 上，`dispatch` 路径零求值 ——
   本补丁把闸门挪到**工具执行的唯一入口**上（详见 core/permissions.ToolPermission）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    PermissionRequest,
    ToolPermission,
    UserRole,
    evaluate,
)

#: 连续多少次「无增量」即判定为原地打转（实现参数）
DEFAULT_NO_PROGRESS_LIMIT = 3
#: 单次任务允许的工具调用总次数上限（实现参数，防跑飞）
DEFAULT_MAX_CALLS = 50


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ToolError("工具调用必须给出 name")

    @property
    def signature(self) -> str:
        """规范化签名：键排序后序列化，避免同义入参被当成不同调用。"""
        canonical = json.dumps(dict(self.payload), sort_keys=True, ensure_ascii=False,
                               default=str)
        return hashlib.sha256(f"{self.name}|{canonical}".encode("utf-8")).hexdigest()[:16]


@dataclass
class ToolResult:
    call: ToolCall
    ok: bool
    output: Any = None
    note: str = ""
    duplicate: bool = False
    progressed: bool = True
    #: 本次调用实际生效的审批档（U45）。Tier1/Tier2 的调用在审批放行前不得执行。
    approval_tier: int = 0

    @property
    def signature(self) -> str:
        return self.call.signature

    @property
    def needs_approval(self) -> bool:
        """是否触碰了需要人工确认的档位（计划书三、P8「付款动作必须经人工确认」）。"""
        return self.approval_tier >= 1


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[..., Any]
    #: 需要 user_id 的工具：调用时由调度器注入 state 里的 user_id
    needs_user_id: bool = False
    #: 判定本次输出是否带来增量；默认「有非空输出即算增量」
    progresses: Callable[[Any], bool] | None = None
    description: str = ""
    #: 本工具动作所需的能力级（U43 表：只读检索 = L0 / 写自有字段 = L1 /
    #: 调外部工具 = L2 / 支付执行·删数据·改权限 = L3）。
    #: 默认 L2：工具是**进程外动作**，未声明即按副作用对待 ——
    #: 与 U43「不得用布尔量推导」同一立场：宁可将纯函数工具显式标 L0，
    #: 也不给未声明的工具留一个「大概只是读」的默认放行。
    required: CapabilityLevel = CapabilityLevel.L2_SIDE_EFFECT
    #: 本工具接触的数据密级（U44）。None = 沿用分发上下文的密级。
    sensitivity: DataSensitivity | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, name: str, handler: Callable[..., Any], *,
                 needs_user_id: bool = False,
                 progresses: Callable[[Any], bool] | None = None,
                 description: str = "",
                 required: CapabilityLevel = CapabilityLevel.L2_SIDE_EFFECT,
                 sensitivity: DataSensitivity | None = None) -> ToolSpec:
        if not name or not str(name).strip():
            raise ToolError("工具必须声明非空的 name")
        if name in self._specs:
            raise ToolError(f"工具名重复：{name!r}")
        spec = ToolSpec(name=name, handler=handler, needs_user_id=needs_user_id,
                        progresses=progresses, description=description,
                        required=required, sensitivity=sensitivity)
        self._specs[name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolError(f"未注册的工具：{name!r}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[n] for n in self.names())


class ToolDispatcher:
    """工具统一调度器。调度层持有，Agent 只能经它触达工具。"""

    def __init__(self, registry: ToolRegistry, *, throttle: Any = None,
                 no_progress_limit: int = DEFAULT_NO_PROGRESS_LIMIT,
                 max_calls: int = DEFAULT_MAX_CALLS,
                 permission: ToolPermission | None = None) -> None:
        self.registry = registry
        #: 可选资源节流器（三、P3「资源节流」）；给了就按账号扣减工具调用配额
        self.throttle = throttle
        self.no_progress_limit = no_progress_limit
        self.max_calls = max_calls
        #: 权限上下文（U43–U45）。**未绑定即拒绝一切分发** ——
        #: 没有「未绑定时放行」的分支，那正是「声明了没接上」的形状。
        self._permission = permission
        self.reset()

    # ---------------- 权限上下文 ---------------- #
    @property
    def permission(self) -> ToolPermission | None:
        return self._permission

    def bind_permission(self, permission: ToolPermission) -> None:
        """绑定（或切换）权限上下文。

        调度器在每次任务开始时绑定；**子 Agent 触达工具时不重新绑定**，
        而是在 `dispatch(..., agent=...)` 里以自己的身份求值（U51 权限不可传递）。
        """
        self._permission = permission

    # ---------------- 生命周期 ---------------- #
    def reset(self) -> None:
        self._cache: dict[str, ToolResult] = {}
        self._calls: list[ToolResult] = []
        self._streak = 0
        self._no_progress = False
        self._blocked_reason = ""

    # ---------------- 状态 ---------------- #
    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def no_progress(self) -> bool:
        """是否已判定原地打转（调度层据此终止）。"""
        return self._no_progress

    @property
    def blocked_reason(self) -> str:
        return self._blocked_reason

    def history(self) -> tuple[ToolResult, ...]:
        return tuple(self._calls)

    # ---------------- 调度 ---------------- #
    def dispatch(self, call: ToolCall, *, user_id: str | None = None,
                 agent: AgentIdentity | None = None) -> ToolResult:
        if self._no_progress:
            return self._refuse(call, f"已判定无增量（{self._blocked_reason}），拒绝继续调用")
        if self.call_count >= self.max_calls:
            self._no_progress = True
            self._blocked_reason = f"工具调用次数达上限 {self.max_calls}"
            return self._refuse(call, self._blocked_reason)

        spec = self.registry.get(call.name)

        # ---- 权限闸（U43–U45 / U51）：先求值，后执行，先于重复拦截 ----
        # 顺序是刻意的：重复拦截会复用上次结果，若权限判定放在它后面，
        # 一次被放行的调用就能靠缓存反复取用，角色切换后仍然畅通。
        gate = self._permission_gate(call, spec, agent)
        if gate is not None:
            return gate

        cached = self._cache.get(call.signature)
        if cached is not None:
            # 重复调用拦截：直接复用上次结果，不真正执行
            duplicate = ToolResult(call=call, ok=cached.ok, output=cached.output,
                                   note=f"重复调用被拦截（同 {cached.signature}）",
                                   duplicate=True, progressed=False)
            self._calls.append(duplicate)
            self._bump_streak()
            return duplicate

        if self.throttle is not None and user_id:
            from ai_butler.scheduler.throttle import ThrottleError
            try:
                self.throttle.consume_tool_call(str(user_id))
            except ThrottleError as exc:
                result = ToolResult(call=call, ok=False, progressed=False,
                                    note=f"资源节流：{exc}")
                self._calls.append(result)
                self._bump_streak()
                return result

        payload = dict(call.payload)
        if spec.needs_user_id:
            # 自动透传：调用方传入的 user_id 一律被 state 的值覆盖，无从伪造
            if not user_id or not str(user_id).strip():
                result = ToolResult(call=call, ok=False,
                                    note="该工具需要 user_id，但当前上下文未绑定账号")
                self._calls.append(result)
                return result
            payload["user_id"] = str(user_id)

        try:
            output = spec.handler(**payload)
        except Exception as exc:                      # noqa: BLE001 — 单次工具失败不阻断主流程
            result = ToolResult(call=call, ok=False, progressed=False,
                                note=f"{type(exc).__name__}: {exc}")
            self._calls.append(result)
            self._bump_streak()
            return result

        progressed = True
        if spec.progresses is not None:
            progressed = bool(spec.progresses(output))
        elif output is None:
            progressed = False
        result = ToolResult(call=call, ok=True, output=output,
                            progressed=progressed,
                            note="" if progressed else "本次调用未产生新信息")
        self._cache[call.signature] = result
        self._calls.append(result)
        self._bump_streak(progressed)
        return result

    def dispatch_many(self, calls: Sequence[ToolCall], *, user_id: str | None = None,
                      agent: AgentIdentity | None = None) -> list[ToolResult]:
        return [self.dispatch(c, user_id=user_id, agent=agent) for c in calls]

    # ---------------- 内部 ---------------- #
    def _permission_gate(self, call: ToolCall, spec: ToolSpec,
                         agent: AgentIdentity | None) -> ToolResult | None:
        """三层权限求值。返回 None = 放行；返回 ToolResult = 拒绝（已记账）。

        **未绑定权限上下文一律拒绝**：没有主体、角色、密级齐备的请求无从判定，
        而无从判定即拒绝（core/permissions.py 模块纪律）。
        """
        ctx = self._permission
        if ctx is None:
            return self._deny(
                call, "未绑定权限上下文（ToolPermission）—— 无从判定即拒绝，不执行（U43–U45）")
        # U51 权限不可传递：调用方显式给出 agent 身份时，以**调用方自己**的身份求值，
        # 不继承调度器绑定的身份 —— 否则调用链就是一条借道提权的梯子。
        identity = agent if agent is not None else ctx.agent
        sensitivity = (spec.sensitivity if spec.sensitivity is not None
                       else ctx.sensitivity)
        verdict = evaluate(PermissionRequest(
            agent=identity, role=ctx.role, sensitivity=sensitivity,
            required=spec.required))
        if not verdict.allowed:
            failed = [k for k, ok in verdict.by_layer.items() if not ok]
            return self._deny(
                call,
                f"权限拒绝：{verdict.reason}（未通过的层：{failed or '—'}；"
                f"工具 {call.name} 需要 {spec.required.name}）")
        if int(verdict.tier) >= 1:
            # Tier1/Tier2 在人工审批放行前**不执行** ——
            # 计划书三、P8「付款动作必须经人工确认」。审批服务接入后，
            # 由 dispatch 先取审批结论再放行（边界见 core/decisions.py U53）。
            return self._deny(
                call,
                f"需人工审批（Tier{int(verdict.tier)}）后放行：{call.name}"
                f"（审批服务接入前一律不执行，U45）",
                tier=int(verdict.tier))
        return None

    def _deny(self, call: ToolCall, note: str, *, tier: int = 0) -> ToolResult:
        result = ToolResult(call=call, ok=False, note=note, progressed=False,
                            approval_tier=tier)
        self._calls.append(result)
        self._bump_streak()
        return result

    def _bump_streak(self, progressed: bool = False) -> None:
        self._streak = 0 if progressed else self._streak + 1
        if self._streak >= self.no_progress_limit:
            self._no_progress = True
            self._blocked_reason = f"连续 {self._streak} 次调用无增量"

    def _refuse(self, call: ToolCall, note: str) -> ToolResult:
        result = ToolResult(call=call, ok=False, note=note, progressed=False)
        self._calls.append(result)
        return result

    def snapshot(self) -> dict[str, Any]:
        return {"calls": self.call_count, "no_progress": self._no_progress,
                "blocked_reason": self._blocked_reason,
                "duplicates": sum(1 for r in self._calls if r.duplicate),
                "failures": sum(1 for r in self._calls if not r.ok),
                #: 权限闸是否绑定（未绑定 = 一切分发被拒，见 _permission_gate）
                "permission_bound": self._permission is not None,
                #: 被权限闸拦下的次数（三层拒绝 + 待审批档，审计要能分别看见）
                "denied": sum(1 for r in self._calls
                              if not r.ok and r.note.startswith(("权限拒绝", "未绑定权限", "需人工审批"))),
                "approval_held": sum(1 for r in self._calls if r.approval_tier >= 1),
                "tools": list(self.registry.names())}
