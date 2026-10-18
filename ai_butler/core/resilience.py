"""P0-1 全局中间件内核：接口熔断 · 超时 · 降级 · 防雪崩。

计划书依据
----------
- 三、P0-1「开发全局中间件：接口熔断、超时、降级、防雪崩机制」
- 三、P0-1「Jev 服务故障系统可降级兼容原有路由」（降级语义在网关侧已落地，本模块泛化）
- 三、P3「开发 Token 限流、检索量管控、资源节流逻辑」

四道闸门的顺序（自外向内）
------------------------
1. 防雪崩 Bulkhead  —— 按依赖维度限制并发，单个慢依赖不得耗尽全部工作线程
2. 熔断 CircuitBreaker —— 连续失败达阈值即快速失败，冷却后半开探测
3. 超时 Timeout       —— 单次调用硬截止
4. 降级 Fallback      —— 前 3 道闸门任一跳闸，返回登记好的降级结果而不是抛穿上层

参数性质
--------
本模块全部阈值均为**实现参数**（负载相关），不属于计划书【八】固化参数，
可按实际负载调整；取值经 Settings 提供。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable, TypeVar

from ai_butler.core.naming import redis_key

T = TypeVar("T")

STATE_CLOSED = "closed"
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """熔断打开：快速失败，不再打后端。"""


class BulkheadFullError(RuntimeError):
    """防雪崩闸门已满：并发已达上限且等待超时。"""


class DependencyTimeout(RuntimeError):
    """依赖调用超时。"""


# --------------------------------------------------------------------------- #
# 防雪崩：按依赖维度的并发闸
# --------------------------------------------------------------------------- #
class Bulkhead:
    def __init__(self, name: str, *, max_concurrency: int = 8,
                 acquire_timeout: float = 5.0) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency 必须为正数")
        self.name = name
        self.max_concurrency = int(max_concurrency)
        self.acquire_timeout = float(acquire_timeout)
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self.in_flight = 0

    async def __aenter__(self) -> "Bulkhead":
        try:
            await asyncio.wait_for(self._sem.acquire(), self.acquire_timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise BulkheadFullError(
                f"依赖 {self.name!r} 并发已达上限 {self.max_concurrency}，"
                f"等待 {self.acquire_timeout}s 仍无空位（防雪崩拦截）"
            ) from exc
        self.in_flight += 1
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.in_flight -= 1
        self._sem.release()


# --------------------------------------------------------------------------- #
# 熔断：closed → open → half_open → closed
# --------------------------------------------------------------------------- #
class CircuitBreaker:
    """三态熔断器。提供 Redis 时状态跨进程一致；否则退化为进程内状态。"""

    def __init__(self, name: str, *, redis=None, failure_threshold: int = 5,
                 cooldown_seconds: int = 30, half_open_max_calls: int = 1) -> None:
        self.name = name
        self._redis = redis
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = int(cooldown_seconds)
        self.half_open_max_calls = int(half_open_max_calls)
        self._mem: dict[str, Any] = {"state": STATE_CLOSED, "failures": 0,
                                     "opened_at": 0.0, "probes": 0}

    @property
    def _key(self) -> str:
        return redis_key("breaker", self.name)

    async def _read(self) -> dict[str, Any]:
        if self._redis is None:
            return dict(self._mem)
        raw = await self._redis.hgetall(self._key)
        if not raw:
            return {"state": STATE_CLOSED, "failures": 0, "opened_at": 0.0, "probes": 0}
        return {
            "state": raw.get("state", STATE_CLOSED),
            "failures": int(raw.get("failures", 0)),
            "opened_at": float(raw.get("opened_at", 0)),
            "probes": int(raw.get("probes", 0)),
        }

    async def _write(self, **fields: Any) -> None:
        if self._redis is None:
            self._mem.update(fields)
            return
        await self._redis.hset(self._key, mapping={k: str(v) for k, v in fields.items()})
        await self._redis.expire(self._key, max(self.cooldown_seconds * 10, 600))

    async def state(self) -> str:
        return (await self._read())["state"]

    async def allow(self) -> None:
        """放行则返回 None，否则抛 CircuitOpenError。"""
        snap = await self._read()
        state = snap["state"]
        now = time.time()
        if state == STATE_OPEN:
            if now - snap["opened_at"] < self.cooldown_seconds:
                raise CircuitOpenError(
                    f"依赖 {self.name!r} 处于熔断状态，"
                    f"冷却剩余 {self.cooldown_seconds - (now - snap['opened_at']):.1f}s"
                )
            await self._write(state=STATE_HALF_OPEN, probes=1)      # 冷却结束 → 半开探测
            return
        if state == STATE_HALF_OPEN:
            if snap["probes"] >= self.half_open_max_calls:
                raise CircuitOpenError(f"依赖 {self.name!r} 半开态探测名额已满，继续快速失败")
            await self._write(probes=snap["probes"] + 1)

    async def record_success(self) -> None:
        await self._write(state=STATE_CLOSED, failures=0, opened_at=0.0, probes=0)

    async def record_failure(self) -> None:
        snap = await self._read()
        failures = snap["failures"] + 1
        if snap["state"] == STATE_HALF_OPEN or failures >= self.failure_threshold:
            await self._write(state=STATE_OPEN, failures=failures, opened_at=time.time())
        else:
            await self._write(failures=failures)

    async def reset(self) -> None:
        await self.record_success()


# --------------------------------------------------------------------------- #
# 组合入口：防雪崩 → 熔断 → 超时 → 降级
# --------------------------------------------------------------------------- #
class Resilience:
    """按依赖名持有闸门，业务侧只调用 call()。"""

    def __init__(self, *, settings, redis=None) -> None:
        self.settings = settings
        self.redis = redis
        self._bulkheads: dict[str, Bulkhead] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def bulkhead(self, name: str) -> Bulkhead:
        if name not in self._bulkheads:
            self._bulkheads[name] = Bulkhead(
                name,
                max_concurrency=self.settings.bulkhead_max_concurrency,
                acquire_timeout=self.settings.bulkhead_acquire_timeout_seconds,
            )
        return self._bulkheads[name]

    def breaker(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name, redis=self.redis,
                failure_threshold=self.settings.circuit_breaker_failure_threshold,
                cooldown_seconds=self.settings.circuit_breaker_cooldown_seconds,
                half_open_max_calls=self.settings.circuit_breaker_half_open_max_calls,
            )
        return self._breakers[name]

    async def call(self, name: str, fn: Callable[..., Awaitable[T]], *,
                   timeout: float | None = None,
                   fallback: Callable[[BaseException], Any] | None = None,
                   **kwargs: Any) -> Any:
        deadline = timeout if timeout is not None else self.settings.request_timeout_seconds
        try:
            async with self.bulkhead(name):                 # 1 防雪崩
                await self.breaker(name).allow()            # 2 熔断
                try:
                    result = await asyncio.wait_for(fn(**kwargs), deadline)   # 3 超时
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    await self.breaker(name).record_failure()
                    raise DependencyTimeout(
                        f"依赖 {name!r} 超过 {deadline}s 未返回"
                    ) from exc
                except Exception:
                    await self.breaker(name).record_failure()
                    raise
                await self.breaker(name).record_success()
                return result
        except BaseException as exc:                        # 4 降级
            if fallback is None:
                raise
            return await _resolve_fallback(fallback, exc)


async def _resolve_fallback(fallback: Callable[[BaseException], Any], exc: BaseException) -> Any:
    value = fallback(exc)
    if inspect.isawaitable(value):
        return await value
    return value
