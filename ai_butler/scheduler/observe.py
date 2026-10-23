"""P3 全链路可观测：Trace ID 贯穿、关键节点埋点、耗时统计。

计划书依据
----------
- 三、P3「全链路可观测：**Trace ID 贯穿**、关键节点埋点、耗时统计」
- 三、P3「全链路降级与回滚机制」

贯穿方式
--------
Trace ID 存放在 ContextVar 中，因此**无需逐层传参**即可在中间件、路由层、
主状态机、子 Agent 中读到同一个 ID。需要跨线程/跨任务时由调用方显式透传。
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol, Sequence, runtime_checkable

TRACE_HEADER = "X-Trace-Id"

_trace_id: ContextVar[str] = ContextVar("aibutler_trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    return _trace_id.get()


def bind_trace_id(value: str | None = None) -> Token:
    return _trace_id.set(value or new_trace_id())


def reset_trace_id(token: Token) -> None:
    _trace_id.reset(token)


@contextmanager
def trace_context(value: str | None = None) -> Iterator[str]:
    token = bind_trace_id(value)
    try:
        yield current_trace_id()
    finally:
        reset_trace_id(token)


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


@dataclass(frozen=True)
class Span:
    """一个关键节点的埋点记录。"""

    trace_id: str
    node: str
    seq: int
    elapsed_ms: int
    ok: bool = True
    note: str = ""
    started_at: str = ""
    fields: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "node": self.node, "seq": self.seq,
                "elapsed_ms": self.elapsed_ms, "ok": self.ok, "note": self.note,
                "started_at": self.started_at, "fields": dict(self.fields)}


@runtime_checkable
class Observer(Protocol):
    def on_span(self, span: Span) -> None:
        ...

    def on_trace(self, trace: "Trace") -> None:
        ...


class NullObserver:
    def on_span(self, span: Span) -> None:
        return None

    def on_trace(self, trace: "Trace") -> None:
        return None


class ListObserver:
    """收集型观察者，供测试与本地排障。"""

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.traces: list[Trace] = []

    def on_span(self, span: Span) -> None:
        self.spans.append(span)

    def on_trace(self, trace: "Trace") -> None:
        self.traces.append(trace)


class LoggingObserver:
    """把埋点写进结构化日志。默认走脱敏 logger（user_id 保留，其余隐私字段掩码）。"""

    def __init__(self, logger=None) -> None:
        if logger is None:
            from ai_butler.core.log_safety import get_logger
            logger = get_logger("trace")
        self.logger = logger

    def on_span(self, span: Span) -> None:
        self.logger.info("span", extra={"trace_id": span.trace_id, "node": span.node,
                                        "elapsed_ms": span.elapsed_ms, "ok": span.ok,
                                        "note": span.note})

    def on_trace(self, trace: "Trace") -> None:
        self.logger.info("trace", extra=trace.summary())


class Trace:
    """一次请求的全链路轨迹。"""

    def __init__(self, *, user_id: str, session_id: str = "", trace_id: str = "",
                 observer: Observer | None = None) -> None:
        self.trace_id = trace_id or current_trace_id() or new_trace_id()
        self.user_id = user_id
        self.session_id = session_id
        self.observer = observer or NullObserver()
        self.spans: list[Span] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._seq = 0

    # ---------------- 埋点 ---------------- #
    @contextmanager
    def span(self, node: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """关键节点埋点：自动统计耗时，异常也会被记录且**原样抛出**。"""
        started = _now_ms()
        box: dict[str, Any] = {"note": "", "fields": dict(fields)}
        ok, note = True, ""
        try:
            yield box
        except Exception as exc:                    # noqa: BLE001 — 记录后继续上抛
            ok, note = False, f"{type(exc).__name__}: {exc}"
            raise
        finally:
            box.setdefault("fields", {}).update(fields)
            self.add(node, elapsed_ms=int(_now_ms() - started), ok=ok,
                     note=note or str(box.get("note") or ""), fields=box.get("fields") or {})

    def add(self, node: str, *, elapsed_ms: int = 0, ok: bool = True, note: str = "",
            fields: dict[str, Any] | None = None) -> Span:
        self._seq += 1
        span = Span(trace_id=self.trace_id, node=node, seq=self._seq,
                    elapsed_ms=max(0, int(elapsed_ms)), ok=ok, note=note,
                    started_at=datetime.now(timezone.utc).isoformat(),
                    fields=dict(fields or {}))
        self.spans.append(span)
        self.observer.on_span(span)
        return span

    # ---------------- 汇总 ---------------- #
    @property
    def total_ms(self) -> int:
        return sum(s.elapsed_ms for s in self.spans)

    @property
    def failures(self) -> tuple[Span, ...]:
        return tuple(s for s in self.spans if not s.ok)

    def slowest(self, limit: int = 5) -> tuple[Span, ...]:
        return tuple(sorted(self.spans, key=lambda s: -s.elapsed_ms)[:limit])

    def summary(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id, "user_id": self.user_id,
            "session_id": self.session_id, "started_at": self.started_at,
            "span_count": len(self.spans), "total_ms": self.total_ms,
            "failure_count": len(self.failures),
            "nodes": [s.node for s in self.spans],
            "slowest": [(s.node, s.elapsed_ms) for s in self.slowest(3)],
        }

    def finish(self) -> dict[str, Any]:
        summary = self.summary()
        self.observer.on_trace(self)
        return summary


# --- 当前 Trace 的上下文绑定：主状态机节点无需把 Trace 塞进 State 契约即可埋点 --- #
_trace: ContextVar["Trace | None"] = ContextVar("aibutler_trace", default=None)


def current_trace() -> "Trace | None":
    return _trace.get()


@contextmanager
def trace_scope(trace: "Trace") -> Iterator["Trace"]:
    token = _trace.set(trace)
    id_token = _trace_id.set(trace.trace_id)
    try:
        yield trace
    finally:
        _trace_id.reset(id_token)
        _trace.reset(token)


def summarize_spans(spans: Sequence[Span]) -> dict[str, Any]:
    """按节点聚合耗时统计。"""
    buckets: dict[str, list[int]] = {}
    for span in spans:
        buckets.setdefault(span.node, []).append(span.elapsed_ms)
    return {
        node: {"count": len(values), "total_ms": sum(values),
               "max_ms": max(values), "avg_ms": round(sum(values) / len(values), 1)}
        for node, values in sorted(buckets.items())
    }
