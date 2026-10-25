"""评测缺陷单 —— 把「不通过」翻译成**可以处置的东西**。

为什么要有这一层
----------------
`EvaluationPlatform` 只会说「X 维度未通过，证据是……」。这对人够用，
对自动修复不够：它没说是哪一类问题、该谁处理、从哪改、怎么复现。

于是出现了最尴尬的局面 —— 评测中台能判「不行」，但判完就结束了，
P10 的 Agentic Loop 那头 `harness_proposals` **全项目没有任何生产者**，
只能等外部 JSON 喂。两边都在，中间没线（E-77 的第三种形态：
不是没实现，是**两个已实现的东西没接上**）。

本模块补的就是这条线：`PlatformReport` → `Defect` → 交给 harness 受理。

三条不可让的处置规则
--------------------
1. **安全缺陷永远不自动修**（`ROUTE_MANUAL`）。
   表 10 第一条明令「禁止移除 user_id 行级过滤与鉴权」。
   如果让离线链路自动去改权限代码，等于**让被评的缺陷自己改判据**——
   改完测试是绿的，漏洞还在。这类缺陷只能人工看，且必须阻断其余自动修复
   （见 `DefectBatch.blocked_by_security`）。
2. **无痕迹不产出可自动修复的缺陷**（`ROUTE_MANUAL`）。
   没有痕迹就没有定位依据；自动化在这种情况下唯一能做的是猜。
   「没数据」与「没问题」必须分开（与平台层第 4 条同一立场）。
3. **改动 Jev 决策模板的一律走审批**（`ROUTE_APPROVAL`）。
   表 10 第三条把这条列为「强制人工审批」，不是「禁止」。**比计划书更严
   不等于更对**（risk.py 也踩过这个坑：把四条红线都按禁止处理，把
   Jev 模板迭代这条规定流程整个堵死）。

其余维度按「缺陷 → 最可能该动的地方」映射成 `target_hint`。
它只是**收敛提示**，不是断言——写入变更单时标注为建议，
让决策层（人或模型）自己判断，不冒充已经定位到根因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ai_butler.evaluation.dimensions import (
    COST_EFFICIENCY,
    DIMENSION_KEYS,
    DIMENSION_TITLES,
    EVIDENCE_FAITHFULNESS,
    OUTPUT_QUALITY,
    RETRIEVAL_RECALL,
    SECURITY_PERMISSIONS,
    STABILITY_AND_EXCEPTIONS,
    TOOL_CALL_AUTHENTICITY,
)
from ai_butler.evaluation.platform import PlatformReport, Verdict
from ai_butler.evaluation.redaction import Redactor


class DefectKind(str, Enum):
    """缺陷类别。**只收「处置方式不同」的类别**——分得再细但不改动作就没意义。"""

    NO_DATA = "no_data"                    # 无痕迹：无从定位
    CONFIG_GAP = "config_gap"              # 评测输入不全：**不是系统缺陷**
    SECURITY_VIOLATION = "security_violation"   # 越权 / 泄露 / 该拒答没拒
    RECALL_GAP = "recall_gap"              # 该召回的素材没召回
    MISSING_EVIDENCE = "missing_evidence"  # 结论没有证据支撑
    FALSE_TOOL_CALL = "false_tool_call"    # 该调的工具没调 / 调了没结果
    UNSTABLE = "unstable"                  # 同一问题多次运行结论不一致
    COST_REGRESSION = "cost_regression"    # 相对基线变慢变贵
    QUALITY_GAP = "quality_gap"            # 答案缺要点 / 与期望答不符


class DefectSeverity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"


class Disposition(str, Enum):
    """处置路由。三种，没有「先改了再说」。"""

    AUTO = "auto"            # 可自动提案（仍需过 Hook 与沙箱）
    APPROVAL = "approval"    # 可自动提案，但必须人工审批后才可落地
    MANUAL = "manual"        # 自动修复有害，只能人工


#: 维度 → (缺陷类别, 严重度, 处置, 收敛提示, 不自动修的理由)
#:
#: 这张表是本模块的**唯一真值源**：不在这里登记的维度不会被产出缺陷，
#: 而不是散在 if 里——否则加一个维度就有地方忘记处置规则。
#:
#: `target_hint` 一律写**仓库相对的全路径**（`ai_butler/xxx/yyy.py`）。
#: 早先写成「retrieval/（候选数、阈值…）」这类模糊描述，人读得懂，
#: 但下游要拿它去读源码片段时一个字也提取不出来——
#: 代码创作层给模型的上下文会是空的，然后模型只能凭空编。
_DIMENSION_RULES: Mapping[str, tuple[DefectKind, DefectSeverity, Disposition,
                                    str, str]] = {
    SECURITY_PERMISSIONS: (
        DefectKind.SECURITY_VIOLATION, DefectSeverity.BLOCKER, Disposition.MANUAL,
        "ai_butler/core/permissions.py、ai_butler/agents/（越权路径的调用点）",
        "自动修改权限与鉴权代码，正是表 10 第一条红线的反面：被评的缺陷会去改判据，"
        "改完测试是绿的、漏洞还在。安全缺陷只能人工处置"),
    RETRIEVAL_RECALL: (
        DefectKind.RECALL_GAP, DefectSeverity.MAJOR, Disposition.AUTO,
        "ai_butler/retrieval/chat.py、ai_butler/retrieval/planner.py"
        "（候选数 top_k、检索阈值与素材侧过滤）",
        ""),
    EVIDENCE_FAITHFULNESS: (
        DefectKind.MISSING_EVIDENCE, DefectSeverity.MAJOR, Disposition.AUTO,
        "ai_butler/retrieval/verification.py、ai_butler/retrieval/chat.py"
        "（回答组装段的证据引用与复检）",
        ""),
    TOOL_CALL_AUTHENTICITY: (
        DefectKind.FALSE_TOOL_CALL, DefectSeverity.MAJOR, Disposition.AUTO,
        "ai_butler/retrieval/planner.py（CATEGORY_TOOLS 与工具注册）",
        ""),
    STABILITY_AND_EXCEPTIONS: (
        DefectKind.UNSTABLE, DefectSeverity.MAJOR, Disposition.AUTO,
        "ai_butler/scheduler/tools.py、ai_butler/scheduler/router.py"
        "（超时、重试与降级路径）",
        ""),
    COST_EFFICIENCY: (
        DefectKind.COST_REGRESSION, DefectSeverity.MINOR, Disposition.AUTO,
        "ai_butler/retrieval/chat.py（top_k 与提示词长度）、"
        "ai_butler/harness/context.py",
        ""),
    OUTPUT_QUALITY: (
        DefectKind.QUALITY_GAP, DefectSeverity.MINOR, Disposition.AUTO,
        "ai_butler/retrieval/chat.py（回答组装与提示词），"
        "必要时是检索召回的上游问题",
        ""),
}

#: 缺陷类别 → 复现要点（harness 受理时按这个去沙箱里验证，不靠自由发挥）
_REPRO_HINTS: Mapping[DefectKind, str] = {
    DefectKind.NO_DATA: "为同一问题重跑一次并落痕迹，确认埋点是否缺失",
    DefectKind.CONFIG_GAP: "补齐该用例的金标准/运行次数后重评，不跑修复",
    DefectKind.SECURITY_VIOLATION: "重放该越权/敏感用例，确认是否仍被放行",
    DefectKind.RECALL_GAP: "用金标准 relevant_ids 重跑检索，看漏了哪些 id",
    DefectKind.MISSING_EVIDENCE: "把答案与证据逐条对齐，看哪句无出处",
    DefectKind.FALSE_TOOL_CALL: "看 plan.tools 与 required_tools 的差集",
    DefectKind.UNSTABLE: "同一问题连续 N 次，比较分类/工具序列/结论",
    DefectKind.COST_REGRESSION: "对比基线的延迟与 token 分项",
    DefectKind.QUALITY_GAP: "按 expected_points 逐条核对答案覆盖",
}

#: 维度结果里的 `inconclusive` 标记 → 「缺哪样输入」的人话。
#:
#: 走 `DimensionResult.details` 的**结构化字段**而不是去匹配 evidence 里的
#: 中文文案：文案一改判据就失效，而且中文匹配本身就是 E-44 那类漂移源。
#: 维度层负责说「判不了」，本层负责决定「判不了该记成什么」。
_INCONCLUSIVE: Mapping[str, str] = {
    "missing_relevant_ids": "用例未给检索金标准（relevant_ids）",
    "missing_evidence_texts": "用例未给证据文本（evidence_texts）",
    "missing_expected": "用例未给期望答案或必备要点",
    "insufficient_runs": "同一问题只跑了 1 次，稳定性无从验证",
}


class DefectError(ValueError):
    """缺陷单输入不合法。"""


@dataclass(frozen=True)
class Defect:
    """一条可处置的缺陷。

    `defect_id` 由「用例 + 维度」构成且**稳定**：同一用例同一维度反复失败，
    编号不变，便于跨轮次看趋势；不会因为换个序号就把老缺陷当新缺陷。
    """

    defect_id: str
    case_id: str
    question: str
    dimension: str
    kind: DefectKind
    severity: DefectSeverity
    disposition: Disposition
    title: str
    evidence: str
    #: 收敛提示（建议改动位置）。**不是根因断言**，写入变更单时如实标注
    target_hint: str
    #: 不自动处置的理由（manual 必填；其余为空）
    blocked_reason: str = ""
    repro: str = ""

    def __post_init__(self) -> None:
        if not self.defect_id.strip():
            raise DefectError("缺陷必须有 defect_id")
        if self.disposition is Disposition.MANUAL and not self.blocked_reason.strip():
            raise DefectError(
                f"缺陷 {self.defect_id} 判为人工处置却没说理由——"
                "「不能自动修」必须有可复核的依据")

    @property
    def auto_repairable(self) -> bool:
        return self.disposition is Disposition.AUTO

    @property
    def priority(self) -> str:
        return {DefectSeverity.BLOCKER: "P0", DefectSeverity.MAJOR: "P1",
                DefectSeverity.MINOR: "P2"}[self.severity]

    def as_dict(self, redactor: Redactor | None = None) -> dict[str, Any]:
        """出口形态。**问题文本必经脱敏**——缺陷单会进看板与工单。"""
        red = redactor or Redactor()
        payload = {
            "defect_id": self.defect_id,
            "case_id": self.case_id,
            "question": self.question,
            "dimension": self.dimension,
            "dimension_title": DIMENSION_TITLES.get(self.dimension, self.dimension),
            "kind": self.kind.value,
            "severity": self.severity.value,
            "priority": self.priority,
            "disposition": self.disposition.value,
            "title": self.title,
            "evidence": self.evidence,
            "target_hint": self.target_hint,
            "blocked_reason": self.blocked_reason,
            "repro": self.repro,
        }
        safe = red.redact_deep(payload)
        red.assert_deep_clean(safe, where=f"缺陷单 {self.defect_id}")
        return safe


@dataclass
class DefectBatch:
    """一批缺陷 + 整批的处置闸门。

    `blocked_by_security` 是**整批级**的否决：只要存在未解决的安全缺陷，
    其他缺陷一律不许自动处置。理由很直接——在一个「会越权泄露余额」的系统上
    去优化检索召回率，是把力气花在错的地方，而且自动改代码会让排查变难。
    """

    defects: tuple[Defect, ...] = ()
    #: 本轮评测覆盖的用例数（用于区分「全过」与「没评」）
    case_count: int = 0

    def by_severity(self) -> dict[str, int]:
        counts = {s.value: 0 for s in DefectSeverity}
        for d in self.defects:
            counts[d.severity.value] += 1
        return counts

    def of(self, disposition: Disposition) -> tuple[Defect, ...]:
        return tuple(d for d in self.defects if d.disposition is disposition)

    @property
    def blocked_by_security(self) -> bool:
        return any(d.kind is DefectKind.SECURITY_VIOLATION for d in self.defects)

    @property
    def clean(self) -> bool:
        """没有任何缺陷。**注意与「没评」区分**：case_count=0 时也 clean，
        但那不叫干净，叫没测——调用方必须看 case_count。"""
        return not self.defects

    def auto_repairable(self) -> tuple[Defect, ...]:
        """可交给 harness 自动处置的缺陷。

        安全缺陷存在时**返回空**：见类注释。这不是保守，是排序——
        安全没解决前，自动化的产出没有可信度。
        """
        if self.blocked_by_security:
            return ()
        return self.of(Disposition.AUTO)

    def as_dict(self, redactor: Redactor | None = None) -> dict[str, Any]:
        red = redactor or Redactor()
        auto = self.auto_repairable()
        return {
            "case_count": self.case_count,
            "defect_count": len(self.defects),
            "by_severity": self.by_severity(),
            "blocked_by_security": self.blocked_by_security,
            "auto_repairable": len(auto),
            "manual_count": len(self.of(Disposition.MANUAL)),
            "approval_count": len(self.of(Disposition.APPROVAL)),
            "defects": [d.as_dict(red) for d in self.defects],
        }


# --------------------------------------------------------------------------- #
def _title_for(case: PlatformReport, kind: DefectKind) -> str:
    title = {
        DefectKind.NO_DATA: "无运行痕迹，无从评测",
        DefectKind.CONFIG_GAP: "评测输入不全，该维度判不了",
        DefectKind.SECURITY_VIOLATION: "安全与权限未通过（一票否决）",
        DefectKind.RECALL_GAP: "检索召回不足",
        DefectKind.MISSING_EVIDENCE: "结论缺少证据支撑",
        DefectKind.FALSE_TOOL_CALL: "应有的工具调用缺失或未生效",
        DefectKind.UNSTABLE: "多次运行结论不稳定",
        DefectKind.COST_REGRESSION: "成本或延迟劣于基线",
        DefectKind.QUALITY_GAP: "答案质量未达要点要求",
    }[kind]
    return f"{title}（用例 {case.case_id}）"


def defects_from_report(report: PlatformReport) -> tuple[Defect, ...]:
    """一份报告 → 若干缺陷。

    规则：
    - **通过的用例不产缺陷**（没坏就别动）。
    - 不适用维度不产缺陷（不适用 ≠ 不通过，把「没测」当「坏了」会制造假工单）。
    - 安全维度优先：它一票否决，且必须在缺陷单里排第一。
    """
    if report.passed:
        return ()

    # 无痕迹是**独立的**一类，且必须先判：没有痕迹时七个维度全部不通过，
    # 但产七张单就等于把一个「埋点没接」的问题说成七个问题，工单池立刻被噪声塞满。
    # 提前返回还有个好处——不会先构造出一条假的「安全违规」再丢掉。
    if report.runs == 0:
        return (Defect(
            defect_id=f"{report.case_id}/no_trace",
            case_id=report.case_id,
            question=report.question,
            dimension="",
            kind=DefectKind.NO_DATA,
            severity=DefectSeverity.MAJOR,
            disposition=Disposition.MANUAL,
            title=_title_for(report, DefectKind.NO_DATA),
            evidence=report.reason,
            target_hint="痕迹生产端（各阶段 Agent 的埋点接线）",
            blocked_reason="没有痕迹就没有定位依据；自动修复只能靠猜，"
                           "且「无痕迹」本身是埋点缺失，属接线问题",
            repro=_REPRO_HINTS[DefectKind.NO_DATA]),)

    out: list[Defect] = []
    # 安全排最前：它是 blocker，阅读顺序上就该先看到
    ordered = (SECURITY_PERMISSIONS,) + tuple(
        k for k in DIMENSION_KEYS if k != SECURITY_PERMISSIONS)

    for key in ordered:
        result = report.dimensions.get(key)
        if result is None or not result.applicable or result.passed:
            continue

        # 「判不了」与「判不过」必须分开（平台层第 4 条）：
        # 用例没给金标准、只跑了一次，这些是**评测输入不全**，
        # 不是系统有问题。把它们记成系统缺陷会让工单池被噪声塞满，
        # 真正的缺陷反而被稀释——这也是「没数据」与「没问题」必须分开的
        # 同一个理由，只是发生在另一个层面。
        inconclusive = result.details.get("inconclusive")
        if inconclusive:
            out.append(Defect(
                defect_id=f"{report.case_id}/{key}",
                case_id=report.case_id, question=report.question,
                dimension=key, kind=DefectKind.CONFIG_GAP,
                severity=DefectSeverity.MINOR, disposition=Disposition.MANUAL,
                title=_title_for(report, DefectKind.CONFIG_GAP),
                evidence=f"{result.evidence}（{_INCONCLUSIVE.get(inconclusive, inconclusive)}）",
                target_hint="评测用例配置（cases/dataset）或运行次数",
                blocked_reason="这是评测输入不全，不是被测系统的问题；"
                               "自动修改被测代码只会掩盖评测本身的缺口",
                repro=_REPRO_HINTS[DefectKind.CONFIG_GAP]))
            continue

        rule = _DIMENSION_RULES.get(key)
        if rule is None:
            # 维度无处置规则：**不猜**，登记成质量缺陷并转人工
            kind, severity, disposition = (DefectKind.QUALITY_GAP,
                                           DefectSeverity.MINOR,
                                           Disposition.MANUAL)
            hint, blocked = "", f"维度 {key} 未登记处置规则，按人工处理（不猜）"
        else:
            kind, severity, disposition, hint, blocked = rule

        out.append(Defect(
            defect_id=f"{report.case_id}/{key}",
            case_id=report.case_id,
            question=report.question,
            dimension=key,
            kind=kind,
            severity=severity,
            disposition=disposition,
            title=_title_for(report, kind),
            evidence=result.evidence,
            target_hint=hint,
            blocked_reason=blocked,
            repro=_REPRO_HINTS[kind]))

    return tuple(out)


def defects_from_reports(
        reports: Sequence[PlatformReport], *,
        redactor: Redactor | None = None) -> DefectBatch:
    """多份报告 → 一批缺陷（去重、排序、脱敏前的一致形态）。"""
    seen: set[str] = set()
    defects: list[Defect] = []
    for report in reports:
        for defect in defects_from_report(report):
            if defect.defect_id in seen:
                # 同一用例同一维度重复出现（多份报告）：保留首条即可。
                # 合并 evidence 会让「哪条报告的结论」变得说不清。
                continue
            seen.add(defect.defect_id)
            defects.append(defect)

    # 排序：blocker 先，然后 P1、P2；同档按 defect_id 稳定排序（可复现）
    order = {DefectSeverity.BLOCKER: 0, DefectSeverity.MAJOR: 1,
             DefectSeverity.MINOR: 2}
    defects.sort(key=lambda d: (order[d.severity], d.defect_id))
    return DefectBatch(defects=tuple(defects), case_count=len(reports))


def synthetic_report(case_id: str, question: str, *,
                     reason: str = "没有运行痕迹") -> PlatformReport:
    """构造一份「无痕迹」报告，供评测链路的空输入路径使用。

    评测中台对「用例在、痕迹没有」的处理是 fail-closed（平台层第 4 条）。
    这个构造函数让该情形也能产出缺陷单，而不是让调用方自己拼一个
    字段不全的 `PlatformReport`（字段不全的报告比没有报告更误导）。
    """
    from ai_butler.evaluation.dimensions import DimensionResult

    dimensions = {k: DimensionResult(key=k, score=0.0, passed=False,
                                     applicable=True, evidence=reason)
                  for k in DIMENSION_KEYS}
    return PlatformReport(
        case_id=case_id, question=question, generated_at="",
        runs=0, dimensions=dimensions, overall_score=0.0,
        verdict=Verdict.FAIL, vetoed=False, reason=reason)


# --------------------------------------------------------------------------- #
# 跨进程还原：State 里只有可 JSON 化的 dict（与全项目一致的纪律）
# --------------------------------------------------------------------------- #
def defect_from_dict(payload: Mapping[str, Any]) -> Defect:
    """dict → Defect。

    枚举一律**显式转换**，未知取值直接报错：带着一个认不出的 severity 继续跑，
    下游的优先级排序会静默错位（比直接失败更坏）。
    """
    if not isinstance(payload, Mapping):
        raise DefectError(f"缺陷应为 dict，实际 {type(payload).__name__}")
    try:
        kind = DefectKind(str(payload.get("kind")))
        severity = DefectSeverity(str(payload.get("severity")))
        disposition = Disposition(str(payload.get("disposition")))
    except ValueError as error:
        raise DefectError(f"缺陷 {payload.get('defect_id')!r} 字段取值非法：{error}") from None
    return Defect(
        defect_id=str(payload.get("defect_id") or ""),
        case_id=str(payload.get("case_id") or ""),
        question=str(payload.get("question") or ""),
        dimension=str(payload.get("dimension") or ""),
        kind=kind, severity=severity, disposition=disposition,
        title=str(payload.get("title") or ""),
        evidence=str(payload.get("evidence") or ""),
        target_hint=str(payload.get("target_hint") or ""),
        blocked_reason=str(payload.get("blocked_reason") or ""),
        repro=str(payload.get("repro") or ""))


def batch_from_dict(payload: Mapping[str, Any]) -> DefectBatch:
    """`DefectBatch.as_dict()` 的逆。

    `blocked_by_security` **不读输入里的布尔值，而是从缺陷重新推导**：
    信任一个可以随手改的标记，等于让调用方决定「安全缺陷能不能自动修」。
    """
    if not isinstance(payload, Mapping):
        raise DefectError(f"缺陷批次应为 dict，实际 {type(payload).__name__}")
    raw = payload.get("defects") or ()
    if not isinstance(raw, (list, tuple)):
        raise DefectError("缺陷批次的 defects 应为列表")
    return DefectBatch(
        defects=tuple(defect_from_dict(item) for item in raw),
        case_count=int(payload.get("case_count") or 0))
