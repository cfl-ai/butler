"""P2 append-only 会话事件账本（裁定 U57）。

计划书依据
----------
**计划书未定义「事件日志 / 轨迹 / 回放 / 恢复」任何机制**——按项目铁律登记为
文档未定义项（U57，实现参数），来源走「开源借鉴」：

- **Event Sourcing（事件溯源）**：不存「当前状态」，存「到达状态的全部事件」；
  状态 = 事件的 fold。追加后不可改不可删，这是回放与恢复能成立的前提。
- **dsh（DeepSeek Harness）对照设计**：append-only 日志 + 崩溃后重放续跑。

为什么不是「又写一份日志」
------------------------
`devkit/log_query.py` 是**运维日志**（排障/标注/缺陷统计），`scheduler/observe.py`
的 Trace 是**内存级**埋点（进程亡即失，没有持久化、没有回放、没有恢复）。
三者职责不同，本模块补的是缺口：**会话级、可回放、可恢复、可验真**的事件账本。
P9 评测中台（evaluation/trace.py 的五类过程痕迹）与这里的事件类型逐一对齐，
账本同时是评测的数据源。

三条硬语义
---------
1. **append-only**：唯一写入口是 `append()`；存储层只暴露 load/append，
   没有任何修改与删除 API。想改历史只能追加一条新事件（补偿语义）。
2. **可验真**：每条事件带 seq（会话内连续）与 hash（sha256 链式），
   `verify()` 断裂即拒——回放/恢复**前**必须先过校验，fail-closed。
3. **先脱敏后入账**：payload 一律过 Redactor（redact_deep），
   明文隐私不许越过这一步（与 harness/context.py 的顺序纪律同构）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from ai_butler.evaluation.redaction import Redactor

#: 事件类型。前五类与 evaluation/trace.py 的过程痕迹逐一对齐（评测数据源），
#: 其余为账本自身的生命周期事件。
EV_RETRIEVAL = "retrieval"
EV_TOOL_CALL = "tool_call"
EV_LLM_CALL = "llm_call"
EV_EXCEPTION = "exception"
EV_CLAIMED_OUTCOME = "claimed_outcome"
EV_SPAN = "span"                # observe.Span 落账（scheduler 埋点）
EV_STAGE = "stage"              # harness 四阶循环的阶段记录
EV_CONTEXT_SHADOW = "context_shadow"   # 上下文压缩让位记录（U58）
EV_SESSION_START = "session_start"
EV_SESSION_END = "session_end"
EV_CHECKPOINT = "checkpoint"

EVENT_TYPES: tuple[str, ...] = (
    EV_RETRIEVAL, EV_TOOL_CALL, EV_LLM_CALL, EV_EXCEPTION, EV_CLAIMED_OUTCOME,
    EV_SPAN, EV_STAGE, EV_CONTEXT_SHADOW, EV_SESSION_START, EV_SESSION_END,
    EV_CHECKPOINT,
)


class JournalError(RuntimeError):
    """账本不可信（链断裂 / 序号断档 / 篡改）。fail-closed：拒绝对外服务。"""


def _canonical(payload: Mapping) -> str:
    """确定性序列化：键排序 + 紧凑分隔符。哈希与存储都用它。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(seq: int, event_id: str, etype: str, payload: str,
            occurred_at: str, prev_hash: str) -> str:
    material = f"{seq}|{event_id}|{etype}|{payload}|{occurred_at}|{prev_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    """一条不可变事件。seq 在会话内连续；hash 链式绑定前序。"""

    seq: int
    event_id: str
    journal_id: str
    type: str
    payload: dict[str, Any]
    occurred_at: str
    prev_hash: str
    hash: str

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "event_id": self.event_id,
                "journal_id": self.journal_id, "type": self.type,
                "payload": dict(self.payload), "occurred_at": self.occurred_at,
                "prev_hash": self.prev_hash, "hash": self.hash}


class JournalStore(Protocol):
    """存储端口：只许 load 与 append。**没有 update/delete**——这就是 append-only。"""

    def load(self, journal_id: str) -> list[dict[str, Any]]:
        ...

    def append(self, journal_id: str, record: dict[str, Any]) -> None:
        ...


class MemoryJournalStore:
    """内存实现：测试与单进程本地排障用。进程亡即失。"""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = {}

    def load(self, journal_id: str) -> list[dict[str, Any]]:
        return list(self._data.get(journal_id, ()))

    def append(self, journal_id: str, record: dict[str, Any]) -> None:
        self._data.setdefault(journal_id, []).append(dict(record))


class FileJournalStore:
    """jsonl 落盘实现：一行一事件，fsync 后返回。

    位置由调用方给（`path` 是文件）；不自行决定目录，避免与 vault 用户隔离域
    （core/storage.py）的职责纠缠。测试用 tmp_path，不碰真实目录。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, journal_id: str) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise JournalError(
                        f"{self.path}:{lineno} 不是合法 JSON——文件被外部损坏，"
                        f"拒绝加载（fail-closed）") from exc
                if rec.get("journal_id") != journal_id:
                    continue
                out.append(rec)
        return out

    def append(self, journal_id: str, record: dict[str, Any]) -> None:
        line = _canonical(record) + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()


@dataclass
class EventJournal:
    """一个会话的 append-only 事件账本。

    构造时从 store 恢复既有事件（崩溃重启后续写，seq 延续不重号）；
    恢复后立即校验链完整性，断裂直接抛 JournalError——**不静默续写**。
    """

    journal_id: str
    store: JournalStore
    redactor: Redactor = field(default_factory=Redactor)
    _events: list[Event] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.journal_id.strip():
            raise JournalError("journal_id 不得为空")
        records = self.store.load(self.journal_id)
        self._events = [self._rebuild(r) for r in records]
        self.verify()

    # ---------------- 写：唯一入口 ----------------
    def append(self, etype: str, payload: Mapping | None = None, *,
               occurred_at: str | None = None) -> Event:
        if self._closed:
            raise JournalError("账本已关闭，拒绝追加")
        if etype not in EVENT_TYPES:
            raise JournalError(
                f"未知事件类型 {etype!r}，已登记 {EVENT_TYPES}")
        # ---- 先脱敏后入账：明文隐私不许越过这一步 ----
        safe = self.redactor.redact_deep(dict(payload or {}))
        self.redactor.assert_clean(_canonical(safe), where=f"事件账本 {self.journal_id}")

        seq = (self._events[-1].seq + 1) if self._events else 1
        event_id = uuid.uuid4().hex
        ts = occurred_at or _now_iso()
        prev_hash = self._events[-1].hash if self._events else "0" * 64
        digest = _digest(seq, event_id, etype, _canonical(safe), ts, prev_hash)
        event = Event(seq=seq, event_id=event_id, journal_id=self.journal_id,
                      type=etype, payload=safe, occurred_at=ts,
                      prev_hash=prev_hash, hash=digest)
        self.store.append(self.journal_id, event.as_dict())
        self._events.append(event)
        return event

    def session_start(self, **fields: Any) -> Event:
        return self.append(EV_SESSION_START, fields)

    def session_end(self, **fields: Any) -> Event:
        return self.append(EV_SESSION_END, fields)

    def checkpoint(self, state: Mapping) -> Event:
        """状态快照入账。恢复 = 最后一个 checkpoint + 其后事件 fold。"""
        return self.append(EV_CHECKPOINT, {"state": dict(state)})

    def close(self) -> None:
        self._closed = True

    # ---------------- 读 ----------------
    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def last_hash(self) -> str:
        return self._events[-1].hash if self._events else "0" * 64

    # ---------------- 验真：fail-closed ----------------
    def verify(self) -> None:
        """校验 seq 连续 + hash 链完整。任何不符 → JournalError。"""
        expected_seq = 1
        prev_hash = "0" * 64
        for event in self._events:
            if event.seq != expected_seq:
                raise JournalError(
                    f"seq 断档：期望 {expected_seq}，实际 {event.seq}"
                    f"（event_id={event.event_id}）——历史被删改或存储损坏")
            recomputed = _digest(event.seq, event.event_id, event.type,
                                 _canonical(event.payload), event.occurred_at,
                                 event.prev_hash)
            if recomputed != event.hash:
                raise JournalError(
                    f"hash 不匹配：{event.event_id} 的内容或前序绑定被篡改")
            if event.prev_hash != prev_hash:
                raise JournalError(
                    f"链断裂：{event.event_id} 的 prev_hash 与前序事件不衔接")
            prev_hash = event.hash
            expected_seq += 1

    # ---------------- 回放 / 恢复 ----------------
    def replay(self, *, fold: Callable[[dict[str, Any], Event], dict[str, Any]],
               initial: dict[str, Any] | None = None) -> dict[str, Any]:
        """从最后一个 checkpoint 起重放，返回恢复出的状态。

        先 verify 再回放——**不可信的账本不对外服务**（fail-closed）。
        fold(state, event) 由调用方给出：它定义「事件如何改变状态」。
        """
        self.verify()
        state: dict[str, Any] = dict(initial or {})
        start = 0
        for i, event in enumerate(self._events):
            if event.type == EV_CHECKPOINT:
                state = dict(event.payload.get("state") or {})
                start = i + 1
        for event in self._events[start:]:
            state = fold(state, event)
        return state

    # ---------------- 内部 ----------------
    @staticmethod
    def _rebuild(record: dict[str, Any]) -> Event:
        try:
            return Event(
                seq=int(record["seq"]), event_id=str(record["event_id"]),
                journal_id=str(record["journal_id"]), type=str(record["type"]),
                payload=dict(record.get("payload") or {}),
                occurred_at=str(record["occurred_at"]),
                prev_hash=str(record["prev_hash"]), hash=str(record["hash"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise JournalError(f"事件记录缺字段或类型错误：{record!r}") from exc


# ---------------- 与 scheduler/observe.py 的接线 ----------------

def journal_observer(journal: EventJournal, *, session_fields: Mapping | None = None):
    """把内存级 Trace 的埋点桥接到持久账本。

    返回的对象实现 `observe.Observer` 协议（on_span / on_trace），
    可直接传给 `Trace(observer=...)`：span 逐条落账，trace 结束时落一条汇总。
    延迟 import observe：账本本身不依赖调度器。
    """
    from ai_butler.scheduler.observe import Observer

    extra = dict(session_fields or {})

    class _JournalObserver:
        def on_span(self, span) -> None:  # noqa: ANN001 — observe.Span，避免 import 环
            journal.append(EV_SPAN, {**extra, **span.as_dict()})

        def on_trace(self, trace) -> None:  # noqa: ANN001
            journal.append(EV_SPAN, {**extra, "kind": "trace_summary",
                                     **trace.summary()})

    assert isinstance(_JournalObserver(), Observer)
    return _JournalObserver()
