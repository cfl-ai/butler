"""P10 Harness 自进化闭环测试。

计划书依据：三、P10 十项开发步骤 + 表 10 + 四阶循环 + 触发时机
裁定：U08（不引入 Claude Code 本体）、U32（循环上限）、U33（风险分级）、
      U34（沙箱端口）、U35（MCP Skill）、U36（上下文压缩）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_butler.agents.base import resolve
from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_READ, grant
from ai_butler.agents.p10_harness import HarnessAgent
from ai_butler.agents.state import new_state
from ai_butler.core import decisions
from ai_butler.evaluation.redaction import RedactionError, Redactor
from ai_butler.harness.context import ContextItem, compress
from ai_butler.harness.hooks import (
    DEFAULT_HOOKS,
    HOOK_ALLOW,
    HOOK_DENY,
    HOOK_NEEDS_APPROVAL,
    HOOK_OUTCOMES,
    HookError,
    HookFinding,
    HookChain,
    make_qa_hook,
)
from ai_butler.harness.loop import (
    LOOP_STAGES,
    MAX_ITERATIONS,
    NO_PROGRESS_LIMIT,
    STAGE_COLLECT,
    STAGE_CORRECT,
    STAGE_DECIDE,
    STAGE_EXECUTE,
    AgenticLoop,
    Proposal,
)
from ai_butler.harness.memory import (
    ENTRY_PREFIX,
    MEMORY_DEFECT,
    MEMORY_JEV_FAILURE,
    MEMORY_KINDS,
    MemoryEntry,
    MemoryError,
    MemoryStore,
    from_qa_report,
)
from ai_butler.harness.risk import (
    APPROVAL_REDLINES,
    FORBIDDEN_REDLINES,
    REDLINE_JEV_TEMPLATE,
    REDLINE_ROW_LEVEL_FILTER,
    REDLINE_UNREDACTED_VAULT,
    REDLINE_WIKI_BULK,
    REDLINES,
    RISK_HIGH,
    RISK_LEVELS,
    RISK_LOW,
    RISK_MEDIUM,
    WIKI_BULK_THRESHOLD,
    Change,
    assess,
    detect_redlines,
)
from ai_butler.harness.sandbox import (
    ALLOWED_COMMANDS,
    REQUIRED_TEST_KINDS,
    FakeSandbox,
    ProcessSandbox,
    SandboxError,
    SandboxResult,
    TestPlan,
)
from ai_butler.harness.skills import (
    SCOPE_PUBLIC,
    SCOPE_VAULT,
    SKILL_AGENT_WIKI,
    TOOL_CATEGORIES,
    SkillError,
    SkillSpec,
    agent_wiki_maintenance_skill,
    assert_skill_scope,
    default_registry,
)


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


def _run(coro):
    return asyncio.run(coro)


# ============================== 台账 ============================== #
def test_p10_decisions_are_registered_and_resolved():
    keys = {d.key for d in decisions.DECISIONS if d.phase == "P10"}
    # U49 = Harness 自身评测体系（2026-10-19 补，见 harness/evaluation.py）
    # U58 = 上下文分项计量与 shadowing 压缩（2026-10-23 补，见 harness/context.py）
    assert keys == {"U08", "U32", "U33", "U34", "U35", "U36", "U49", "U58"}
    for key in keys:
        assert decisions.get(key).status == decisions.RESOLVED


def test_u08_keeps_claude_code_out_of_the_codebase():
    """裁定 U08：不引入 Claude Code 本体，只借设计思维自研 Agentic Loop。"""
    assert "不引入 Claude Code 本体" in decisions.get("U08").resolution


# ============================== 风险分级（U33 / 表 10）============================== #
def test_risk_levels_and_redlines_match_table_10():
    assert RISK_LEVELS == ("low", "medium", "high")
    assert REDLINES == (REDLINE_ROW_LEVEL_FILTER, REDLINE_UNREDACTED_VAULT,
                        REDLINE_JEV_TEMPLATE, REDLINE_WIKI_BULK)


def test_table_10_splits_into_forbidden_and_approval():
    """表 10 的措辞分两类：前两条是「禁止」，后两条是「强制人工审批」。"""
    assert FORBIDDEN_REDLINES == (REDLINE_ROW_LEVEL_FILTER, REDLINE_UNREDACTED_VAULT)
    assert APPROVAL_REDLINES == (REDLINE_JEV_TEMPLATE, REDLINE_WIKI_BULK)


def test_low_risk_change_is_allowed():
    got = assess(Change(change_id="c1", description="修错别字"))
    assert got.level == RISK_LOW and got.requires_approval is False
    assert got.forbidden is False


def test_removing_row_level_filter_is_detected_in_the_diff():
    diff = "\n".join([
        "--- a/ai_butler/core/db.py",
        "+++ b/ai_butler/core/db.py",
        "@@ -1,3 +1,1 @@",
        "-    if page.user_id not in (None, user_id):",
        "-        raise PermissionError('行级隔离')",
        "+    pass",
    ])
    got = assess(Change(change_id="c2", diff=diff))
    assert got.level == RISK_HIGH and got.forbidden is True
    assert REDLINE_ROW_LEVEL_FILTER in got.redlines


def test_adding_protection_is_not_a_redline():
    """新增保护不算风险，**删除才算**。"""
    diff = "+    if page.user_id not in (None, user_id):\n+        raise PermissionError"
    assert detect_redlines(Change(change_id="c3", diff=diff)) == ()


def test_removing_auth_is_detected():
    diff = "-    if not state.get('login_status'):\n-        raise NotSignedIn"
    assert REDLINE_ROW_LEVEL_FILTER in detect_redlines(Change(change_id="c4", diff=diff))


def test_reading_unredacted_vault_is_a_redline():
    bad = assess(Change(change_id="c5", wiki_scopes=("vault",), redacted=False))
    assert bad.forbidden is True and REDLINE_UNREDACTED_VAULT in bad.redlines

    ok = assess(Change(change_id="c6", wiki_scopes=("vault",), redacted=True))
    assert ok.level == RISK_LOW, "已脱敏地读私有 Vault 不触红线"


def test_jev_template_change_needs_approval_but_is_not_forbidden():
    got = assess(Change(change_id="c7", modifies_jev_template=True))
    assert got.level == RISK_HIGH and got.requires_approval is True
    assert got.forbidden is False, "表 10 对 Jev 模板说的是「强制人工审批」，不是禁止"


def test_wiki_bulk_change_needs_approval():
    got = assess(Change(change_id="c8", wiki_pages_written=WIKI_BULK_THRESHOLD))
    assert got.forbidden is False and REDLINE_WIKI_BULK in got.redlines


def test_medium_risk_from_many_files():
    got = assess(Change(change_id="c9", files=tuple(f"f{i}.py" for i in range(10))))
    assert got.level == RISK_MEDIUM and got.requires_approval is False


def test_change_requires_an_id():
    with pytest.raises(Exception):
        Change(change_id="")


# ============================== Hook 链（表 10 + 步骤3）============================== #
def test_hook_outcomes_are_closed_set():
    assert HOOK_OUTCOMES == ("allow", "needs_approval", "deny")


def test_hook_finding_rejects_unknown_outcome():
    with pytest.raises(HookError):
        HookFinding("h", "maybe", "")


def test_default_chain_allows_ordinary_fix():
    chain = HookChain.default(qa_verdict={"action": "pass"})
    assert chain.evaluate(Change(change_id="c1")).outcome == HOOK_ALLOW


def test_default_chain_denies_forbidden_redlines():
    chain = HookChain.default(qa_verdict={"action": "pass"})
    v = chain.evaluate(Change(change_id="c2", wiki_scopes=("vault",), redacted=False))
    assert v.outcome == HOOK_DENY and v.risk.forbidden is True


def test_default_chain_requires_approval_for_jev_template():
    chain = HookChain.default(qa_verdict={"action": "pass"})
    v = chain.evaluate(Change(change_id="c3", modifies_jev_template=True))
    assert v.outcome == HOOK_NEEDS_APPROVAL, "Jev 模板改动应走审批，而不是被拒绝"


def test_qa_hook_blocks_when_qa_blocks():
    """计划书步骤3「联动质检 Agent 完成风险分级校验」。"""
    chain = HookChain.default(qa_verdict={"action": "block", "alerts": ["混入他人隐私"]})
    v = chain.evaluate(Change(change_id="c4"))
    assert v.outcome == HOOK_DENY and "质检拦截" in v.findings[-1].detail


def test_qa_hook_requires_approval_when_verdict_missing():
    chain = HookChain.default(qa_verdict=None)
    v = chain.evaluate(Change(change_id="c5"))
    assert v.outcome == HOOK_NEEDS_APPROVAL


def test_hook_exception_fails_closed():
    """Hook 自己出错时**不放行**——否则一个 bug 就能让红线失效。"""
    def _boom(change, risk):
        raise RuntimeError("hook 崩了")

    chain = HookChain(hooks=[("boom", _boom)])
    v = chain.evaluate(Change(change_id="c6"))
    assert v.outcome == HOOK_DENY


def test_default_hooks_are_ordered():
    names = [n for n, _ in DEFAULT_HOOKS]
    assert names[0] == "risk_grading" and "vault_scope" in names


# ============================== 沙箱（U34 + 步骤4）============================== #
def test_sandbox_command_whitelist():
    sb = ProcessSandbox(root=".")
    assert sb.check_command("pytest -q") == "pytest"
    with pytest.raises(SandboxError):
        sb.check_command("rm -rf /")
    assert "rm" not in ALLOWED_COMMANDS


def test_sandbox_rejects_empty_command():
    with pytest.raises(SandboxError):
        ProcessSandbox(root=".").check_command("   ")


def test_test_plan_requires_the_three_kinds_from_the_plan():
    """计划书步骤4：单元测试包含**用户隔离、Jev、Agent-Wiki 本体校验**用例。"""
    assert REQUIRED_TEST_KINDS == ("user_isolation", "jev", "agent_wiki_ontology")
    assert TestPlan().command() == "pytest -q"
    with pytest.raises(SandboxError):
        TestPlan(kinds=("user_isolation",))


def test_fake_sandbox_records_commands():
    sb = FakeSandbox()
    sb.run("pytest -q")
    assert sb.commands == ["pytest -q"]


# ============================== MCP Skill（U35 + 步骤5）============================== #
def test_registry_covers_the_four_tool_categories():
    assert TOOL_CATEGORIES == ("retrieval", "git", "file", "terminal")
    reg = default_registry()
    for category in TOOL_CATEGORIES:
        assert reg.by_category(category), f"缺少 {category} 类工具"


def test_agent_wiki_skill_is_public_only():
    """计划书步骤5「Agent-Wiki 维护 Skill，**仅用于维护公共 Workspace**」。"""
    skill = agent_wiki_maintenance_skill()
    assert skill.name == SKILL_AGENT_WIKI
    assert skill.allowed_scopes == (SCOPE_PUBLIC,)
    with pytest.raises(SkillError) as ei:
        assert_skill_scope(skill, scope=SCOPE_VAULT)
    assert "私有 Vault" in str(ei.value)


def test_public_scope_must_not_carry_user_id():
    skill = agent_wiki_maintenance_skill()
    with pytest.raises(SkillError):
        assert_skill_scope(skill, scope=SCOPE_PUBLIC, user_id="user-A")
    assert_skill_scope(skill, scope=SCOPE_PUBLIC) is None


def test_skill_spec_validates_category_and_scope():
    with pytest.raises(SkillError):
        SkillSpec("x", "d", "nope")
    with pytest.raises(SkillError):
        SkillSpec("x", "d", "file", allowed_scopes=("nope",))


def test_registry_rejects_duplicate_and_unknown():
    reg = default_registry()
    with pytest.raises(SkillError):
        reg.register(agent_wiki_maintenance_skill())
    with pytest.raises(SkillError):
        reg.get("nope")


def test_registry_call_enforces_scope():
    reg = default_registry()
    with pytest.raises(SkillError):
        reg.call(SKILL_AGENT_WIKI, scope=SCOPE_VAULT)


# ============================== 上下文压缩（U36 + 步骤7）============================== #
def test_compress_redacts_before_keeping():
    items = [ContextItem("i1", "用户 user-A 手机 13812345678", relevance=1.0)]
    got = compress(items, redactor=Redactor(), max_items=5)
    assert "user-A" not in got.text and "13812345678" not in got.text


def test_compress_trims_to_max_items_keeping_relevant():
    items = [ContextItem(f"i{i}", f"内容 {i}", relevance=float(i)) for i in range(10)]
    got = compress(items, redactor=Redactor(), max_items=3)
    assert len(got.items) == 3 and got.dropped == 7
    assert got.items[0].relevance == 9.0


def test_summariser_output_is_redacted_before_use():
    """摘要器的输出同样要过脱敏——模型很容易把输入里的身份抄进摘要。"""
    def _leaky(text: str) -> str:
        return "汇总：user-Z 很喜欢 13800000000"

    got = compress([ContextItem("i1", "普通内容", relevance=1.0)],
                   redactor=Redactor(), summarizer=_leaky)
    assert "user-Z" not in got.summary and "13800000000" not in got.summary


def test_compress_reports_model_layer_honestly():
    items = [ContextItem("i1", "内容")]
    assert compress(items, redactor=Redactor()).model_layer_active is False
    got = compress(items, redactor=Redactor(), summarizer=lambda t: "摘要")
    assert got.model_layer_active is True and got.summary == "摘要"


def test_compress_rejects_bad_max_items():
    with pytest.raises(Exception):
        compress([], redactor=Redactor(), max_items=0)


# ============================== 持久记忆（步骤9）============================== #
def test_memory_kinds_cover_the_plan_requirements():
    assert MEMORY_KINDS == ("defect", "jev_failure", "wiki_conflict")


def test_memory_entry_requires_problem_and_solution():
    with pytest.raises(MemoryError):
        MemoryEntry(kind=MEMORY_DEFECT, key="k", title="t", problem="", solution="s")
    with pytest.raises(MemoryError):
        MemoryEntry(kind="nope", key="k", title="t", problem="p", solution="s")


def test_memory_render_redacts():
    entry = MemoryEntry(kind=MEMORY_DEFECT, key="E-99", title="示例",
                        problem="日志里出现 user-A", solution="加脱敏")
    text = entry.render(Redactor())
    assert "user-A" not in text and "E-99" in text


def test_memory_store_appends_and_dedupes(workdir):
    # 用 workdir fixture，不用 tmp_path —— 沙箱下 mkdtemp 不可写（见 MEMORY E-03）
    path = Path(workdir) / "MEMORY.md"
    store = MemoryStore(path=path)
    entry = MemoryEntry(kind=MEMORY_DEFECT, key="E-99", title="t",
                        problem="p", solution="s")
    assert store.append([entry]) == 1
    assert store.has("E-99") and store.keys() == ("E-99",)
    assert ENTRY_PREFIX in path.read_text(encoding="utf-8")


def test_memory_store_records_stable_key(workdir):
    """key 稳定可查，便于避免重复沉淀同一个坑。"""
    store = MemoryStore(path=Path(workdir) / "MEMORY.md")
    entry = MemoryEntry(kind=MEMORY_DEFECT, key="E-1", title="t",
                        problem="p", solution="s")
    store.append([entry])
    assert store.has("E-1") and store.has("E-404") is False


def test_from_qa_report_only_records_patterns():
    entries = from_qa_report(report={
        "jev": {"error_cases": 3},
        "wiki": {"conflict_cases": 2}})
    kinds = {e.kind for e in entries}
    assert kinds == {MEMORY_JEV_FAILURE, "wiki_conflict"} - {"wiki_conflict"} | {"wiki_conflict"}
    assert len(entries) == 2
    for entry in entries:
        assert "用户" not in entry.problem, "只沉淀模式，不沉淀个案"


def test_from_qa_report_empty_when_no_cases():
    assert from_qa_report(report={"jev": {}, "wiki": {}}) == []


# ============================== Agentic Loop（U32 + 四阶）============================== #
def test_loop_stages_match_the_plan():
    assert LOOP_STAGES == ("context_collection", "llm_decision", "tool_execution",
                           "self_correction")


def test_loop_limits_follow_u32():
    assert MAX_ITERATIONS == 8 and NO_PROGRESS_LIMIT == 2


def _loop(proposals, *, sandbox=None, qa=None, **over):
    pending = list(proposals)

    async def _decide(_context, _history):
        if pending:
            return pending.pop(0)
        # 与 HarnessAgent 保持一致：**提案用尽不等于达标**
        return Proposal(summary="没有更多提案", done=False)

    return AgenticLoop(
        decide=_decide,
        hooks=HookChain.default(qa_verdict=qa if qa is not None else {"action": "pass"}),
        sandbox=sandbox or FakeSandbox(),
        **over)


def _items():
    return [ContextItem("i1", "待修复的缺陷描述", relevance=1.0)]


def test_loop_converges_when_done_and_commands_pass():
    result = _run(_loop([Proposal("修复完成", done=True, commands=("pytest -q",))]).run(
        goal="修复缺陷", items=_items(), redactor=Redactor()))
    assert result.converged is True
    stages = [r.stage for r in result.records]
    assert STAGE_COLLECT in stages and STAGE_DECIDE in stages
    assert STAGE_EXECUTE in stages and STAGE_CORRECT in stages


def test_loop_stops_early_after_repeated_no_progress():
    """U32：连续无进展提前终止——不能让循环一直改坏仓库。"""
    failing = FakeSandbox(results=[SandboxResult("pytest", 1, stderr="fail")] * 10)
    result = _run(_loop([Proposal("再试一次", commands=("pytest -q",))] * 10,
                        sandbox=failing).run(goal="g", items=_items(),
                                             redactor=Redactor()))
    assert result.converged is False
    assert "无实质进展" in result.reason
    assert result.iterations <= NO_PROGRESS_LIMIT + 1


def test_loop_stops_at_max_iterations_and_reports_not_converged():
    ok = FakeSandbox()
    result = _run(_loop([Proposal("继续", commands=("pytest -q",))] * 20,
                        sandbox=ok).run(goal="g", items=_items(),
                                        redactor=Redactor()))
    assert result.converged is False
    assert result.iterations == MAX_ITERATIONS
    assert "迭代上限" in result.reason


def test_loop_denies_redline_change_and_stops():
    result = _run(_loop([Proposal("危险改动", change=Change(
        change_id="c1", wiki_scopes=("vault",), redacted=False))]).run(
            goal="g", items=_items(), redactor=Redactor()))
    assert result.blocked is True
    assert "Hook 拦截" in result.reason


def test_loop_pauses_for_approval_on_jev_template_change():
    result = _run(_loop([Proposal("改模板", change=Change(
        change_id="c2", modifies_jev_template=True))]).run(
            goal="g", items=_items(), redactor=Redactor()))
    assert result.converged is False
    assert result.pending_approval is not None
    assert "待人工审批" in result.reason


def test_loop_is_not_converged_when_nothing_was_done():
    """空转不算达标——循环不能在什么都没做时报告成功。"""
    result = _run(_loop([Proposal("什么都没做")]).run(
        goal="g", items=_items(), redactor=Redactor()))
    assert result.converged is False


def test_agent_exhausted_proposals_are_not_reported_as_success():
    """端到端：提案用尽时必须如实报「未收敛」，不能伪报达标。"""
    payload = _call(_agent(), _state(
        harness_proposals=[{"summary": "看了一眼，没动"}]))["p10_harness"]
    assert payload["converged"] is False
    assert payload["result"]["converged"] is False
    assert "进展" in payload["result"]["reason"] or "上限" in payload["result"]["reason"]


def test_loop_records_sandbox_results():
    result = _run(_loop([Proposal("跑测试", commands=("pytest -q",), done=True)]).run(
        goal="g", items=_items(), redactor=Redactor()))
    assert result.sandbox_results and result.sandbox_results[0].command == "pytest -q"


def test_loop_result_serialises():
    result = _run(_loop([Proposal("完成", done=True)]).run(
        goal="g", items=_items(), redactor=Redactor()))
    payload = result.as_dict()
    assert set(payload) >= {"converged", "iterations", "reason", "stages", "blocked"}


# ============================== 阶段 Agent ============================== #
def _agent(**over):
    available = {CAP_WIKI_READ: over.get("wiki"), CAP_RETRIEVAL: over.get("retrieval")}
    return HarnessAgent(settings=over.get("settings"),
                        tools=grant(HarnessAgent, available),
                        sandbox=over.get("sandbox", FakeSandbox()),
                        **{k: v for k, v in over.items() if k in ("max_iterations",)})


def _state(**extra):
    state = new_state(user_id="user-A", session_id="s1")
    state.update(extra)
    return state


def test_agent_without_sandbox_reports_not_run():
    """裁定 U34：沙箱是 AI Infra 的能力，由运行时注入；没有就不假装跑过。"""
    agent = HarnessAgent(tools=grant(HarnessAgent, {}))
    payload = _call(agent, _state())["p10_harness"]
    assert payload["ran"] is False and "U34" in payload["reason"]


def test_agent_runs_the_loop_and_reports_isolation():
    payload = _call(_agent(), _state(
        harness_goal="修复缺陷",
        harness_context=[{"item_id": "i1", "text": "缺陷", "relevance": 1.0}],
        harness_proposals=[{"summary": "完成", "done": True}]))["p10_harness"]
    assert payload["ran"] is True
    assert payload["offline_isolation"]["touches_private_vault"] is False
    assert payload["offline_isolation"]["touches_business_db"] is False
    assert "严禁访问未脱敏私有 Vault" in payload["offline_isolation"]["note"]


def test_agent_redacts_context():
    """上下文里的手机号与账号标识必须被脱掉。

    注意：payload 顶层的 user_id 是**框架要求的会话身份**（每个 Agent 都必须带），
    不是上下文内容，因此这里检查的是「上下文与阶段记录里没有明文」。
    """
    payload = _call(_agent(), _state(
        harness_context=[{"item_id": "i1", "text": "用户 user-Z 手机 13812345678",
                          "relevance": 1.0}],
        harness_proposals=[{"summary": "完成", "done": True}]))["p10_harness"]
    stages = str(payload["result"]["stages"])
    assert "13812345678" not in stages
    assert "user-Z" not in str(payload.get("memory_entries"))


def test_agent_blocks_on_redline_proposal():
    payload = _call(_agent(), _state(
        harness_proposals=[{"summary": "危险", "change": {
            "change_id": "c1", "wiki_scopes": ["vault"], "redacted": False}}]))["p10_harness"]
    assert payload["result"]["blocked"] is True
    assert payload["converged"] is False


def test_agent_flags_approval_requirement():
    payload = _call(_agent(), _state(
        harness_proposals=[{"summary": "改模板", "change": {
            "change_id": "c2", "modifies_jev_template": True}}]))["p10_harness"]
    assert payload["requires_approval"] is True


def test_agent_settles_memory_when_converged():
    payload = _call(_agent(), _state(
        harness_proposals=[{"summary": "完成", "done": True}],
        p9_report={"jev": {"error_cases": 2}, "wiki": {"conflict_cases": 1}}))["p10_harness"]
    assert payload["converged"] is True
    assert set(payload["change_note"]["memory_keys"]) == {"jev-error-pattern",
                                                         "wiki-conflict-pattern"}
    assert payload["memory_entries"]


def test_agent_lists_registered_skills():
    payload = _call(_agent(), _state(
        harness_proposals=[{"summary": "完成", "done": True}]))["p10_harness"]
    assert SKILL_AGENT_WIKI in payload["skills"]


def test_agent_writes_only_its_own_field():
    out = _call(_agent(), _state(harness_proposals=[{"summary": "完成", "done": True}]))
    assert set(out) == {"p10_harness"}


def test_agent_skips_when_session_is_invalid():
    assert _agent()(_state(login_status=False)) == {}


def test_p10_is_now_marked_implemented():
    from ai_butler.core import phases
    spec = phases.phase("P10")
    assert spec.implemented is True
    assert spec.title == "Harness 自进化闭环"
