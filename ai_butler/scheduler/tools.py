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
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

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

    @property
    def signature(self) -> str:
        return self.call.signature


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[..., Any]
    #: 需要 user_id 的工具：调用时由调度器注入 state 里的 user_id
    needs_user_id: bool = False
    #: 判定本次输出是否带来增量；默认「有非空输出即算增量」
    progresses: Callable[[Any], bool] | None = None
    description: str = ""


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, name: str, handler: Callable[..., Any], *,
                 needs_user_id: bool = False,
                 progresses: Callable[[Any], bool] | None = None,
                 description: str = "") -> ToolSpec:
        if not name or not str(name).strip():
            raise ToolError("工具必须声明非空的 name")
        if name in self._specs:
            raise ToolError(f"工具名重复：{name!r}")
        spec = ToolSpec(name=name, handler=handler, needs_user_id=needs_user_id,
                        progresses=progresses, description=description)
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
                 max_calls: int = DEFAULT_MAX_CALLS) -> None:
        self.registry = registry
        #: 可选资源节流器（三、P3「资源节流」）；给了就按账号扣减工具调用配额
        self.throttle = throttle
        self.no_progress_limit = no_progress_limit
        self.max_calls = max_calls
        self.reset()

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
    def dispatch(self, call: ToolCall, *, user_id: str | None = None) -> ToolResult:
        if self._no_progress:
            return self._refuse(call, f"已判定无增量（{self._blocked_reason}），拒绝继续调用")
        if self.call_count >= self.max_calls:
            self._no_progress = True
            self._blocked_reason = f"工具调用次数达上限 {self.max_calls}"
            return self._refuse(call, self._blocked_reason)

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

        spec = self.registry.get(call.name)
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

    def dispatch_many(self, calls: Sequence[ToolCall], *, user_id: str | None = None
                      ) -> list[ToolResult]:
        return [self.dispatch(c, user_id=user_id) for c in calls]

    # ---------------- 内部 ---------------- #
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
                "tools": list(self.registry.names())}
