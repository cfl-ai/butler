"""修复闭环的装配与驱动守护（U80）。

这里挡的是三种**比「功能没做完」更难发现**的退化，它们都会让测试更绿：

1. **类齐全但没人装配**（E-77 的第四种形态）：`EvaluationPlatformAgent`、
   `HarnessAgent`、`StreamTraceProducer` 各有实现、各有测试，但全项目没有
   一处把它们装在一起——闭环只在测试里存在过。
2. **复评在没重载模块的进程里跑**（E-83）：补丁已落盘，被测模块还是旧对象，
   于是稳定地报「白改」。看起来像修复无效，实际是评错了对象。
3. **`revise` 被当成续写**：真实管道弃答时会发 `revise`，那条事件带的是
   用户真正看到的那一版；把它拼在初稿后面，痕迹里就出现一段谁也没说过的答案。

第 2 条有对照组（`test_closed_loop_reports_no_effect_when_pipeline_not_rebuilt`）：
没有它，第 1 条那个「通过」可能只是碰巧。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Sequence

from ai_butler.agents.eval_platform import EvaluationPlatformAgent
from ai_butler.agents.repair_loop import (
    CHAT_PIPELINE_MODULES,
    PipelineHolder,
    RepairLoop,
    build_pipeline_producer,
    build_repair_loop,
    reload_targets,
)
from ai_butler.evaluation.platform import EvaluationPlatform
from ai_butler.evaluation.producer import StreamTraceProducer
from ai_butler.evaluation.recheck import RecheckVerdict
from ai_butler.evaluation.trace import AgentTrace, RetrievalRecord
from ai_butler.harness.patch import PatchApplier, parse_unified_diff

# --------------------------------------------------------------------------- #
# 桩：事件流 / 两个 Agent 的替身
# --------------------------------------------------------------------------- #
class Ev:
    """形如 `retrieval.chat.ChatEvent` 的最小事件。"""

    def __init__(self, type: str, text: str = "", data: dict | None = None) -> None:
        self.type = type
        self.text = text
        self.data = data or {}


def _stream(events: Sequence[Ev], counter: list[int] | None = None):
    async def factory(_query: str):
        if counter is not None:
            counter.append(1)
        for event in events:
            yield event
    return factory


class StubEvaluator:
    """按需产出「第一轮报告」与「复评结论」的替身。"""

    def __init__(self, *, case_count: int = 1, passed: int = 0,
                 defects: Sequence[Any] = (), blocked: bool = False,
                 recheck: dict | None = None) -> None:
        self.case_count = case_count
        self.passed = passed
        self.defects = list(defects)
        self.blocked = blocked
        self.recheck_payload = dict(recheck or {})
        self.calls: list[str] = []

    def run(self, _state: dict) -> dict[str, Any]:
        self.calls.append("evaluate")
        return {"eval_platform": {
            "user_id": "system:p10-repair",
            "case_count": self.case_count,
            "trace_count": self.case_count,
            "summary": {"passed": self.passed,
                        "failed": self.case_count - self.passed,
                        "mean_score": 0.42,
                        "defect_count": len(self.defects)},
            "defects": {"defects": self.defects,
                        "case_count": self.case_count,
                        "blocked_by_security": self.blocked},
            "reports": [],
        }}

    async def recheck(self, _state: dict) -> dict[str, Any]:
        self.calls.append("recheck")
        return {"eval_platform_recheck": dict(self.recheck_payload)}


class StubHarness:
    def __init__(self, *, ran: bool = True, applied: bool = True,
                 diff: str = "", converged: bool = False,
                 reason: str = "") -> None:
        self.ran = ran
        self.applied = applied
        self.diff = diff
        self.converged = converged
        self.reason = reason
        self.calls: list[str] = []

    async def run(self, _state: dict) -> dict[str, Any]:
        self.calls.append("repair")
        return {"p10_harness": {
            "ran": self.ran,
            "reason": "" if self.ran else self.reason,
            "converged": self.converged,
            "patch": {"applied": self.applied, "diff": self.diff,
                      "coder_wired": True},
            "patch_not_applied": {
                "applied": self.applied,
                "reason": "" if self.applied else (self.reason or "没产出补丁"),
            },
        }}


def _defect(kind: str = "recall_gap") -> dict[str, Any]:
    return {"defect_id": "d1", "case_id": "c1", "kind": kind,
            "dimension": "retrieval_recall", "severity": "major",
            "disposition": "auto", "title": "兜底分类错了",
            "evidence": "无信号时返回 SMALL_TALK",
            "target_hint": "ai_butler/retrieval/planner.py"}


def _outcome(loop: RepairLoop, *, cases=None, traces=None, extra=None):
    return asyncio.run(loop.run(
        cases=cases or [], traces=traces or [], extra=extra))


# --------------------------------------------------------------------------- #
# 一、痕迹生产端
# --------------------------------------------------------------------------- #
def test_revise_replaces_draft_instead_of_concatenating():
    """复检弃答 → 痕迹里的答案是弃答文本，**不是「初稿 + 弃答」**。"""
    producer = StreamTraceProducer(stream_factory=_stream([
        Ev("chunk", text="初稿：应该是 30 天"),
        Ev("revise", text="资料里没有依据，我不能凭印象回答。"),
        Ev("done", data={"retrieved_ids": ["doc-1"]}),
    ]), runs=1)
    traces = asyncio.run(producer.produce(_case()))
    assert traces[0].answer == "资料里没有依据，我不能凭印象回答。"
    assert "初稿" not in traces[0].answer


def test_missing_done_marks_run_incomplete():
    producer = StreamTraceProducer(stream_factory=_stream([
        Ev("chunk", text="说到一半")]), runs=1)
    assert asyncio.run(producer.produce(_case()))[0].completed is False


def test_before_run_runs_once_per_case_not_per_run():
    """一个用例的重跑组只重建一次：否则 reload 的次数取决于 `runs`。"""
    holder_calls: list[int] = []
    producer = StreamTraceProducer(
        stream_factory=_stream([Ev("chunk", text="答案"),
                                Ev("done", data={})]),
        runs=3, before_run=lambda: holder_calls.append(1))
    asyncio.run(producer.produce(_case()))
    assert len(holder_calls) == 1
    assert producer.refreshes == 1


def test_before_run_failure_propagates_instead_of_faking_stale_trace():
    """重建失败就抛出去（复评会判 UNVERIFIED），不拿旧模块的痕迹充数。"""
    def boom() -> None:
        raise RuntimeError("重建失败")

    producer = StreamTraceProducer(
        stream_factory=_stream([Ev("chunk", text="旧代码的答案")]),
        runs=1, before_run=boom)
    try:
        asyncio.run(producer.produce(_case()))
    except RuntimeError as exc:
        assert "重建失败" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("重建失败时必须抛异常，不能产出痕迹")


# --------------------------------------------------------------------------- #
# 二、装配点
# --------------------------------------------------------------------------- #
def test_assembly_point_wires_real_pipeline_producer():
    """给了 builder 就必须装上真实管道的生产端（这是 E-77 的疫苗）。"""
    holder = {"n": 0}

    class Pipe:
        async def stream(self, *, user_id, query):
            yield Ev("chunk", text=f"{user_id}:{query}")
            yield Ev("done", data={})

    def builder():
        holder["n"] += 1
        return Pipe()

    loop = build_repair_loop(builder=builder, modules=())
    producer = loop.evaluator.producer
    assert isinstance(producer, StreamTraceProducer)
    assert isinstance(producer.stream_factory, PipelineHolder)
    # 装配期就建一次：builder 里的错误当场暴露，而不是复评跑到一半才炸
    assert holder["n"] == 1
    # 默认重载链是对话管道本身（本用例传空以避开在测试进程里 reload 真模块）
    assert CHAT_PIPELINE_MODULES[-1] == "ai_butler.retrieval.chat"
    assert producer.stream_factory.base_modules == ()


def test_assembly_point_without_builder_leaves_producer_off():
    """不给管道就不给生产端——**刻意的默认**，不是忘了接。"""
    loop = build_repair_loop()
    assert loop.evaluator.producer is None
    assert getattr(loop.harness, "coder", None) is None


def test_builder_error_fails_loudly_at_assembly_time():
    def broken():
        raise ImportError("管道依赖缺失")

    try:
        build_repair_loop(builder=broken, modules=())
    except ImportError as exc:
        assert "管道依赖缺失" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("装配期就应当报错")


def test_both_producer_and_builder_is_a_configuration_error():
    """不安静地挑一个：挑错了复评跑的就是另一条管道，结论看着正常但不对。"""
    try:
        build_repair_loop(producer=StreamTraceProducer(stream_factory=_stream([])),
                          builder=lambda: None, modules=())
    except ValueError as exc:
        assert "只能给一个" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("同时给 producer 与 builder 必须报错")


def test_reload_targets_put_touched_modules_before_pipeline():
    """补丁改在管道的**依赖**上时，依赖必须先 reload（E-85）。"""
    diff = ("--- a/ai_butler/scheduler/router.py\n"
            "+++ b/ai_butler/scheduler/router.py\n"
            "@@ -1 +1 @@\n-a = 1\n+a = 2\n")
    targets = reload_targets(diff)
    assert targets[0] == "ai_butler.scheduler.router"
    assert "ai_butler.retrieval.chat" in targets
    assert targets.index("ai_butler.scheduler.router") < \
        targets.index("ai_butler.retrieval.chat")


def test_reload_targets_falls_back_to_pipeline_when_diff_unparsable():
    assert reload_targets("这不是补丁") == CHAT_PIPELINE_MODULES


# --------------------------------------------------------------------------- #
# 三、驱动：顺序与四种停站
# --------------------------------------------------------------------------- #
def test_loop_order_is_evaluate_repair_recheck():
    evaluator = StubEvaluator(defects=[_defect()],
                              recheck={"verdict": "accepted", "accepted": True,
                                       "reason": "修好了"})
    harness = StubHarness(diff="X" * 10)
    loop = RepairLoop(evaluator=evaluator, harness=harness)
    asyncio.run(loop.run(cases=[], traces=[]))
    assert evaluator.calls == ["evaluate", "recheck"]
    assert harness.calls == ["repair"]
    assert evaluator.calls[0] == "evaluate"


def test_loop_stops_when_first_round_has_no_defect():
    """首轮全绿 → 不调 harness，也不能自称「修好了」。"""
    evaluator = StubEvaluator(case_count=2, passed=2, defects=[])
    harness = StubHarness()
    outcome = _outcome(RepairLoop(evaluator=evaluator, harness=harness))
    assert harness.calls == []
    assert outcome.clean is True
    assert outcome.accepted is False
    assert outcome.verdict == ""
    assert [s.name for s in outcome.stages] == ["evaluate"]


def test_loop_stops_when_nothing_was_evaluated():
    """0 个用例时缺陷数必然是 0，看着最干净——必须与「全过」分开。"""
    outcome = _outcome(RepairLoop(evaluator=StubEvaluator(case_count=0),
                                  harness=StubHarness()))
    assert outcome.clean is False
    assert outcome.requires_manual is True
    assert "没测" in outcome.reason


def test_loop_stops_on_security_block_without_touching_code():
    evaluator = StubEvaluator(defects=[_defect("security_violation")], blocked=True)
    harness = StubHarness()
    outcome = _outcome(RepairLoop(evaluator=evaluator, harness=harness))
    assert harness.calls == []
    assert outcome.requires_manual is True
    assert "安全" in outcome.reason


def test_loop_stops_when_no_patch_applied():
    """没改代码就没有「改动效果」可评：直接停，不跑一次注定白跑的复评。"""
    evaluator = StubEvaluator(defects=[_defect()])
    harness = StubHarness(applied=False, reason="模型判断不该改代码：SKIP:")
    outcome = _outcome(RepairLoop(evaluator=evaluator, harness=harness))
    assert evaluator.calls == ["evaluate"]
    assert outcome.requires_manual is True
    assert "SKIP" in outcome.reason


def test_loop_stops_when_harness_never_ran():
    evaluator = StubEvaluator(defects=[_defect()])
    harness = StubHarness(ran=False, reason="未接入沙箱")
    outcome = _outcome(RepairLoop(evaluator=evaluator, harness=harness))
    assert evaluator.calls == ["evaluate"]
    assert outcome.requires_manual is True
    assert "未接入沙箱" in outcome.reason


def test_loop_does_not_treat_harness_convergence_as_verdict():
    """harness 自称收敛，复评说白改 → 结论只能是白改。"""
    evaluator = StubEvaluator(defects=[_defect()],
                              recheck={"verdict": "rejected_no_effect",
                                       "accepted": False, "reason": "一个都没修好"})
    harness = StubHarness(diff="X" * 20, converged=True)
    outcome = _outcome(RepairLoop(evaluator=evaluator, harness=harness))
    assert outcome.accepted is False
    assert outcome.verdict == "rejected_no_effect"
    assert outcome.repair["converged"] is True       # 只作记录


def test_loop_without_producer_is_unverified_not_accepted():
    """真实评测中台、没有生产端：结论必须是「不知道」。"""
    evaluator = EvaluationPlatformAgent(producer=None, rechecker=None)
    harness = StubHarness(diff=_DEMO_DIFF)
    loop = RepairLoop(evaluator=evaluator, harness=harness)
    outcome = _outcome(loop, cases=[_case_dict()], traces=[])
    # 没有痕迹 → 首轮失败 → 走到复评 → 没有生产端 → unverified
    assert outcome.verdict == RecheckVerdict.UNVERIFIED.value
    assert outcome.accepted is False
    assert outcome.requires_manual is True


# --------------------------------------------------------------------------- #
# 四、端到端：补丁落盘 → 重建管道 → 复评
# --------------------------------------------------------------------------- #
_DEMO_SOURCE = '''"""复评用的最小被测模块。"""
CATEGORY = "small_talk"


class _Event:
    def __init__(self, type, text="", data=None):
        self.type = type
        self.text = text
        self.data = data or {}


class Pipe:
    """管道替身：分类在构造时绑定，模拟 chat.py 的 import 期绑定。"""

    def __init__(self):
        self.category = CATEGORY

    async def stream(self, *, user_id, query):
        yield _Event("chunk", text="分类=" + self.category)
        yield _Event("done", data={"retrieved_ids": ["doc-1"]})
'''

_DEMO_DIFF = (
    "--- a/ai_butler/retrieval/demo_probe.py\n"
    "+++ b/ai_butler/retrieval/demo_probe.py\n"
    "@@ -1,2 +1,2 @@\n"
    ' """复评用的最小被测模块。"""\n'
    '-CATEGORY = "small_talk"\n'
    '+CATEGORY = "semantic"\n'
)

_CASE_DICT: dict[str, Any] = {
    "case_id": "c1",
    "question": "那个啥来着",
    "expected_answer": "分类=semantic",
    "expected_points": ["分类=semantic"],
    "relevant_ids": ["doc-1"],
    "evidence_texts": {"doc-1": "分类=semantic 表示走语义检索。"},
}


def _case_dict() -> dict[str, Any]:
    return dict(_CASE_DICT)


def _case():
    from ai_butler.evaluation.platform import EvaluationCase
    return EvaluationCase.from_dict(_CASE_DICT)


def _trace(answer: str) -> AgentTrace:
    return AgentTrace(trace_id="c1#run1", question=_CASE_DICT["question"],
                      answer=answer,
                      retrieval=RetrievalRecord(query=_CASE_DICT["question"],
                                                retrieved_ids=("doc-1",)))


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    pkg = root / "ai_butler" / "retrieval"
    pkg.mkdir(parents=True)
    (pkg / "demo_probe.py").write_text(_DEMO_SOURCE, encoding="utf-8")
    return root


def _install_module(root: Path, name: str) -> None:
    path = root / "ai_butler" / "retrieval" / "demo_probe.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


def _before_reports():
    """修复前的基线：同样跑两次，答案是错的版本。"""
    platform = EvaluationPlatform()
    case = _case()
    bad = _trace("分类=small_talk")
    return [platform.evaluate([bad, bad], case)]


def _recheck_state(diff: str) -> dict[str, Any]:
    return {
        "user_id": "system:p10-repair",
        "eval_cases": [_case_dict()],
        "eval_platform": {"reports": [r.as_dict() for r in _before_reports()]},
        "p10_harness": {
            "converged": True, "patch": {"applied": True, "diff": diff},
            "intake": {"tickets": [{"defect": {"kind": "recall_gap"}}]},
        },
    }


def test_closed_loop_accepts_after_patch_and_rebuild(tmp_path):
    """补丁落盘 → 重建管道 → 复评看到新行为 → accepted。"""
    root = _make_repo(tmp_path)
    name = "demo_probe_ok"
    _install_module(root, name)
    try:
        producer = build_pipeline_producer(
            builder=lambda: sys.modules[name].Pipe(), runs=2, modules=(name,))
        # 落盘（真文件）
        applier = PatchApplier(root=root)
        assert applier.apply(parse_unified_diff(_DEMO_DIFF)).ok
        applier.commit()

        agent = EvaluationPlatformAgent(producer=producer)
        out = asyncio.run(agent.recheck(_recheck_state(_DEMO_DIFF)))[
            "eval_platform_recheck"]
        assert out["verdict"] == RecheckVerdict.ACCEPTED.value, out["reason"]
        assert out["fixed"] == ["c1"]
        assert producer.refreshes >= 1
        assert name in producer.stream_factory.reloaded
    finally:
        sys.modules.pop(name, None)


def test_closed_loop_reports_no_effect_when_pipeline_not_rebuilt(tmp_path):
    """对照组：同样落盘、同样用例，只是**没重建** → 稳定地报白改。

    这一条不为「通过」而存在：它证明上一条的 accepted 是重建换来的，
    不是评测标准太松。E-83 就是这个形状。
    """
    root = _make_repo(tmp_path)
    name = "demo_probe_stale"
    _install_module(root, name)
    try:
        stale = sys.modules[name].Pipe()
        producer = StreamTraceProducer(stream_factory=lambda q: _canned(stale, q),
                                      runs=2)
        applier = PatchApplier(root=root)
        assert applier.apply(parse_unified_diff(_DEMO_DIFF)).ok
        applier.commit()

        agent = EvaluationPlatformAgent(producer=producer)
        out = asyncio.run(agent.recheck(_recheck_state(_DEMO_DIFF)))[
            "eval_platform_recheck"]
        assert out["verdict"] == RecheckVerdict.REJECTED_NO_EFFECT.value
        # 理由里必须给出排查方向，否则会被误读成「补丁没用」
        assert "旧代码" in out["reason"]
    finally:
        sys.modules.pop(name, None)


def _canned(pipe: Any, query: str):
    return pipe.stream(user_id="system:p10-recheck", query=query)
