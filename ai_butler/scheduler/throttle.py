"""P3 资源节流：Token 限流 · 检索量管控 · 资源节流。

计划书依据
----------
- 三、P3「开发 **Token 限流、检索量管控、资源节流**逻辑；
  **限流按账号维度统计**；**Jev 调用独立配额统计**」
- 三、P3 核心能力「循环熔断、资源节流」

与网关限流的分工
----------------
- 网关 ~{LLMGateway._check_rate}~ 用 Redis 做**跨进程**的 LLM / Jev 每分调用次数限流；
- 本模块做**任务内**的资源预算：Token 消耗、检索次数、工具调用次数，
  按账号维度计数，超限即拒绝并给出可读原因。

计数存放：默认进程内滑动分钟窗口（实现参数）。多进程部署时应换成 Redis，
与网关限流同一套做法——已登记为后续升级项，不在本阶段发明新参数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class ThrottleError(RuntimeError):
    """资源预算超限。"""


#: 各配额的默认值（实现参数；计划书未给数值）
DEFAULT_TOKENS_PER_ACCOUNT_PER_MINUTE = 60_000
DEFAULT_RETRIEVALS_PER_ACCOUNT_PER_MINUTE = 30
DEFAULT_JEV_CALLS_PER_ACCOUNT_PER_MINUTE = 120
DEFAULT_TOOL_CALLS_PER_TASK = 50
WINDOW_SECONDS = 60


@dataclass(frozen=True)
class ThrottlePolicy:
    tokens_per_account_per_minute: int = DEFAULT_TOKENS_PER_ACCOUNT_PER_MINUTE
    retrievals_per_account_per_minute: int = DEFAULT_RETRIEVALS_PER_ACCOUNT_PER_MINUTE
    jev_calls_per_account_per_minute: int = DEFAULT_JEV_CALLS_PER_ACCOUNT_PER_MINUTE
    tool_calls_per_task: int = DEFAULT_TOOL_CALLS_PER_TASK

    def __post_init__(self) -> None:
        for name in ("tokens_per_account_per_minute", "retrievals_per_account_per_minute",
                     "jev_calls_per_account_per_minute", "tool_calls_per_task"):
            if getattr(self, name) <= 0:
                raise ThrottleError(f"{name} 必须为正数")


class ResourceThrottle:
    """按账号维度的资源预算。Jev 走**独立**配额，不占业务话术的预算。"""

    #: 配额类别
    TOKENS = "tokens"
    RETRIEVALS = "retrievals"
    JEV = "jev"
    TOOL_CALLS = "tool_calls"
    BUCKETS: tuple[str, ...] = (TOKENS, RETRIEVALS, JEV, TOOL_CALLS)

    def __init__(self, policy: ThrottlePolicy | None = None, *,
                 clock=time.monotonic) -> None:
        self.policy = policy or ThrottlePolicy()
        self._clock = clock
        self._windows: dict[tuple[str, str], list[tuple[float, int]]] = {}
        self._task_calls: dict[str, int] = {}

    # ---------------- 窗口 ---------------- #
    def _limit(self, bucket: str) -> int:
        return {
            self.TOKENS: self.policy.tokens_per_account_per_minute,
            self.RETRIEVALS: self.policy.retrievals_per_account_per_minute,
            self.JEV: self.policy.jev_calls_per_account_per_minute,
            self.TOOL_CALLS: self.policy.tool_calls_per_task,
        }[bucket]

    def _require_user(self, user_id: str | None, bucket: str) -> str:
        if not user_id or not str(user_id).strip():
            raise ThrottleError(
                f"{bucket} 配额必须按账号维度统计（计划书三、P3），但未提供 user_id")
        return str(user_id)

    def _prune(self, key: tuple[str, str], now: float) -> list[tuple[float, int]]:
        events = [e for e in self._windows.get(key, []) if now - e[0] < WINDOW_SECONDS]
        self._windows[key] = events
        return events

    def used(self, user_id: str, bucket: str) -> int:
        if bucket == self.TOOL_CALLS:
            return self._task_calls.get(str(user_id), 0)
        now = self._clock()
        return sum(n for _, n in self._prune((str(user_id), bucket), now))

    def remaining(self, user_id: str, bucket: str) -> int:
        return max(0, self._limit(bucket) - self.used(user_id, bucket))

    # ---------------- 消耗 ---------------- #
    def consume(self, user_id: str | None, bucket: str, amount: int = 1) -> int:
        """消耗配额，返回消耗后的用量。超限即抛 ThrottleError。"""
        if bucket not in self.BUCKETS:
            raise ThrottleError(f"未知配额类别 {bucket!r}，可选 {list(self.BUCKETS)}")
        if amount <= 0:
            raise ThrottleError("消耗量必须为正数")
        uid = self._require_user(user_id, bucket)

        if bucket == self.TOOL_CALLS:
            used = self._task_calls.get(uid, 0) + amount
            if used > self._limit(bucket):
                raise ThrottleError(
                    f"账号 {uid} 本次任务的工具调用已达上限 "
                    f"{self.policy.tool_calls_per_task}")
            self._task_calls[uid] = used
            return used

        now = self._clock()
        events = self._prune((uid, bucket), now)
        used = sum(n for _, n in events) + amount
        if used > self._limit(bucket):
            raise ThrottleError(
                f"账号 {uid} 当前分钟内的 {bucket} 用量已达上限 {self._limit(bucket)}")
        events.append((now, amount))
        self._windows[(uid, bucket)] = events
        return used

    def consume_tokens(self, user_id: str, tokens: int) -> int:
        return self.consume(user_id, self.TOKENS, tokens)

    def consume_retrieval(self, user_id: str, count: int = 1) -> int:
        return self.consume(user_id, self.RETRIEVALS, count)

    def consume_jev(self, user_id: str, count: int = 1) -> int:
        """Jev 独立配额：与业务话务分开计数，互不挤占。"""
        return self.consume(user_id, self.JEV, count)

    def consume_tool_call(self, user_id: str, count: int = 1) -> int:
        return self.consume(user_id, self.TOOL_CALLS, count)

    def reset_task(self, user_id: str) -> None:
        self._task_calls.pop(str(user_id), None)

    # ---------------- 观测 ---------------- #
    def snapshot(self, user_id: str) -> dict[str, Any]:
        return {bucket: {"used": self.used(user_id, bucket),
                         "limit": self._limit(bucket),
                         "remaining": self.remaining(user_id, bucket)}
                for bucket in self.BUCKETS}
