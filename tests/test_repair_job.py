"""排程单元（U85）：只给调度单元，不自己起调度器。

测的重点是三种「看起来正常」的失败：

1. **失败不推进时间戳** → 调度器每次轮询都重试，一个坏掉的依赖变成
   每分钟一次模型调用 + 全量评测；
2. **允许重入** → 两名调度器同时驱动，两份补丁改同一批文件，
   而版本账只认「一轮一次提交」；
3. **时钟回拨时装死** → NTP 校正或容器迁移之后永久停摆，
   监控上显示「任务还在，只是没到点」。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ai_butler.agents.repair_job import RepairJob, build_repair_job
from ai_butler.agents.repair_loop import RepairLoop, RepairOutcome

_T0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


class _Loop:
    """闭环替身：只记调用次数，可按需要失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def run(self, **kwargs: Any) -> RepairOutcome:
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("依赖挂了")
        return RepairOutcome(user_id="system:p10-repair", reason="跑完了",
                             rounds_run=1)


def _job(*, interval: float = 3600, fail: bool = False,
         clock=lambda: _T0) -> tuple[RepairJob, _Loop]:
    loop = _Loop(fail=fail)
    job = RepairJob(loop=loop, interval_seconds=interval,
                    cases=({"case_id": "c1", "question": "q1"},), clock=clock)
    return job, loop


# --------------------------------------------------------------------------- #
# 一、该不该跑
# --------------------------------------------------------------------------- #
def test_first_run_is_always_due():
    """首轮不该等一个间隔：否则部署后要空等一整个周期。"""
    job, _ = _job(interval=86400)
    assert job.due(_T0) is True


def test_not_due_before_the_interval():
    job, _ = _job(interval=3600)
    job.last_run_at = _T0
    assert job.due(_T0 + timedelta(minutes=59)) is False


def test_due_when_the_interval_elapsed():
    job, _ = _job(interval=3600)
    job.last_run_at = _T0
    assert job.due(_T0 + timedelta(hours=1)) is True


def test_clock_rollback_is_treated_as_due():
    """时钟回拨（NTP 校正 / 容器迁移）不可装死。

    装死的后果：任务永远「还没到点」，而监控只看到它还在注册表里。
    多跑一次的代价是一次多余评测，停摆的代价是这条链路再也不会运行。
    """
    job, _ = _job(interval=3600)
    job.last_run_at = _T0
    assert job.due(_T0 - timedelta(days=2)) is True


def test_naive_time_is_read_as_utc():
    """naive 与 aware 混着比大小会算错，而算错不会报错。"""
    job, _ = _job(interval=3600)
    job.last_run_at = _T0
    assert job.due(datetime(2026, 10, 25, 13, 0)) is True
    assert job.due(datetime(2026, 10, 25, 12, 30)) is False


# --------------------------------------------------------------------------- #
# 二、跑一次
# --------------------------------------------------------------------------- #
def test_run_records_outcome_and_time():
    job, loop = _job()
    outcome = asyncio.run(job.run(now=_T0))
    assert outcome.reason == "跑完了"
    assert job.runs == 1
    assert job.last_run_at == _T0
    assert job.last_error == ""
    assert loop.calls[0]["cases"] == [{"case_id": "c1", "question": "q1"}]


def test_failure_still_advances_the_clock():
    """失败也算跑过——不推进时间戳，一个坏依赖会变成每分钟一次重试风暴。"""
    job, _ = _job(fail=True)
    with pytest.raises(RuntimeError):
        asyncio.run(job.run(now=_T0))
    assert job.last_run_at == _T0          # 推进了
    assert "依赖挂了" in job.last_error
    assert job.runs == 1
    assert job.due(_T0 + timedelta(minutes=1)) is False


def test_next_success_clears_the_error():
    loop = _Loop(fail=True)
    job = RepairJob(loop=loop, interval_seconds=3600,
                    cases=({"case_id": "c1", "question": "q1"},))
    with pytest.raises(RuntimeError):
        asyncio.run(job.run(now=_T0))
    loop.fail = False
    asyncio.run(job.run(now=_T0 + timedelta(hours=1)))
    assert job.last_error == ""


def test_passes_the_undo_ticket_through():
    """人工批准的撤销票必须在**下一次**运行里用掉（票一次性）。"""
    job, loop = _job()
    asyncio.run(job.run(now=_T0, undo_approval="ticket-1", undo_rounds=["R3"]))
    assert loop.calls[0]["undo_approval"] == "ticket-1"
    assert loop.calls[0]["undo_rounds"] == ["R3"]


# --------------------------------------------------------------------------- #
# 三、防护
# --------------------------------------------------------------------------- #
def test_reentry_is_refused():
    """两名调度器同时驱动会并发改同一批文件——第二次直接抛错。"""
    job, _ = _job()
    job._running = True
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(job.run(now=_T0))
    assert "正在运行" in str(exc.value)


def test_non_positive_interval_is_refused():
    with pytest.raises(ValueError):
        RepairJob(loop=_Loop(), interval_seconds=0,
                  cases=({"case_id": "c1", "question": "q"},))


def test_empty_cases_are_refused():
    """永远跑不出结论的定时任务，在监控上看起来像「一直正常」。"""
    with pytest.raises(ValueError) as exc:
        RepairJob(loop=_Loop(), interval_seconds=60, cases=())
    assert "没有用例" in str(exc.value)


# --------------------------------------------------------------------------- #
# 四、监控出口
# --------------------------------------------------------------------------- #
def test_snapshot_is_json_safe_and_carries_no_case_text():
    job, _ = _job()
    asyncio.run(job.run(now=_T0))
    payload = job.snapshot()
    json.dumps(payload)                          # 可序列化
    assert payload["runs"] == 1
    assert payload["last"]["reason"] == "跑完了"
    assert "question" not in json.dumps(payload, ensure_ascii=False)


def test_build_repair_job_defaults_to_one_day():
    job = build_repair_job(_Loop(), cases=[{"case_id": "c1", "question": "q"}])
    assert job.interval_seconds == 24 * 3600
    assert job.cases == ({"case_id": "c1", "question": "q"},)
    assert isinstance(job.loop, _Loop)


def test_real_loop_stops_without_traces():
    """与真闭环联跑一次：账本空 → 停在 no_traces，调度状态如实反映。"""
    from ai_butler.agents.capabilities import grant
    from ai_butler.agents.eval_platform import EvaluationPlatformAgent
    from ai_butler.evaluation.journal_source import JournalTraceSource
    from ai_butler.core.event_journal import EventJournal, MemoryJournalStore

    loop = RepairLoop(
        evaluator=EvaluationPlatformAgent(tools=grant(EvaluationPlatformAgent, {})),
        harness=_Loop(), max_rounds=1,
        source=JournalTraceSource(EventJournal(
            journal_id="s", store=MemoryJournalStore())))
    job = RepairJob(loop=loop, interval_seconds=3600,
                    cases=({"case_id": "c1", "question": "q1"},), clock=lambda: _T0)
    outcome = asyncio.run(job.run(now=_T0))
    assert outcome.requires_manual is True
    assert job.snapshot()["last"]["requires_manual"] is True
    assert job.last_run_at == _T0
