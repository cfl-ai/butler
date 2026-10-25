"""大模型改代码 → 沙箱验证 → 评测复评：这条闭环的守护测试（U78/U79）。

这里挡的不是「代码跑不跑」，而是**五种会让闭环变成自证的退化**。
每一种都比「功能没做完」更难发现，因为它们都会让测试更绿：

1. **改测试**：删掉失败断言，测试立刻全绿，缺陷原样存在。
2. **改评测判据**：让被评的一方改判决书，改完必然「通过」。
3. **未验证就保留改动**：命令红了 / 根本没跑命令，代码却留在了仓库里。
4. **把「没法复评」当成「修好了」**：没有重新跑的痕迹，却报告 accepted。
5. **越界改别的子系统**：修检索召回，顺手动了调度器。

第 4 条是本模块最核心的一条：**「不知道」必须和「通过」分开**。
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import pytest

from ai_butler.agents.capabilities import grant
from ai_butler.agents.eval_platform import EvaluationPlatformAgent
from ai_butler.agents.p10_harness import HarnessAgent
from ai_butler.agents.state import new_state
from ai_butler.evaluation.defects import (
    Defect,
    DefectBatch,
    DefectKind,
    DefectSeverity,
    Disposition,
)
from ai_butler.evaluation.platform import EvaluationCase, EvaluationPlatform
from ai_butler.evaluation.recheck import (
    AuditSeverity,
    Rechecker,
    RecheckVerdict,
    audit_change,
)
from ai_butler.evaluation.redaction import Redactor
from ai_butler.evaluation.trace import AgentTrace, RetrievalRecord
from ai_butler.harness.coder import (
    CodeProposal,
    LLMCoder,
    SnippetReader,
    extract_diff,
)
from ai_butler.harness.hooks import HOOK_DENY, HookChain
from ai_butler.harness.loop import AgenticLoop, Proposal
from ai_butler.harness.patch import (
    PatchApplier,
    PatchError,
    PatchGuard,
    PatchResult,
    parse_unified_diff,
)
from ai_butler.harness.risk import Change
from ai_butler.harness.sandbox import FakeSandbox

# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
DEMO = "ai_butler/retrieval/demo.py"

DEMO_SRC = (
    "def fallback():\n"
    "    return 'UNKNOWN'\n"
    "\n"
    "\n"
    "def classify(q):\n"
    "    if q == 'hi':\n"
    "        return 'SMALL_TALK'\n"
    "    return fallback()\n"
)


@pytest.fixture()
def repo(tmp_path):
    pkg = tmp_path / "ai_butler" / "retrieval"
    pkg.mkdir(parents=True)
    (pkg / "demo.py").write_text(DEMO_SRC, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("assert True\n",
                                                    encoding="utf-8")
    return tmp_path


def _diff(path: str = DEMO, *, body: str, start: int = 7, count: int = 2) -> str:
    return (f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -{start},{count} +{start},{count} @@\n{body}\n")


#: 把 `return fallback()` 改成显式返回——等价于「修好了兜底分支」
GOOD_DIFF = _diff(body="         return 'SMALL_TALK'\n"
                       "-    return fallback()\n"
                       "+    return 'UNKNOWN'")


def _change(diff: str = GOOD_DIFF, **kw) -> Change:
    base: dict[str, Any] = dict(change_id="chg-1", description="修复兜底分类",
                                files=(DEMO,), diff=diff)
    base.update(kw)
    return Change(**base)


def _case(**kw) -> EvaluationCase:
    base: dict[str, Any] = dict(
        case_id="c1", question="那个啥来着",
        expected_answer="该问题属于语义检索范围",
        expected_points=("语义检索范围",),
        relevant_ids=("doc-1",),
        evidence_texts={"doc-1": "该问题属于语义检索范围。"})
    base.update(kw)
    return EvaluationCase(**base)


def _traces(*, good: bool, question: str = "那个啥来着") -> tuple[AgentTrace, ...]:
    answer = "该问题属于语义检索范围" if good else "闲聊，无需检索"
    retrieved = ("doc-1",) if good else ()
    return tuple(
        AgentTrace(trace_id=f"t{i}", question=question, answer=answer,
                   retrieval=RetrievalRecord(query=question,
                                             retrieved_ids=retrieved))
        for i in range(2))


class Producer:
    """痕迹再生产者桩：`good` 决定它「修好了没」。"""

    def __init__(self, *, good: bool) -> None:
        self.good = good
        self.calls = 0

    async def produce(self, case: EvaluationCase) -> Sequence[AgentTrace]:
        self.calls += 1
        return _traces(good=self.good, question=case.question)


class ExplodingProducer:
    async def produce(self, case: EvaluationCase) -> Sequence[AgentTrace]:
        raise RuntimeError("管道不可用")


class SpyApplier:
    """记录调用但不动文件的补丁应用器，用来验证 loop 的调用时机。"""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.applied: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    @property
    def pending(self) -> tuple[str, ...]:
        return ()

    def apply(self, patch: Any) -> PatchResult:
        self.applied.append(patch)
        return PatchResult(self.ok, paths=(DEMO,), touched=2,
                           reason="" if self.ok else "应用失败")

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> tuple[str, ...]:
        self.rollbacks += 1
        return (DEMO,)


def _loop(*proposals: Proposal, sandbox=None, patcher=None,
          qa: dict[str, Any] | None = None) -> AgenticLoop:
    items = list(proposals)

    async def decide(_context, _history):
        return items.pop(0) if items else Proposal(summary="用尽", done=False)

    return AgenticLoop(
        decide=decide,
        hooks=HookChain.default(qa_verdict=qa or {"action": "pass", "alerts": []}),
        sandbox=sandbox or FakeSandbox(), patcher=patcher)


def _run(loop: AgenticLoop):
    return asyncio.run(loop.run(goal="g", items=(), redactor=Redactor()))


# --------------------------------------------------------------------------- #
# 一、补丁机械层：解析、应用、回滚
# --------------------------------------------------------------------------- #
def test_parse_standard_diff():
    patch = parse_unified_diff(GOOD_DIFF)
    assert patch.paths == (DEMO,)
    assert patch.touched == 2


def test_parse_strips_markdown_fence():
    """模型习惯套 ```diff 围栏 + 前后说两句。提取由 `extract_diff` 负责，
    `parse_unified_diff` 只吃纯 diff——**两层各管一件事**，
    解析器不宽容前言，是为了让「格式不对」在提取层就暴露并触发重试。"""
    fenced = "这是补丁：\n```diff\n" + GOOD_DIFF + "```\n以上。"
    diff, _ = extract_diff(fenced)
    assert parse_unified_diff(diff).paths == (DEMO,)


@pytest.mark.parametrize("path", ["ai_butler/retrieval/demo.py",
                                  "./ai_butler/retrieval/demo.py",
                                  "ai_butler\\retrieval\\demo.py"])
def test_parse_normalizes_paths(path):
    """路径必须归一到同一个键，否则白名单会漏判（漏判即防线失效）。"""
    text = _diff(path, body=" a\n-b\n+c")
    assert parse_unified_diff(text).paths == (DEMO,)


def test_parse_rejects_parent_path():
    with pytest.raises(PatchError):
        parse_unified_diff(_diff("../../../etc/passwd", body=" a\n-b\n+c"))


def test_parse_rejects_file_deletion():
    """不支持删除文件：删除不是「修复」，且回滚语义会变复杂。"""
    text = "--- a/ai_butler/retrieval/demo.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
    with pytest.raises(PatchError):
        parse_unified_diff(text)


def test_apply_matches_context_with_drift(repo):
    """行号差几行仍能应用——模型数的是它眼里的片段行号，不是文件真实行号。"""
    text = _diff(body="         return 'SMALL_TALK'\n"
                      "-    return fallback()\n"
                      "+    return 'UNKNOWN'", start=9, count=2)
    applier = PatchApplier(root=repo)
    assert applier.apply(parse_unified_diff(text)).ok
    assert "return 'UNKNOWN'\ndef" not in (repo / DEMO).read_text(encoding="utf-8")
    assert (repo / DEMO).read_text(encoding="utf-8").count("'UNKNOWN'") == 2


def test_apply_refuses_when_context_mismatch_and_leaves_file_intact(repo):
    """**最坏的失败模式是错位应用**，所以宁可失败。"""
    before = (repo / DEMO).read_text(encoding="utf-8")
    text = _diff(body=" 不存在的行\n-也不存在\n+替换")
    result = PatchApplier(root=repo).apply(parse_unified_diff(text))
    assert result.ok is False
    assert "找不到匹配的原文" in result.reason
    assert (repo / DEMO).read_text(encoding="utf-8") == before


def test_apply_multiple_files(repo):
    other = repo / "ai_butler" / "retrieval" / "extra.py"
    other.write_text("def f():\n    return 1\n", encoding="utf-8")
    text = (_diff(body="         return 'SMALL_TALK'\n"
                       "-    return fallback()\n"
                       "+    return 'UNKNOWN'")
            + "--- a/ai_butler/retrieval/extra.py\n"
              "+++ b/ai_butler/retrieval/extra.py\n"
              "@@ -1,2 +1,2 @@\n"
              " def f():\n"
              "-    return 1\n"
              "+    return 2\n")
    result = PatchApplier(root=repo).apply(parse_unified_diff(text))
    assert result.ok and len(result.paths) == 2
    assert "return 2" in other.read_text(encoding="utf-8")


def test_rollback_restores_every_file(repo):
    before = (repo / DEMO).read_text(encoding="utf-8")
    applier = PatchApplier(root=repo)
    assert applier.apply(parse_unified_diff(GOOD_DIFF)).ok
    assert (repo / DEMO).read_text(encoding="utf-8") != before
    applier.rollback()
    assert (repo / DEMO).read_text(encoding="utf-8") == before
    assert applier.pending == ()


def test_commit_makes_rollback_a_noop(repo):
    """commit 之后改动就不再可回滚——所以 commit 必须发生在验证通过之后。"""
    applier = PatchApplier(root=repo)
    applier.apply(parse_unified_diff(GOOD_DIFF))
    after = (repo / DEMO).read_text(encoding="utf-8")
    applier.commit()
    applier.rollback()
    assert (repo / DEMO).read_text(encoding="utf-8") == after


# --------------------------------------------------------------------------- #
# 二、范围白名单
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,reason", [
    ("tests/test_demo.py", "改测试"),
    ("ai_butler/evaluation/dimensions.py", "改评测判据"),
    ("ai_butler/harness/hooks.py", "改门禁"),
    ("ai_butler/scheduler/router.py", "越界子系统"),
])
def test_guard_blocks_out_of_scope_paths(path, reason):
    patch = parse_unified_diff(_diff(path, body=" a\n-b\n+c", start=1, count=2))
    violations = PatchGuard.for_kind("recall_gap").violations(patch)
    assert violations, f"{reason}（{path}）未被拦住"


def test_guard_blocks_size_blowup():
    body = "\n".join(f"-line{i}\n+new{i}" for i in range(300))
    patch = parse_unified_diff(_diff(body=body, start=1, count=300))
    assert any("超过上限" in v for v in
               PatchGuard.for_kind("recall_gap").violations(patch))


def test_guard_rejects_unregistered_kind():
    patch = parse_unified_diff(GOOD_DIFF)
    assert PatchGuard.for_kind("security_violation").violations(patch)


# --------------------------------------------------------------------------- #
# 三、代码创作层
# --------------------------------------------------------------------------- #
def test_extract_diff_from_fence():
    diff, _ = extract_diff("说明\n```diff\n" + GOOD_DIFF + "```\n")
    assert parse_unified_diff(diff).paths == (DEMO,)


def test_extract_diff_from_bare_headers():
    diff, _ = extract_diff("好的：\n" + GOOD_DIFF)
    assert diff.startswith("--- ")


def test_extract_skip_is_recognized():
    diff, rationale = extract_diff("```diff\n```\nSKIP: 这是数据问题，不该改代码")
    assert diff == "" and rationale.startswith("SKIP:")


class _StubGateway:
    """按顺序回放预设回复，记录调用次数。"""

    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    async def chat(self, **kw):
        user = kw["messages"][-1].content
        self.calls.append(user)
        text = self.replies.pop(0) if self.replies else ""

        class R:
            pass

        r = R()
        r.text = text
        return r


def _defect(kind: str = "recall_gap", **kw) -> Defect:
    base: dict[str, Any] = dict(
        defect_id="d1", case_id="c1", question="那个啥来着",
        dimension="retrieval_recall", kind=DefectKind(kind),
        severity=DefectSeverity.MAJOR, disposition=Disposition.AUTO,
        title="兜底分类错了", evidence="无信号时返回 SMALL_TALK",
        target_hint=f"{DEMO}（兜底分支）")
    base.update(kw)
    return Defect(**base)


def test_coder_retries_once_with_feedback_then_gives_up():
    """第一次格式错 → 带报错重试一次；第二次还错就放弃，**不无限重试**。"""
    gateway = _StubGateway(["我不会写", "还是不写"])
    coder = LLMCoder(gateway=gateway, max_attempts=2)
    result = asyncio.run(coder.propose(defect=_defect(), snippets=(),
                                       verify_target="pytest -q"))
    assert result.ok is False
    assert result.attempts == 2
    assert len(gateway.calls) == 2
    # 第二次请求里必须带上第一次的失败原因，否则重试等于复读
    assert "没有可识别的 diff" in gateway.calls[1]


def test_coder_rejects_out_of_scope_patch():
    """模型给出越界补丁 → 拒绝，且把违规原因回灌下一轮。"""
    bad = _diff("tests/test_demo.py", body=" a\n-b\n+c", start=1, count=2)
    gateway = _StubGateway(["```diff\n" + bad + "```", "```diff\n" + bad + "```"])
    coder = LLMCoder(gateway=gateway, max_attempts=2)
    result = asyncio.run(coder.propose(defect=_defect(), snippets=(),
                                       verify_target="pytest -q"))
    assert result.ok is False
    assert any("禁改" in r or "允许范围" in r for r in result.rejected)
    assert "只能改" in gateway.calls[1]


def test_coder_accepts_compliant_patch_and_claims_nothing():
    """`ok=True` 只表示「格式与范围都过了」——**没有「已修复」这种字段**。"""
    gateway = _StubGateway(["```diff\n" + GOOD_DIFF + "```"])
    result = asyncio.run(LLMCoder(gateway=gateway).propose(
        defect=_defect(), snippets=(), verify_target="pytest -q"))
    assert result.ok and result.paths == (DEMO,)
    payload = result.as_dict()
    assert "fixed" not in payload and "verified" not in payload
    assert payload["touched"] == 2


def test_snippet_reader_never_reads_outside_allowed_scope(repo):
    """越界路径静默跳过：为一句收敛提示而整轮失败不划算，越界的补丁另有闸。"""
    reader = SnippetReader(root=repo)
    assert reader.read(("tests/test_demo.py",), kind="recall_gap") == ()
    assert reader.read((DEMO,), kind="recall_gap")[0].path == DEMO


def test_snippet_reader_slices_large_file_by_keyword(repo):
    """超过整篇上限才切片，且**预算均分**给各命中窗口。

    均分是要点：关键分支常在文件后半段，让第一个窗口吃掉全部预算，
    等于仍然只给了开头（这正是真机上翻车的方式）。
    """
    big = repo / "ai_butler" / "retrieval" / "big.py"
    lines = [f"line_{i} = {i}" for i in range(400)]
    lines[40] = "first_symbol = 1"
    lines[320] = "def target_symbol():"
    big.write_text("\n".join(lines) + "\n", encoding="utf-8")
    snippets = SnippetReader(root=repo, max_lines=160,
                             full_file_cap=100).read(
        ("ai_butler/retrieval/big.py",), kind="recall_gap",
        keywords=("first_symbol", "target_symbol"))
    starts = {s.start_line for s in snippets}
    assert any(st > 250 for st in starts), starts
    assert sum(len(s.text.splitlines()) for s in snippets) <= 160


def test_snippet_reader_gives_whole_file_when_it_is_small(repo):
    """小文件整篇给。起因是一次真机失败：切片漏掉兜底分支，
    模型正确地输出了 `SKIP: 上下文被截断`——它没错，是上下文给少了。"""
    snippet = SnippetReader(root=repo).read((DEMO,), kind="recall_gap")[0]
    assert snippet.start_line == 1
    assert snippet.text.count("def ") == 2


# --------------------------------------------------------------------------- #
# 四、改动审计（结构判据）
# --------------------------------------------------------------------------- #
def test_audit_detects_removed_assert():
    text = _diff(DEMO, body=" def f():\n-    assert x == 1\n+    pass", start=1)
    audit = audit_change(text, kind="recall_gap")
    assert audit.passed is False
    assert "assert_removed" in {f.code for f in audit.blocking}


def test_audit_detects_added_skip():
    text = _diff(DEMO, body=" def f():\n-    run()\n+@pytest.mark.skip", start=1)
    audit = audit_change(text, kind="recall_gap")
    assert "skip_added" in {f.code for f in audit.blocking}


def test_audit_blocks_forbidden_path_even_if_scope_matches():
    text = _diff("tests/test_demo.py", body=" a\n-b\n+c", start=1)
    audit = audit_change(text, kind="recall_gap",
                         allowed_prefixes=("tests/",))
    assert "forbidden_path" in {f.code for f in audit.blocking}


def test_audit_warns_but_does_not_block_on_guard_removal():
    """删掉含 user_id 的行**可能是重构**，不能一律判死；但必须转人工看。"""
    text = _diff(DEMO, body=" def f():\n-    rows = q.filter(user_id=uid)\n"
                            "+    rows = q.all()", start=1)
    audit = audit_change(text, kind="recall_gap")
    codes = {f.code: f.severity for f in audit.findings}
    assert codes.get("guard_maybe_removed") is AuditSeverity.WARNING
    assert audit.passed is True


# --------------------------------------------------------------------------- #
# 五、复评（效果判据）
# --------------------------------------------------------------------------- #
def _before(*, passed: bool = False) -> Any:
    platform = EvaluationPlatform()
    return platform.evaluate(_traces(good=passed), _case())


def test_recheck_accepted_only_when_target_turns_green():
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=Producer(good=True)).recheck(
        cases=[_case()], before_reports=[_before(passed=False)],
        diff=GOOD_DIFF, kind="recall_gap"))
    assert result.verdict is RecheckVerdict.ACCEPTED
    assert result.fixed == ("c1",)


def test_recheck_no_effect_when_nothing_changes():
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=Producer(good=False)).recheck(
        cases=[_case()], before_reports=[_before(passed=False)],
        diff=GOOD_DIFF, kind="recall_gap"))
    assert result.verdict is RecheckVerdict.REJECTED_NO_EFFECT
    assert result.still_broken == ("c1",)


def test_recheck_regression_when_previously_green_turns_red():
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=Producer(good=False)).recheck(
        cases=[_case()], before_reports=[_before(passed=True)],
        diff=GOOD_DIFF, kind="recall_gap"))
    assert result.verdict is RecheckVerdict.REJECTED_REGRESSION
    assert result.regressed == ("c1",)


def test_recheck_audit_short_circuits_before_rerunning():
    """乱改的补丁连复跑都不该跑：它的「效果变好」可能来自删断言。"""
    producer = Producer(good=True)
    bad = _diff("tests/test_demo.py", body=" a\n-assert x\n+assert True", start=1)
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=producer).recheck(
        cases=[_case()], before_reports=[_before()], diff=bad, kind="recall_gap"))
    assert result.verdict is RecheckVerdict.REJECTED_AUDIT
    assert producer.calls == 0


def test_recheck_without_producer_is_unverified_not_accepted():
    """**最核心的一条**：没有重新跑的痕迹，就不能说修好了。"""
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=None).recheck(
        cases=[_case()], before_reports=[_before(passed=False)],
        diff=GOOD_DIFF, kind="recall_gap"))
    assert result.verdict is RecheckVerdict.UNVERIFIED
    assert result.verdict.accepted is False


def test_recheck_without_baseline_is_unverified():
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=Producer(good=True)).recheck(
        cases=[_case()], before_reports=[], diff=GOOD_DIFF, kind="recall_gap"))
    assert result.verdict is RecheckVerdict.UNVERIFIED
    assert "基线" in result.reason


def test_recheck_all_errors_is_unverified():
    result = asyncio.run(Rechecker(
        platform=EvaluationPlatform(), producer=ExplodingProducer()).recheck(
        cases=[_case()], before_reports=[_before(passed=False)], diff="",
        kind="recall_gap"))
    assert result.verdict is RecheckVerdict.UNVERIFIED


# --------------------------------------------------------------------------- #
# 六、Loop 的补丁时序
# --------------------------------------------------------------------------- #
def test_loop_does_not_apply_patch_when_hook_denies():
    """Hook 判死时不碰代码。顺序反了就是「先落地再拦截」。"""
    spy = SpyApplier()
    dangerous = _change(
        _diff(DEMO, body=" def f():\n-    rows = q.filter(user_id=uid)\n"
                         "+    rows = q.all()", start=1))
    result = _run(_loop(Proposal(summary="改权限", change=dangerous), patcher=spy))
    assert result.blocked is True
    assert spy.applied == []
    assert result.reason.startswith("Hook 拦截")


def test_loop_commits_when_commands_are_green(repo):
    spy = SpyApplier()
    sandbox = FakeSandbox()
    result = _run(_loop(
        Proposal(summary="修", change=_change(), commands=("pytest -q",)),
        Proposal(summary="确认", done=True),
        sandbox=sandbox, patcher=spy))
    assert spy.commits == 1 and spy.rollbacks == 0
    assert result.converged is True
    assert sandbox.commands == ["pytest -q"]


def test_loop_rolls_back_when_commands_fail(repo):
    spy = SpyApplier()
    failing = FakeSandbox([PatchResult and None or None])  # 占位，下面覆写
    from ai_butler.harness.sandbox import SandboxResult
    failing = FakeSandbox([SandboxResult("pytest -q", 1, stdout="FAILED")])
    result = _run(_loop(
        Proposal(summary="修", change=_change(), commands=("pytest -q",)),
        sandbox=failing, patcher=spy))
    assert spy.commits == 0
    assert spy.rollbacks == 1
    assert result.converged is False


def test_loop_rolls_back_patch_that_was_never_verified(repo):
    """改了代码但没跑命令 = 未验证，**不得留在仓库里**。"""
    spy = SpyApplier()
    _run(_loop(Proposal(summary="只改不验", change=_change()), patcher=spy))
    assert spy.rollbacks == 1 and spy.commits == 0


def test_loop_rolls_back_when_decider_raises(repo):
    """决策层抛异常时，已应用的补丁也必须还原（兜底在 finally 里）。"""
    applier = PatchApplier(root=repo)
    before = (repo / DEMO).read_text(encoding="utf-8")

    async def boom(_context, _history):
        raise RuntimeError("决策层崩了")

    loop = AgenticLoop(
        decide=boom,
        hooks=HookChain.default(qa_verdict={"action": "pass", "alerts": []}),
        sandbox=FakeSandbox(), patcher=applier)
    # 先手动应用一次，模拟「应用到一半崩了」
    assert applier.apply(parse_unified_diff(GOOD_DIFF)).ok
    assert (repo / DEMO).read_text(encoding="utf-8") != before
    with pytest.raises(RuntimeError):
        asyncio.run(loop.run(goal="g", items=(), redactor=Redactor()))
    assert (repo / DEMO).read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# 七、两个 Agent 的接线
# --------------------------------------------------------------------------- #
def _batch() -> DefectBatch:
    return DefectBatch(defects=(_defect(),), case_count=1)


def _harness_state(**kw) -> dict[str, Any]:
    state = new_state(user_id="system:p10-harness", session_id="s1")
    state["eval_platform"] = {"defects": _batch().as_dict()}
    state.update(kw)
    return state


def test_harness_without_coder_says_why_it_did_not_change_code():
    agent = HarnessAgent(tools=grant(HarnessAgent, {}), sandbox=FakeSandbox())
    payload = asyncio.run(agent.run(_harness_state()))["p10_harness"]
    assert payload["patch"]["applied"] is False
    assert payload["patch"]["coder_wired"] is False
    assert "代码创作" in payload["patch_not_applied"]["reason"]


def test_harness_with_stub_coder_applies_and_verifies(repo):
    """真应用、真跑、真保留：用桩模型 + 真文件。"""
    spy = SpyApplier()

    class Writer:
        async def propose(self, *, defect, snippets, verify_target, feedback=""):
            return CodeProposal(True, diff=GOOD_DIFF, attempts=1,
                                patches=parse_unified_diff(GOOD_DIFF))

    agent = HarnessAgent(
        tools=grant(HarnessAgent, {}), sandbox=FakeSandbox(),
        coder=Writer(), patch_root=str(repo), applier=spy)
    payload = asyncio.run(agent.run(_harness_state()))["p10_harness"]
    assert payload["patch"]["applied"] is True
    assert payload["patch"]["diff"] == GOOD_DIFF
    assert payload["converged"] is True
    assert spy.commits == 1


def test_eval_platform_recheck_reads_diff_but_not_self_claims():
    """复评只从 harness 段取 diff；`converged=True` 不得影响结论。"""
    platform = EvaluationPlatform()
    agent = EvaluationPlatformAgent(tools=grant(EvaluationPlatformAgent, {}),
                                    producer=Producer(good=False))
    state = new_state(user_id="u1", session_id="s1")
    state["eval_cases"] = [{
        "case_id": "c1", "question": "那个啥来着",
        "expected_answer": "该问题属于语义检索范围",
        "expected_points": ["语义检索范围"],
        "relevant_ids": ["doc-1"],
        "evidence_texts": {"doc-1": "该问题属于语义检索范围。"},
    }]
    state["eval_platform"] = {"reports": [_before(passed=False).as_dict()]}
    state["p10_harness"] = {
        "converged": True, "patch": {"diff": GOOD_DIFF, "applied": True},
        "intake": {"tickets": [{"defect": {"kind": "recall_gap"}}]},
    }
    out = asyncio.run(agent.recheck(state))["eval_platform_recheck"]
    # harness 自称 converged，但复评跑出来没修好 → 必须如实否决
    assert out["accepted"] is False
    assert out["verdict"] == "rejected_no_effect"
    assert out["inputs"]["diff_chars"] == len(GOOD_DIFF)
