"""跨轮重试的四道约束与溯源（U81）。

上一轮把「跨轮重试」如实登记为未实现，理由是撤销一个**已提交**补丁是
独立的高危能力。这一轮补上，同时钉住四条不能省的约束——它们的共同点是
**单轮跑一万次也不会暴露，多轮第一次就中**：

1. **边界**：本轮只动本轮缺陷所属板块，且不动前几轮已改过的文件。
   同一文件叠两份自动补丁，出问题时说不清是哪一轮造成的。
2. **错误信息必须已更改**：跑满 N 轮却从未推进，只是把同一动作重复 N 次，
   代价却是 N 次模型调用 + N 次全量评测。
3. **撤销已提交补丁需人工确认**：撤销是生效过的变更，不能与「验证失败
   自动回滚」共用入口——前者的高频会让后者的门槛形同虚设。
4. **每轮可溯源**：问题在哪、改了什么、为什么这样改，三者必须能对上。

这里刻意保留几个**对照组**（`test_..._without_...`）：只证明「成功路径能过」
是不够的，得同时证明「没有这个条件时会失败」，否则那个通过可能只是判据太松。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from ai_butler.agents.capabilities import grant
from ai_butler.agents.eval_platform import EvaluationPlatformAgent
from ai_butler.agents.p10_harness import HarnessAgent
from ai_butler.agents.repair_loop import REPAIR_USER_ID, RepairLoop
from ai_butler.agents.state import new_state
from ai_butler.core.approval import ApprovalError, ApprovalService
from ai_butler.evaluation.defects import defects_from_reports
from ai_butler.evaluation.dimensions import DIMENSION_KEYS, DimensionResult
from ai_butler.evaluation.fingerprint import fingerprint_of, normalize_evidence
from ai_butler.evaluation.platform import PlatformReport, Verdict
from ai_butler.evaluation.recheck import RecheckVerdict
from ai_butler.harness.coder import CodeProposal
from ai_butler.harness.patch import PatchGuard, parse_unified_diff
from ai_butler.harness.revision import (
    CommittedPatch,
    RevisionError,
    RevisionLedger,
    undo_signature,
)
from ai_butler.harness.sandbox import SandboxResult
from ai_butler.harness.trail import RepairTrail

#: 只让「检索召回」一个维度失败的报告 —— 只产一条 AUTO 类缺陷。
#: 用 `synthetic_report` 不行：它七个维度全 fail，会带出 security_violation，
#: 于是闭环在第二轮按 U76 整批阻断，根本走不到跨轮逻辑。
_DIM = "retrieval_recall"

_PLANNER = "ai_butler/retrieval/planner.py"
_CHAT = "ai_butler/retrieval/chat.py"
#: 与 `_DIFF` 的 hunk 上下文**逐字对应**：补丁的定位靠逐行精确匹配，
#: 仓库内容随便写会让应用失败，于是「跨轮」这一组全在测应用失败。
_ORIGINAL = "return SMALL_TALK\nsecond\n"
_DIFF = (f"--- a/{_PLANNER}\n"
         f"+++ b/{_PLANNER}\n"
         "@@ -1,1 +1,1 @@\n"
         "-return SMALL_TALK\n"
         "+return UNKNOWN\n")


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #
def _report(*, fail: bool = True, evidence: str = "检索召回 1/3，期望 doc-1",
            case_id: str = "c1") -> PlatformReport:
    """一份只让 `_DIM` 维度失败的报告。"""
    dimensions = {}
    for key in DIMENSION_KEYS:
        bad = fail and key == _DIM
        dimensions[key] = DimensionResult(
            key=key, score=0.0 if bad else 1.0, passed=not bad,
            applicable=True, evidence=evidence if bad else "通过")
    return PlatformReport(
        case_id=case_id, question="那个啥来着", generated_at="",
        runs=2, dimensions=dimensions,
        overall_score=0.0 if fail else 1.0,
        verdict=Verdict.FAIL if fail else Verdict.PASS,
        vetoed=False, reason=evidence if fail else "全部通过")


def _defects_of(report: PlatformReport) -> list[dict[str, Any]]:
    """由报告导出缺陷（State 形态）。两边同源，避免手写字段对不上。"""
    return [dict(d) for d in
            defects_from_reports([report]).as_dict()["defects"]]


def _recheck(verdict: str, *, reports: Sequence[PlatformReport] = (),
             reason: str = "", fixed: Sequence[str] = (),
             remaining: Sequence[str] = ()) -> dict[str, Any]:
    return {"verdict": verdict,
            "accepted": verdict == RecheckVerdict.ACCEPTED.value,
            "reason": reason or verdict, "fixed": list(fixed),
            "regressed": [], "still_broken": [], "errored": [],
            "remaining": list(remaining),
            "reports": [r.as_dict() for r in reports]}


class ScriptedEvaluator:
    """按脚本给「本轮基线」与「复评结论」的评测中台替身。

    `section_from_reports` 转发给真的 `EvaluationPlatformAgent`：
    基线晋升的形态必须与真实实现**同一份代码**产出，否则这里测过的
    形态到了生产上不成立（E-44 那类漂移）。
    """

    def __init__(self, steps: Sequence[Mapping[str, Any]]) -> None:
        self.steps = list(steps)
        self.index = 0
        self.calls: list[str] = []
        self._probe = EvaluationPlatformAgent()

    def section_from_reports(self, **kwargs: Any) -> dict[str, Any]:
        return self._probe.section_from_reports(**kwargs)

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("evaluate")
        self.index = 0
        reports = self.steps[0]["evaluate"]
        return {"eval_platform": self._probe.section_from_reports(
            uid=REPAIR_USER_ID, reports=list(reports), case_count=1)}

    async def recheck(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("recheck")
        step = self.steps[min(self.index, len(self.steps) - 1)]
        self.index += 1
        return {"eval_platform_recheck": dict(step["recheck"])}


class StubHarness:
    """按需要「应用到 / 应用不上」的 harness 替身。"""

    def __init__(self, *, applied: bool = True, diff: str = _DIFF,
                 committed: Mapping[str, Any] | None = None,
                 ran: bool = True, reason: str = "",
                 rationale: str = "无信号时应回 UNKNOWN") -> None:
        self.applied = applied
        self.diff = diff
        self.committed = dict(committed) if committed is not None else None
        self.ran = ran
        self.reason = reason
        self.rationale = rationale
        self.calls: list[str] = []
        self.scopes: list[dict[str, Any]] = []

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("repair")
        self.scopes.append(dict(state.get("patch_scope") or {}))
        committed = self.committed
        if committed is None and self.applied and self.diff:
            committed = {"paths": [_PLANNER], "before": {_PLANNER: "old\n"},
                         "defect_ids": ["c1/retrieval_recall"],
                         "rationale": self.rationale}
        return {"p10_harness": {
            "ran": self.ran,
            "reason": "" if self.ran else self.reason,
            "converged": self.applied,
            "result": {"sandbox": [{"command": "pytest -q", "exit_code": 0}]},
            "patch": {"applied": self.applied, "diff": self.diff,
                      "coder_wired": True,
                      "code": [{"ok": self.applied, "rationale": self.rationale}],
                      "committed": committed},
            "patch_not_applied": {
                "applied": self.applied,
                "reason": "" if self.applied else (self.reason or "没产出补丁")},
        }}


def _run(loop: RepairLoop, **kwargs: Any):
    return asyncio.run(loop.run(cases=[{"case_id": "c1"}], traces=[], **kwargs))


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "ai_butler" / "retrieval").mkdir(parents=True, exist_ok=True)
    (root / _PLANNER).write_text(_ORIGINAL, encoding="utf-8")
    return root


def _committed(round_id: str, content: str = _ORIGINAL,
               path: str = _PLANNER) -> CommittedPatch:
    return CommittedPatch(round_id=round_id, change_id=f"round:{round_id}",
                          diff=_DIFF, paths=(path,), before={path: content},
                          rationale="历史补丁")


# --------------------------------------------------------------------------- #
# 一、缺陷指纹：错误信息到底变了没有
# --------------------------------------------------------------------------- #
def test_fingerprint_ignores_numeric_jitter():
    """「仍不通过 3 个」与「2 个」是同一个形态，不该被当成进展。

    把它当成进展的后果：每一轮都判定「有推进，继续」，刹车永久失效。
    """
    before = fingerprint_of([{"defect_id": "d1", "kind": "recall_gap",
                              "dimension": _DIM, "severity": "major",
                              "evidence": "仍不通过 3 个用例"}])
    after = fingerprint_of([{"defect_id": "d1", "kind": "recall_gap",
                             "dimension": _DIM, "severity": "major",
                             "evidence": "仍不通过 2 个用例"}])
    assert before.stable == after.stable
    assert after.diff_against(before).identical is True


def test_fingerprint_detects_real_change_in_wording():
    """措辞变了就是真变化：它说明这一轮的改动确实打到了别处。"""
    before = fingerprint_of([{"defect_id": "d1", "kind": "recall_gap",
                              "dimension": _DIM, "severity": "major",
                              "evidence": "检索召回 1/3"}])
    after = fingerprint_of([{"defect_id": "d1", "kind": "recall_gap",
                             "dimension": _DIM, "severity": "major",
                             "evidence": "检索召回 1/3，且结果排序把 doc-1 挤到末位"}])
    diff = after.diff_against(before)
    assert diff.identical is False
    assert [m[0] for m in diff.mutated] == ["d1"]
    assert "证据变化" in diff.render()


def test_fingerprint_tracks_resolved_and_appeared():
    before = fingerprint_of([{"defect_id": "a", "kind": "recall_gap",
                              "evidence": "x"}, {"defect_id": "b",
                                                 "kind": "quality_gap",
                                                 "evidence": "y"}])
    after = fingerprint_of([{"defect_id": "b", "kind": "quality_gap",
                             "evidence": "y"}, {"defect_id": "c",
                                                "kind": "unstable",
                                                "evidence": "z"}])
    diff = after.diff_against(before)
    assert diff.resolved == ("a",)
    assert diff.appeared == ("c",)
    assert diff.carried == ("b",)
    assert diff.moved is True


def test_fingerprint_direction_cannot_be_reversed():
    """方向写反不会报错，只会把「修好了」显示成「新出现」——所以钉住它。"""
    broken = fingerprint_of([{"defect_id": "a", "kind": "recall_gap", "evidence": "x"}])
    fixed = fingerprint_of([])
    assert fixed.diff_against(broken).resolved == ("a",)
    assert broken.diff_against(fixed).appeared == ("a",)


def test_fingerprint_keeps_target_hint_as_part_of_the_shape():
    """定位提示换文件 = 缺陷指向换了地方，那是真变化，不是措辞差异。"""
    before = fingerprint_of([{"defect_id": "d1", "kind": "recall_gap",
                              "evidence": "召回不足", "target_hint": _PLANNER}])
    after = fingerprint_of([{"defect_id": "d1", "kind": "recall_gap",
                             "evidence": "召回不足",
                             "target_hint": "ai_butler/retrieval/chat.py"}])
    assert after.diff_against(before).identical is False


def test_normalize_keeps_paths_and_identifiers():
    """归一化只吃「同一件事的不同量级」，不吃「不同的事」。"""
    text = normalize_evidence("planner.py 的 SMALL_TALK 命中率 0.31，共 1782ms")
    assert "planner.py" in text and "SMALL_TALK" in text
    assert "0.31" not in text and "1782" not in text


# --------------------------------------------------------------------------- #
# 二、版本账：撤销已提交补丁必须有人签字
# --------------------------------------------------------------------------- #
def _service(tmp_path: Path, *, ttl: float = 3600) -> ApprovalService:
    return ApprovalService(ttl_seconds=ttl)


def _approved(service: ApprovalService, round_ids: Sequence[str],
              *, by: str = "reviewer") -> str:
    ticket = service.request(tool="harness.revision.undo",
                             signature=undo_signature(round_ids),
                             agent=REPAIR_USER_ID, role="p10-harness",
                             tier=2, reason="撤销测试")
    service.approve(ticket.approval_id, by=by)
    return ticket.approval_id


def test_undo_without_ticket_is_refused(tmp_path):
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    service = _service(tmp_path)
    try:
        ledger.undo("R1", approval_id="", service=service,
                    requester=REPAIR_USER_ID, role="p10-harness")
    except ApprovalError:
        pass
    else:                                              # pragma: no cover
        raise AssertionError("无票撤销必须被拒")
    assert (root / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL


def test_undo_without_service_is_refused(tmp_path):
    """没接审批 ≠ 不用审批。缺服务就停。"""
    ledger = RevisionLedger(root=_repo(tmp_path))
    ledger.record(_committed("R1"))
    try:
        ledger.undo("R1", approval_id="whatever", service=None,
                    requester=REPAIR_USER_ID, role="p10-harness")
    except RevisionError as exc:
        assert "审批" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("未接入审批服务时必须拒绝")


def test_undo_ticket_is_bound_to_the_round(tmp_path):
    """批准撤销 R1 的票拿去撤 R2 —— 票只对它被批准的那一次有效。"""
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    ledger.record(_committed("R2"))
    service = _service(tmp_path)
    ticket = _approved(service, ["R1"])
    try:
        ledger.undo("R2", approval_id=ticket, service=service,
                    requester=REPAIR_USER_ID, role="p10-harness")
    except ApprovalError as exc:
        assert "不符" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("签名绑定必须拦住跨轮复用")


def test_undo_ticket_is_single_use(tmp_path):
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    ledger.record(_committed("R2", path=_PLANNER))
    service = _service(tmp_path)
    ticket = _approved(service, ["R1"])
    ledger.undo("R1", approval_id=ticket, service=service,
                requester=REPAIR_USER_ID, role="p10-harness")
    try:
        ledger.undo("R1", approval_id=ticket, service=service,
                    requester=REPAIR_USER_ID, role="p10-harness")
    except RevisionError as exc:
        assert "二次撤销" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("已撤销的轮次不得再撤")


def test_undo_refuses_when_file_drifted(tmp_path):
    """提交后文件又被改过 → 盲写回 before 会把别人的改动冲掉。"""
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))          # after_digest = 当前内容
    (root / _PLANNER).write_text("有人又改了一版\n", encoding="utf-8")
    service = _service(tmp_path)
    ticket = _approved(service, ["R1"])
    try:
        ledger.undo("R1", approval_id=ticket, service=service,
                    requester=REPAIR_USER_ID, role="p10-harness")
    except RevisionError as exc:
        assert "又被改动过" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("漂移必须拒，不能覆盖")
    assert (root / _PLANNER).read_text(encoding="utf-8") == "有人又改了一版\n"


def test_undo_restores_before_content(tmp_path):
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    service = _service(tmp_path)
    ticket = _approved(service, ["R1"])
    undo = ledger.undo("R1", approval_id=ticket, service=service,
                       requester=REPAIR_USER_ID, role="p10-harness",
                       reason="改坏了")
    assert undo.decided_by == "reviewer"
    assert undo.restored == (_PLANNER,)
    assert (root / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL


def test_locked_paths_unlock_only_after_undo(tmp_path):
    """「锁」与「已撤销」必须联动：撤了就等于回到第一次改的状态。"""
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    assert ledger.locked_paths() == (_PLANNER,)
    service = _service(tmp_path)
    ledger.undo("R1", approval_id=_approved(service, ["R1"]), service=service,
                requester=REPAIR_USER_ID, role="p10-harness")
    assert ledger.locked_paths() == ()
    # 历史不改写：撤销后那一轮仍在账上
    assert ledger.get("R1") is not None


def test_ledger_rejects_two_commits_in_one_round(tmp_path):
    """一轮两条记录 → 「撤销这一轮」有歧义，而歧义在自动化里等于不可执行。"""
    ledger = RevisionLedger(root=_repo(tmp_path))
    ledger.record(_committed("R1"))
    try:
        ledger.record(_committed("R1"))
    except RevisionError as exc:
        assert "一轮只能提交一次" in str(exc)
    else:                                              # pragma: no cover
        raise AssertionError("同一轮重复记账必须被拒")


# --------------------------------------------------------------------------- #
# 三、边界：只负责自己的板块
# --------------------------------------------------------------------------- #
def test_guard_blocks_files_locked_by_previous_rounds():
    guard = PatchGuard.for_kind("recall_gap", frozen_paths=(_PLANNER,))
    patch = parse_unified_diff(_DIFF)
    assert guard.frozen_hits(patch) == (_PLANNER,)
    assert any("已被前几轮" in v for v in guard.violations(patch))


def test_scope_takes_union_of_kinds_not_intersection():
    """两个板块的缺陷同时存在时取并集：交集会让这批缺陷谁都改不了。"""
    loop = RepairLoop(evaluator=ScriptedEvaluator([]), harness=StubHarness())
    batch = _batch_of([
        {"defect_id": "c1/retrieval_recall", "kind": "recall_gap",
         "dimension": _DIM, "severity": "major", "disposition": "auto",
         "evidence": "e", "target_hint": _PLANNER},
        {"defect_id": "c1/stability_and_exceptions", "kind": "unstable",
         "dimension": "stability_and_exceptions", "severity": "major",
         "disposition": "auto", "evidence": "e",
         "target_hint": "ai_butler/scheduler/tools.py"},
    ])
    scope = loop._scope("R1", batch)
    assert "ai_butler/retrieval/" in scope.allowed_prefixes
    assert "ai_butler/scheduler/" in scope.allowed_prefixes


def test_scope_flags_conflict_only_for_locked_targets(tmp_path):
    loop = RepairLoop(evaluator=ScriptedEvaluator([]), harness=StubHarness(),
                      ledger=RevisionLedger(root=_repo(tmp_path)))
    loop.ledger.record(_committed("R1"))
    batch = _batch_of(_defects_of(_report()))
    scope = loop._scope("R2", batch)
    assert scope.conflicts == (_PLANNER,)
    assert scope.undo_rounds == ("R1",)
    assert scope.needs_undo is True


def test_scope_never_treats_forbidden_paths_as_conflicts():
    """`tests/` 是永远不可改，不是「等撤销就解锁」——两者处置完全不同。"""
    loop = RepairLoop(evaluator=ScriptedEvaluator([]), harness=StubHarness(),
                      ledger=RevisionLedger())
    loop.ledger.record(CommittedPatch(
        round_id="R1", change_id="c", diff=_DIFF,
        paths=("tests/test_x.py",), before={"tests/test_x.py": ""}))
    batch = _batch_of([{
        "defect_id": "d", "kind": "quality_gap", "dimension": "output_quality",
        "severity": "minor", "disposition": "auto", "evidence": "e",
        "target_hint": "tests/test_x.py"}])
    scope = loop._scope("R2", batch)
    assert scope.conflicts == ()


# --------------------------------------------------------------------------- #
# 四、跨轮驱动
# --------------------------------------------------------------------------- #
def test_single_round_behaves_like_u80():
    """对照组：`max_rounds=1` 时与上一轮的单轮闭环等价。"""
    bad = _report()
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                            reports=[bad], reason="没修好")}])
    harness = StubHarness()
    loop = RepairLoop(evaluator=evaluator, harness=harness, max_rounds=1)
    out = _run(loop)
    assert [s.name for s in out.stages] == ["evaluate", "boundary", "repair",
                                            "recheck", "stop"]
    assert out.rounds_run == 1
    assert harness.calls == ["repair"]


def test_stops_when_error_message_did_not_change():
    """核心：错误信息一字未变 → 不跑第二轮。

    这条不生效的后果不是「多花点钱」，是**闭环永远停不下来**：
    每轮都「改了 → 没效果 → 再改」，看起来在努力，实际在重复同一个动作。
    """
    bad = _report()
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                            reports=[bad], reason="改了等于没改")}])
    harness = StubHarness()
    loop = RepairLoop(evaluator=evaluator, harness=harness, max_rounds=3)
    out = _run(loop)
    assert out.rounds_run == 1
    assert harness.calls == ["repair"]
    assert "no_progress" in out.reason
    assert evaluator.calls == ["evaluate", "recheck"]
    assert out.rounds[0]["moved"] is False
    assert "一字未变" in out.rounds[0]["error_change"]


def test_continues_when_error_message_changed():
    """证据变了（同样的缺陷、不同的表现）→ 值得再试一轮。"""
    first = _report(evidence="检索召回 1/3，期望 doc-1")
    second = _report(evidence="检索召回 1/3，且 doc-1 被排到候选末位")
    good = _report(fail=False)
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[second], reason="有推进但没修好")},
        {"recheck": _recheck(RecheckVerdict.ACCEPTED.value, reports=[good],
                             reason="修好了", fixed=["c1"])},
    ])
    harness = StubHarness()
    loop = RepairLoop(evaluator=evaluator, harness=harness, max_rounds=3)
    out = _run(loop)
    assert out.rounds_run == 2
    assert out.accepted is True
    assert harness.calls == ["repair", "repair"]
    assert len(out.rounds) == 2
    assert out.rounds[0]["moved"] is True


def test_second_round_baseline_is_promoted_from_recheck():
    """第二轮评的是「修完之后」那一版，不是修复前的旧基线。"""
    first = _report(evidence="检索召回 1/3，期望 doc-1")
    second = _report(evidence="检索召回 2/3，还差 doc-1")
    good = _report(fail=False)
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[second], reason="有推进")},
        {"recheck": _recheck(RecheckVerdict.ACCEPTED.value, reports=[good],
                             reason="修好了", fixed=["c1"])},
    ])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=3)
    out = _run(loop)
    # 第二轮的基线应当是「2/3」那一版，而不是第一轮的「1/3」
    second = loop.trail.get("R2")
    assert second is not None
    assert "2/3" in second.defects[0]["evidence"]
    assert out.rounds[1]["round_id"] == "R2"


def test_target_locked_and_uncurable_asks_for_approval(tmp_path):
    """改不下去 + 定位提示指向锁定文件 → 申请撤销票，不硬改。"""
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    first = _report(evidence="检索召回 1/3")
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[first], reason="没修好")}])
    harness = StubHarness(applied=False, reason="待改文件都已被前几轮补丁改动过")
    loop = RepairLoop(evaluator=evaluator, harness=harness, max_rounds=3,
                      ledger=ledger, approvals=ApprovalService(ttl_seconds=600))
    out = _run(loop)
    assert out.requires_approval is True
    assert out.approval_request.get("round_ids") == ["R1"]
    assert "撤销" in out.reason
    # 没有票 → 一个字都没改，也没有 eval 复评
    assert out.verdict == ""
    assert evaluator.calls == ["evaluate"]


def test_conflict_without_approval_service_stops_hard(tmp_path):
    """对照组：缺审批服务时**不能**有捷径，也不能假装撤过了。"""
    root = _repo(tmp_path)
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))
    first = _report(evidence="检索召回 1/3")
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[first], reason="没修好")}])
    loop = RepairLoop(evaluator=evaluator,
                      harness=StubHarness(applied=False,
                                          reason="待改文件都已被前几轮改动过"),
                      max_rounds=3, ledger=ledger, approvals=None)
    out = _run(loop)
    assert out.requires_approval is False
    assert out.requires_manual is True
    assert "未接入审批服务" in out.reason
    assert (root / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL


def test_approved_undo_unlocks_and_reruns(tmp_path):
    """批准后带回票 → 撤销生效、锁定解除、本轮重新开工。"""
    root = _repo(tmp_path)
    # 先让文件处于「R1 改完之后」的形态，再 record —— 否则撤销前的漂移检查
    # 会把「磁盘和提交时不一致」当成别人动过（那正是它该拦的情况）。
    (root / _PLANNER).write_text("被 R1 改过\n", encoding="utf-8")
    ledger = RevisionLedger(root=root)
    ledger.record(_committed("R1"))          # after_digest = 「被 R1 改过」
    service = ApprovalService(ttl_seconds=600)
    first = _report(evidence="检索召回 1/3")
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.ACCEPTED.value,
                             reports=[_report(fail=False)],
                             reason="修好了", fixed=["c1"])}])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(),
                      max_rounds=3, ledger=ledger, approvals=service)
    ticket = _approved(service, ["R1"])
    out = _run(loop, undo_approval=ticket, undo_rounds=["R1"])
    assert [u["round_id"] for u in out.undone] == ["R1"]
    assert out.undone[0]["decided_by"] == "reviewer"
    # 回到补丁之前的原文
    assert (root / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL
    assert out.accepted is True


def test_regression_stops_and_requests_undo_ticket():
    """改坏了 → 停，并申请一张「撤销本轮」的票（人工批了才回基线）。"""
    bad = _report()
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.REJECTED_REGRESSION.value,
                            reports=[_report(evidence="另一个用例挂了")],
                            reason="原本通过的用例不通过了")}])
    service = ApprovalService(ttl_seconds=600)
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=3,
                      approvals=service)
    out = _run(loop)
    assert out.verdict == RecheckVerdict.REJECTED_REGRESSION.value
    assert out.requires_approval is True
    assert out.approval_request.get("round_ids") == ["R1"]


def test_unverified_stops_instead_of_stacking_on_unknown():
    bad = _report()
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.UNVERIFIED.value, reason="重跑不了")}])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=3)
    out = _run(loop)
    assert out.rounds_run == 1
    assert "unverified" in out.reason
    assert out.accepted is False


def test_partially_fixed_is_not_accepted():
    """修好一半不叫解决——这是本轮对 U79 口径的修正。"""
    first = _report(evidence="检索召回 1/3")
    partial = _recheck(RecheckVerdict.PARTIALLY_FIXED.value,
                       reports=[_report(evidence="检索召回 2/3，还差一条")],
                       reason="修好了一部分", fixed=["c1"], remaining=["c2/x"])
    evaluator = ScriptedEvaluator([{"evaluate": [first], "recheck": partial}])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=1)
    out = _run(loop)
    assert out.accepted is False
    assert out.verdict == "partially_fixed"


# --------------------------------------------------------------------------- #
# 五、溯源：问题在哪、改了什么、为什么
# --------------------------------------------------------------------------- #
def test_every_round_leaves_a_record():
    first = _report(evidence="检索召回 1/3")
    second = _report(evidence="检索召回 1/3，候选排序异常")
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[second], reason="有推进")},
        {"recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[second], reason="又没推进")},
    ])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=2)
    _run(loop)
    rounds = loop.trail.rounds()
    assert [r.round_id for r in rounds] == ["R1", "R2"]


def test_round_record_holds_the_whole_chain():
    """六段齐全：问题在哪 / 边界 / 改了什么 / 为什么 / 验了什么 / 判了什么。"""
    bad = _report(evidence="检索召回 1/3")
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.ACCEPTED.value,
                            reports=[_report(fail=False)],
                            reason="修好了", fixed=["c1"])}])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=1)
    out = _run(loop)
    record = loop.trail.get("R1")
    assert record is not None
    # ① 问题在哪
    assert record.defects
    assert _PLANNER in record.defects[0]["target_hint"]
    assert record.defects[0]["evidence"] == "检索召回 1/3"
    assert record.fingerprint
    # ② 边界
    assert record.scope["allowed_prefixes"]
    # ③ 改了什么
    committed = record.patch.get("committed") or {}
    assert committed.get("paths") == [_PLANNER]
    # 溯源账里**不含源码正文**：`before` 只归版本账，账本是给人读的
    assert "before" not in committed
    # ④ 为什么这样改
    assert "UNKNOWN" in record.rationale
    # ⑤ 验了什么
    assert record.verification and "pytest" in record.verification[0]
    # ⑥ 判了什么
    assert record.verdict == RecheckVerdict.ACCEPTED.value
    assert record.fingerprint_diff["moved"] is True
    assert out.rounds[0]["round_id"] == "R1"


def test_trace_defect_follows_one_issue_across_rounds():
    """给一条缺陷 id，能答「它哪轮出现、哪轮消失、中间改了什么」。"""
    first = _report(evidence="检索召回 1/3")
    second = _report(evidence="检索召回 1/3，候选排序异常")
    good = _report(fail=False)
    evaluator = ScriptedEvaluator([
        {"evaluate": [first],
         "recheck": _recheck(RecheckVerdict.REJECTED_NO_EFFECT.value,
                             reports=[second], reason="有推进")},
        {"recheck": _recheck(RecheckVerdict.ACCEPTED.value, reports=[good],
                             reason="修好了", fixed=["c1"])},
    ])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=2)
    _run(loop)
    traces = loop.trace_defect("c1/retrieval_recall")
    assert [t.round_id for t in traces] == ["R1", "R2"]
    assert traces[0].present is True and traces[0].changed is None
    # R2 的基线里它还在（上一轮没修好），但这一轮它的证据已经变了
    assert traces[1].present is True and traces[1].changed is True
    # 它是在 R2 复评之后消失的 —— 「何时好了」与「何时还在」是两件事
    second = loop.trail.get("R2")
    assert "c1/retrieval_recall" in (second.fingerprint_diff or {}).get("resolved", ())
    # 每轮都能读出「为什么这样改」
    assert all(t.rationale for t in traces)


def test_explain_reads_like_a_story():
    bad = _report(evidence="检索召回 1/3")
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.ACCEPTED.value,
                            reports=[_report(fail=False)],
                            reason="修好了", fixed=["c1"])}])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=1)
    _run(loop)
    text = loop.explain("R1")
    for keyword in ("问题", "边界", "改动", "为什么这样改", "验证", "复评"):
        assert keyword in text, f"缺少「{keyword}」这一段：{text}"


def test_trail_persists_and_reloads(tmp_path):
    """落盘可回读：溯源不能只在内存里成立。"""
    path = tmp_path / "trail.jsonl"
    trail = RepairTrail(path=path)
    bad = _report()
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.ACCEPTED.value,
                            reports=[_report(fail=False)],
                            reason="修好了", fixed=["c1"])}])
    loop = RepairLoop(evaluator=evaluator, harness=StubHarness(), max_rounds=1,
                      trail=trail)
    _run(loop)
    assert path.exists()
    reloaded = RepairTrail(path=path)
    rounds = reloaded.load()
    assert [r.round_id for r in rounds] == ["R1"]
    assert rounds[0].verdict == RecheckVerdict.ACCEPTED.value


def test_trail_redacts_before_writing(tmp_path):
    """账本要进看板：出口必须脱敏，且明文不得落盘。"""
    path = tmp_path / "trail.jsonl"
    trail = RepairTrail(path=path)
    bad = _report()
    evaluator = ScriptedEvaluator([{
        "evaluate": [bad],
        "recheck": _recheck(RecheckVerdict.ACCEPTED.value,
                            reports=[_report(fail=False)],
                            reason="修好了", fixed=["c1"])}])
    loop = RepairLoop(evaluator=evaluator,
                      harness=StubHarness(rationale="联系 13800138000 确认"),
                      max_rounds=1, trail=trail)
    _run(loop)
    raw = path.read_text(encoding="utf-8")
    assert "13800138000" not in raw
    json.loads(raw.splitlines()[0])       # 仍是合法 JSONL


# --------------------------------------------------------------------------- #
# 六、harness 跨轮不串味
# --------------------------------------------------------------------------- #
class _Sandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, *, workdir: str | None = None,
            timeout: int = 0) -> SandboxResult:
        self.commands.append(command)
        return SandboxResult(command=command, exit_code=0)


class TieredWriter:
    """第 1 次给补丁，之后一律弃权。"""

    def __init__(self, diff: str = _DIFF) -> None:
        self.diff = diff
        self.calls = 0
        self.seen: list[tuple[str, ...]] = []

    async def propose(self, *, defect, snippets, verify_target, feedback=""):
        self.calls += 1
        self.seen.append(tuple(s.path for s in snippets))
        if self.calls == 1:
            return CodeProposal(True, diff=self.diff, attempts=1,
                                rationale="第一次改", patches=parse_unified_diff(self.diff))
        return CodeProposal(False, attempts=1, skipped=True,
                            rationale="SKIP: 本轮没有新的把握")


def _harness_agent(repo: Path, coder: Any, *, scope: Mapping[str, Any] | None = None):
    agent = HarnessAgent(tools=grant(HarnessAgent, {}), sandbox=_Sandbox(),
                         coder=coder, patch_root=str(repo))
    return agent, _harness_state(scope=scope)


def _harness_state(*, scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    state = new_state(user_id=REPAIR_USER_ID, session_id="t")
    state["eval_platform"] = {
        "case_count": 1,
        "defects": {"case_count": 1, "blocked_by_security": False,
                    "defects": _defects_of(_report())}}
    if scope is not None:
        state["patch_scope"] = dict(scope)
    return state


def test_second_run_does_not_inherit_first_run_diff(tmp_path):
    """跨轮复用同一个 Agent 实例时，上一轮的 diff 不得被当成本轮的。"""
    repo = _repo(tmp_path)
    writer = TieredWriter()
    agent, state = _harness_agent(repo, writer)
    first = asyncio.run(agent.run(state))["p10_harness"]
    assert first["patch"]["applied"] is True
    assert first["patch"]["diff"] == _DIFF

    second = asyncio.run(agent.run(_harness_state()))["p10_harness"]
    assert second["patch"]["applied"] is False
    # 关键：不是「补丁未保留」那种含混说法，而是本轮真的没产出补丁
    assert "SKIP" in second["patch_not_applied"]["reason"]
    assert "补丁未保留" not in second["patch_not_applied"]["reason"]
    assert second["patch"]["diff"] == ""
    assert len(second["patch"]["code"]) == 1        # 只有本轮那一条


def test_locked_files_are_removed_from_coder_context(tmp_path):
    """模型看不到锁定文件，就不会去改——比「给了再拦」少烧一次调用。"""
    repo = _repo(tmp_path)
    writer = TieredWriter()
    # `retrieval_recall` 的收敛提示指向 chat.py 与 planner.py 两个文件；
    # 两个都锁住 → 候选为空 → 干脆不调模型。
    agent, state = _harness_agent(
        repo, writer,
        scope={"locked_paths": ["ai_butler/retrieval/chat.py", _PLANNER]})
    payload = asyncio.run(agent.run(state))["p10_harness"]
    assert writer.calls == 0                       # 候选被剔空 → 不调模型
    assert payload["patch"]["applied"] is False
    assert payload["patch"]["code"][0]["skipped"] is True
    assert "撤销" in payload["patch_not_applied"]["reason"]


def test_patch_touching_locked_file_is_rejected(tmp_path):
    """候选剔掉了，模型仍可能凭记忆写锁定文件——产物侧再拦一次。

    这是本组最要紧的一条：`_candidate_files` 只是「不给看」，
    不是判据。真正的边界由 `PatchGuard.frozen_hits` 在产物上判。
    """
    repo = _repo(tmp_path)
    writer = TieredWriter()                        # 总是写 planner.py
    agent, state = _harness_agent(repo, writer,
                                  scope={"locked_paths": [_PLANNER]})
    payload = asyncio.run(agent.run(state))["p10_harness"]
    # 候选里还剩 chat.py，所以确实调了模型
    assert writer.calls == 1
    # 但它写的是锁定文件 → 整份补丁作废
    assert payload["patch"]["applied"] is False
    assert "跨轮边界" in payload["patch_not_applied"]["reason"]
    assert (repo / _PLANNER).read_text(encoding="utf-8") == _ORIGINAL


def test_patch_touching_unlocked_files_still_passes(tmp_path):
    """对照组：锁定别的文件不应误伤能改的文件。"""
    repo = _repo(tmp_path)
    writer = TieredWriter()
    agent, state = _harness_agent(
        repo, writer, scope={"locked_paths": ["ai_butler/retrieval/chat.py"]})
    payload = asyncio.run(agent.run(state))["p10_harness"]
    assert writer.calls == 1
    assert payload["patch"]["applied"] is True


def _batch_of(defects: Sequence[Mapping[str, Any]]):
    from ai_butler.evaluation.defects import batch_from_dict
    return batch_from_dict({"defects": list(defects), "case_count": 1})
