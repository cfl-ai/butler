"""P10 Hook 安全拦截 —— 变更进入沙箱执行前的最后一道闸。

计划书依据
----------
- 表 10「Hook 安全校验：禁止移除 user_id 行级过滤与鉴权；禁止读取未脱敏私有 Vault；
  修改 Jev 模板、Wiki 公共知识库大规模变更强制人工审批」
- 三、P10 步骤3「接入 Hook 钩子体系，**联动质检 Agent 完成风险分级校验**」
- 三、P10 离线链路「Hook + 质检风险校验 → 沙箱执行代码修改与自测」

设计要点
--------
1. Hook 是**串联短路**的：任一 Hook 判 high 即中止，后续 Hook 不再放行该变更。
2. 「联动质检 Agent」体现为可注入的 QA Hook：质检结论为 block 时直接拦下。
3. 结论只有三种：~{allow}~ / ~{needs_approval}~ / ~{deny}~。
   **没有「警告但继续」这一档**——离线链路是自动改代码的，
   让它带着已知红线继续跑，风险不可控。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ai_butler.harness.risk import (
    FORBIDDEN_REDLINES,
    RISK_HIGH,
    RISK_LEVELS,
    REDLINES,
    Change,
    RiskAssessment,
    assess,
)

#: Hook 结论
HOOK_ALLOW = "allow"
HOOK_NEEDS_APPROVAL = "needs_approval"
HOOK_DENY = "deny"
HOOK_OUTCOMES: tuple[str, ...] = (HOOK_ALLOW, HOOK_NEEDS_APPROVAL, HOOK_DENY)


class HookError(RuntimeError):
    pass


@dataclass(frozen=True)
class HookFinding:
    hook: str
    outcome: str
    detail: str

    def __post_init__(self) -> None:
        if self.outcome not in HOOK_OUTCOMES:
            raise HookError(f"未知 Hook 结论 {self.outcome!r}")

    def as_dict(self) -> dict[str, Any]:
        return {"hook": self.hook, "outcome": self.outcome, "detail": self.detail}


@dataclass(frozen=True)
class HookVerdict:
    outcome: str
    risk: RiskAssessment
    findings: tuple[HookFinding, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.outcome == HOOK_ALLOW

    @property
    def blocked(self) -> bool:
        return self.outcome == HOOK_DENY

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "risk": self.risk.as_dict(),
                "findings": [f.as_dict() for f in self.findings]}


#: 一个 Hook 就是「变更 → 结论(可选)」的函数；返回 None 表示本 Hook 无异议
Hook = Callable[[Change, RiskAssessment], HookFinding | None]


def risk_grading_hook(change: Change, risk: RiskAssessment) -> HookFinding | None:
    """基础分级 Hook。

    **只拒绝「禁止」类红线**；「强制人工审批」类交给后面的专门 Hook 判为审批，
    否则会把 Jev 模板迭代这条规定流程整个堵死（这是本阶段实际踩过的错）。
    """
    if risk.forbidden:
        banned = [r for r in risk.redlines if r in FORBIDDEN_REDLINES]
        return HookFinding("risk_grading", HOOK_DENY,
                           "命中禁止类红线，不得自动执行：" + "；".join(banned))
    if risk.requires_approval:
        return HookFinding("risk_grading", HOOK_NEEDS_APPROVAL,
                           "高危变更须人工审批：" + "；".join(risk.redlines))
    return None


def unreduced_vault_hook(change: Change, risk: RiskAssessment) -> HookFinding | None:
    """表 10 第二条：禁止读取未脱敏私有 Vault。"""
    for scope in change.wiki_scopes:
        if scope in ("vault", "private_vault") and not change.redacted:
            return HookFinding("vault_scope", HOOK_DENY,
                               f"读取 {scope} 且未脱敏（表 10 明令禁止）")
    return None


def jev_template_hook(change: Change, risk: RiskAssessment) -> HookFinding | None:
    """表 10 第三条前半：修改 Jev 模板强制人工审批。"""
    if change.modifies_jev_template:
        return HookFinding("jev_template", HOOK_NEEDS_APPROVAL,
                           "修改 Jev 决策模板须人工审批")
    return None


def wiki_bulk_hook(change: Change, risk: RiskAssessment) -> HookFinding | None:
    """表 10 第三条后半：公共知识库大规模变更强制人工审批。"""
    from ai_butler.harness.risk import WIKI_BULK_THRESHOLD
    if change.wiki_pages_written >= WIKI_BULK_THRESHOLD:
        return HookFinding("wiki_bulk", HOOK_NEEDS_APPROVAL,
                           f"公共知识库变更 {change.wiki_pages_written} 页，规模过大")
    return None


def make_qa_hook(qa_verdict: Mapping[str, Any] | None) -> Hook:
    """联动质检 Agent（三、P10 步骤3）。

    质检结论为 block 时直接拒绝——**质检说不行的变更，Hook 不会放它进沙箱**。
    """
    def _hook(change: Change, risk: RiskAssessment) -> HookFinding | None:
        if not qa_verdict:
            return HookFinding("qa_link", HOOK_NEEDS_APPROVAL,
                               "未收到质检结论，按『联动质检』要求不放行")
        if qa_verdict.get("action") == "block":
            alerts = "；".join(qa_verdict.get("alerts") or []) or "存在隐私问题"
            return HookFinding("qa_link", HOOK_DENY, f"质检拦截：{alerts}")
        return None
    return _hook


#: 默认 Hook 链（顺序即优先级）
DEFAULT_HOOKS: tuple[tuple[str, Hook], ...] = (
    ("risk_grading", risk_grading_hook),
    ("vault_scope", unreduced_vault_hook),
    ("jev_template", jev_template_hook),
    ("wiki_bulk", wiki_bulk_hook),
)


@dataclass
class HookChain:
    """Hook 链。**任一 deny 即中止；任一 needs_approval 即需人工。**"""

    hooks: list[tuple[str, Hook]] = field(default_factory=list)

    @classmethod
    def default(cls, *, qa_verdict: Mapping[str, Any] | None = None) -> "HookChain":
        return cls(hooks=list(DEFAULT_HOOKS) + [("qa_link", make_qa_hook(qa_verdict))])

    def evaluate(self, change: Change, *,
                 wiki_bulk_threshold: int | None = None) -> HookVerdict:
        risk = assess(change) if wiki_bulk_threshold is None else assess(
            change, wiki_bulk_threshold=wiki_bulk_threshold)

        findings: list[HookFinding] = []
        outcome = HOOK_ALLOW
        for name, hook in self.hooks:
            try:
                finding = hook(change, risk)
            except Exception as error:                     # Hook 自身出错 = 不放行
                findings.append(HookFinding(name, HOOK_DENY, f"Hook 执行异常：{error}"))
                return HookVerdict(HOOK_DENY, risk, tuple(findings))
            if finding is None:
                continue
            findings.append(finding)
            if finding.outcome == HOOK_DENY:
                return HookVerdict(HOOK_DENY, risk, tuple(findings))
            if finding.outcome == HOOK_NEEDS_APPROVAL:
                outcome = HOOK_NEEDS_APPROVAL
        return HookVerdict(outcome, risk, tuple(findings))
