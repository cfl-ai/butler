"""P1-C / P1-D 提速基准：基础设施层极限测量（fake gateway，无真实 LLM 依赖）。

三个测量面：
1. 工具并发规模扫描——一轮 N 个 100ms 只读工具，wall time 应近似
   常数（线程池并行），串行基线为 N×100ms；记录加速比与并发上限。
2. 负载稳定性——50 轮连续并发回合，观察耗时漂移与线程数回收。
3. 路由缓存——不同去重率下的命中收益与单次命中/回源延迟。

复现（项目根目录）：
    .venv/Scripts/python.exe scripts/bench_perf.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_butler.core.permissions import CapabilityLevel, UserRole
from ai_butler.core.permissions import AgentIdentity, ToolPermission
from ai_butler.infra.llm_gateway import ChatMessage, ChatResult, ModelToolCall
from ai_butler.scheduler.router import (
    HardRule,
    HardRuleLayer,
    LayerVerdict,
    MemoizedIntentLayer,
    RouteRequest,
)
from ai_butler.scheduler.tool_loop import FunctionCallLoop
from ai_butler.scheduler.tools import ToolRegistry, ToolDispatcher

LABELS = ("ask_weather", "book_hotel")


# ------------------------- 基础设施 ------------------------- #
def _latency_gateway(replies: list[ChatResult]) -> object:
    class GW:
        def __init__(self) -> None:
            self.replies = list(replies)

        async def chat(self, *, user_id, messages, tools=None, **kw):
            return self.replies.pop(0) if self.replies else ChatResult(
                text="end", provider="fake", model="fake", latency_ms=0)

    return GW()


def _reply(text: str = "", tool_calls=()) -> ChatResult:
    return ChatResult(text=text, provider="fake", model="fake", latency_ms=0,
                      tool_calls=tuple(tool_calls))


def _dispatcher_with_slow_tools(tool_ms: float, count: int) -> ToolDispatcher:
    """注册 count 个各 sleep tool_ms 的 L0 工具。"""
    registry = ToolRegistry()
    for i in range(count):
        def handler(payload=None, _ms=tool_ms, **kw):
            time.sleep(_ms / 1000.0)
            return {"ok": True}
        registry.register(f"t{i}", handler, description="只读探针",
                          params_schema={"type": "object"},
                          required=CapabilityLevel.L0_READ)
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="bench", level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER))
    return dispatcher


# ------------------------- 1. 并发规模扫描 ------------------------- #
def bench_tool_concurrency() -> None:
    print("=" * 64)
    print("1) 工具并发规模扫描（单工具 100ms，单轮 N 个只读工具）")
    print(f"{'N 工具数':>8} {'串行基线ms':>12} {'实测wall ms':>12} {'加速比':>8} {'轮数':>6}")
    for n in (1, 2, 4, 8, 16, 32):
        dispatcher = _dispatcher_with_slow_tools(100.0, n)
        calls = [ModelToolCall(name=f"t{i}", arguments={}) for i in range(n)]
        gateway = _latency_gateway([
            _reply(tool_calls=calls),
            _reply(text="done"),
        ])
        loop = FunctionCallLoop(gateway, dispatcher, max_rounds=2)
        t0 = time.perf_counter()
        out = asyncio.run(loop.run(user_id="bench",
                                   messages=[ChatMessage(role="user", content="x")],
                                   tools=[]))
        wall_ms = (time.perf_counter() - t0) * 1000
        serial_ms = n * 100.0
        speedup = serial_ms / wall_ms if wall_ms else 0
        ok = "OK" if (out.ok and len(out.tool_results) == n) else "FAIL"
        print(f"{n:>8} {serial_ms:>12.0f} {wall_ms:>12.0f} {speedup:>7.1f}x {ok:>5}")


# ------------------------- 2. 负载稳定性 ------------------------- #
def bench_load_stability(rounds: int = 50, tools_per_round: int = 8) -> None:
    print("=" * 64)
    print(f"2) 负载稳定性：连续 {rounds} 回合 × 每回合 {tools_per_round} 个 30ms 只读工具"
          f"（每回合重置 dispatcher，测真实执行而非重复拦截）")
    baseline_threads = threading.active_count()
    walls: list[float] = []
    total_calls = 0
    for i in range(rounds):
        dispatcher = _dispatcher_with_slow_tools(30.0, tools_per_round)
        calls = [ModelToolCall(name=f"t{j}", arguments={}) for j in range(tools_per_round)]
        gateway = _latency_gateway([_reply(tool_calls=calls), _reply(text="done")])
        loop = FunctionCallLoop(gateway, dispatcher, max_rounds=2)
        t0 = time.perf_counter()
        out = asyncio.run(loop.run(user_id="bench",
                                   messages=[ChatMessage(role="user", content="x")],
                                   tools=[]))
        assert out.ok, f"第 {i} 回合失败"
        assert all(r.ok for r in out.tool_results)
        total_calls += dispatcher.call_count
        walls.append((time.perf_counter() - t0) * 1000)
    threads_after = threading.active_count()
    p50 = sorted(walls)[len(walls) // 2]
    p95 = sorted(walls)[int(len(walls) * 0.95) - 1]
    drift = (sum(walls[-5:]) / 5) - (sum(walls[:5]) / 5)
    print(f"    P50={p50:.0f}ms  P95={p95:.0f}ms  首5轮均值={sum(walls[:5])/5:.0f}ms  "
          f"末5轮均值={sum(walls[-5:])/5:.0f}ms  漂移={drift:+.0f}ms")
    print(f"    线程数：基线 {baseline_threads} → 结束后 {threads_after}"
          f"（{'无泄漏' if threads_after <= baseline_threads + 2 else '疑似泄漏'}）")
    print(f"    dispatcher 记账：{total_calls} 次调用（期望 {rounds * tools_per_round}，"
          f"{'一致' if total_calls == rounds * tools_per_round else '不一致!'}）")


# ------------------------- 3. 路由缓存 ------------------------- #
class _SlowEmbedder:
    """模拟向量层 encode 的真实开销（20ms/次）。"""

    def __init__(self, dim: int = 8, cost_ms: float = 20.0) -> None:
        self.dim, self.cost = dim, cost_ms / 1000.0

    def encode(self, texts):
        time.sleep(self.cost * len(texts))
        return [[0.1] * self.dim for _ in texts]


class _VectorLikeLayer:
    """向量层形态的假层：encode 开销 + 判定。"""

    name = "vector"

    def __init__(self, cost_ms: float = 20.0) -> None:
        self.embedder = _SlowEmbedder(cost_ms=cost_ms)
        self.calls = 0

    async def evaluate(self, request):
        self.embedder.encode([request.utterance])
        self.calls += 1
        return LayerVerdict(accepted=True, label="ask_weather", confidence=0.9)


def bench_route_cache(distinct: int = 20, repeats: int = 5) -> None:
    print("=" * 64)
    print(f"3) 路由缓存：{distinct} 条不同表述 × 各重复 {repeats} 次（encode 开销 20ms）")
    total = distinct * repeats
    utterances = [f"帮我查第{i}号城市的天气情况" for i in range(distinct)]

    # 无缓存基线（单事件循环内完成，排除建循环开销）
    async def run_all(layer):
        for _ in range(repeats):
            for u in utterances:
                await _eval_once(layer, u)

    inner = _VectorLikeLayer(20.0)
    t0 = time.perf_counter()
    asyncio.run(run_all(inner))
    raw_ms = (time.perf_counter() - t0) * 1000
    baseline_calls = inner.calls

    # 有缓存
    memo = MemoizedIntentLayer(inner)
    t0 = time.perf_counter()
    asyncio.run(run_all(memo))
    memo_ms = (time.perf_counter() - t0) * 1000
    source_calls = inner.calls - baseline_calls
    hit_count = total - source_calls

    print(f"    无缓存总耗时：{raw_ms:.0f}ms（{baseline_calls} 次 encode，"
          f"{raw_ms / total:.1f}ms/次）")
    print(f"    有缓存总耗时：{memo_ms:.0f}ms（{source_calls} 次回源 + {hit_count} 次命中）")
    print(f"    命中率：{memo.hit_rate:.0%}（理论 {1 - 1/repeats:.0%}）  "
          f"总提速：{raw_ms / memo_ms:.1f}x")
    # 单次命中延迟（复用同一事件循环，量出纯缓存查找成本）
    async def hit_latency():
        t0 = time.perf_counter()
        for _ in range(1000):
            await memo.evaluate(RouteRequest(user_id="u", session_id="s",
                                             utterance=utterances[0], labels=tuple(LABELS)))
        return (time.perf_counter() - t0) * 1000 / 1000 * 1000
    hit_us = asyncio.run(hit_latency())
    print(f"    单次命中延迟：{hit_us:.0f}µs（纯 await 路径，无建循环开销）")


async def _eval_once(layer, utterance: str):
    req = RouteRequest(user_id="u", session_id="s", utterance=utterance,
                       labels=tuple(LABELS))
    return await layer.evaluate(req)


def bench_hardrule_overhead() -> None:
    print("=" * 64)
    print("4) 硬规则层：正则直算 vs 记忆化包装（衡量包装本身的开销）")
    rules = [HardRule(pattern=f"天气|气温|下雨|温度|降雨|风速|湿度|预报|伞|雨衣{i}",
                      label="ask_weather", priority=i) for i in range(20)]
    inner = HardRuleLayer(rules)
    memo = MemoizedIntentLayer(inner)
    req = RouteRequest(user_id="u", session_id="s", utterance="今天气温多少度",
                       labels=tuple(LABELS))

    async def many(layer, n):
        for _ in range(n):
            await layer.evaluate(req)

    t0 = time.perf_counter()
    asyncio.run(many(inner, 1000))
    raw_us = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    asyncio.run(many(memo, 1000))
    memo_us = (time.perf_counter() - t0) * 1000
    print(f"    正则直算 1000 次：{raw_us:.1f}ms（{raw_us} µs/次）")
    print(f"    记忆化   1000 次：{memo_us:.1f}ms（{memo_us} µs/次，含首算）")
    print(f"    结论：硬规则层本身 ~µs 级，包装开销同量级——缓存对它收益有限，"
          f"主要价值在向量层与垂类模型层")


if __name__ == "__main__":
    bench_tool_concurrency()
    bench_load_stability()
    bench_route_cache()
    bench_hardrule_overhead()
