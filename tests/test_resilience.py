"""P0-1 全局中间件内核测试：熔断 · 超时 · 降级 · 防雪崩。

计划书依据：三、P0-1「开发全局中间件：接口熔断、超时、降级、防雪崩机制」。
"""

from __future__ import annotations

import asyncio

import pytest

from ai_butler.core.resilience import (
    STATE_CLOSED,
    STATE_OPEN,
    BulkheadFullError,
    CircuitOpenError,
    DependencyTimeout,
    Resilience,
)


def _res(settings, redis_client, **over):
    return Resilience(settings=settings.model_copy(update=over), redis=redis_client)


def test_bulkhead_caps_concurrency(settings, redis_client):
    """防雪崩：同一依赖的并发不得超过上限。"""
    res = _res(settings, redis_client, bulkhead_max_concurrency=2,
               bulkhead_acquire_timeout_seconds=2.0)
    peak = {"n": 0, "cur": 0}

    async def work():
        peak["cur"] += 1
        peak["n"] = max(peak["n"], peak["cur"])
        await asyncio.sleep(0.05)
        peak["cur"] -= 1
        return "ok"

    async def main():
        return await asyncio.gather(*[res.call("dep", work) for _ in range(6)])

    results = asyncio.run(main())
    assert results == ["ok"] * 6
    assert peak["n"] == 2, f"并发峰值应为 2，实际 {peak['n']}"


def test_bulkhead_full_raises_when_wait_exceeds(settings, redis_client):
    res = _res(settings, redis_client, bulkhead_max_concurrency=1,
               bulkhead_acquire_timeout_seconds=0.05)

    async def work():
        await asyncio.sleep(0.5)
        return "ok"

    async def main():
        return await asyncio.gather(*[res.call("dep", work) for _ in range(3)],
                                    return_exceptions=True)

    results = asyncio.run(main())
    assert sum(isinstance(r, BulkheadFullError) for r in results) >= 1


def test_circuit_opens_after_threshold_then_fast_fails(settings, redis_client):
    res = _res(settings, redis_client, circuit_breaker_failure_threshold=3,
               circuit_breaker_cooldown_seconds=60)

    async def boom():
        raise RuntimeError("dependency down")

    async def main():
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await res.call("dep", boom)
        state = await res.breaker("dep").state()
        with pytest.raises(CircuitOpenError):
            await res.call("dep", boom)
        return state

    assert asyncio.run(main()) == STATE_OPEN


def test_circuit_half_opens_after_cooldown_then_closes(settings, redis_client):
    res = _res(settings, redis_client, circuit_breaker_failure_threshold=1,
               circuit_breaker_cooldown_seconds=0, circuit_breaker_half_open_max_calls=1)

    async def boom():
        raise RuntimeError("down")

    async def ok():
        return "recovered"

    async def main():
        with pytest.raises(RuntimeError):
            await res.call("dep", boom)
        opened = await res.breaker("dep").state()
        recovered = await res.call("dep", ok)       # cooldown=0 → 立即半开探测
        return opened, recovered, await res.breaker("dep").state()

    opened, recovered, closed = asyncio.run(main())
    assert opened == STATE_OPEN
    assert recovered == "recovered"
    assert closed == STATE_CLOSED


def test_timeout_raises_dependency_timeout(settings, redis_client):
    """接口超时：单次调用硬截止。"""
    res = _res(settings, redis_client, circuit_breaker_failure_threshold=99)

    async def slow():
        await asyncio.sleep(5)

    async def main():
        with pytest.raises(DependencyTimeout):
            await res.call("dep", slow, timeout=0.05)

    asyncio.run(main())


def test_fallback_returns_degraded_value_instead_of_raising(settings, redis_client):
    """降级：闸门跳闸时返回登记好的降级结果。"""
    res = _res(settings, redis_client, circuit_breaker_failure_threshold=1,
               circuit_breaker_cooldown_seconds=60)
    seen = {}

    async def boom():
        raise RuntimeError("down")

    def fallback(exc):
        seen["exc"] = exc
        return {"degraded": True}

    async def main():
        return await res.call("dep", boom, fallback=fallback)

    assert asyncio.run(main()) == {"degraded": True}
    assert isinstance(seen["exc"], RuntimeError)


def test_fallback_also_covers_open_breaker(settings, redis_client):
    res = _res(settings, redis_client, circuit_breaker_failure_threshold=1,
               circuit_breaker_cooldown_seconds=60)

    async def boom():
        raise RuntimeError("down")

    async def main():
        with pytest.raises(RuntimeError):
            await res.call("dep", boom)
        return await res.call("dep", boom, fallback=lambda e: "降级兜底")

    assert asyncio.run(main()) == "降级兜底"
