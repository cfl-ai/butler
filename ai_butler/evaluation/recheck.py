"""评测中台复评 —— 判 harness 改完之后「修好了没」与「有没有乱改」。

计划书依据
----------
- 三、P10 离线链路「沙箱执行代码修改与自测 → **评测迭代效果，未达标持续循环修复** →
  达标后沉淀记忆、生成版本变更单 → 人工审批后台审核」
- 三、P9「评测中台对全链路做客观评价」——**评价方是评测中台，不是改代码的一方**
- 表 10「禁止移除 user_id 行级过滤与鉴权」

为什么「复评」必须与 harness 分离
----------------------------------
计划书把「评测迭代效果」放在 harness 的链路里，但**评价的主体自始至终是
评测中台**。这条边界一旦模糊，闭环立刻变成自证：

    harness 改了代码 → harness 说自己修好了 → 上线

所以本模块只有评测中台调用，且**只接受两类证据**：
修复前的原报告（before）与本次重跑的新痕迹（after）。
harness 自己的任何声称（`patch_applied` / `converged` / `done`）都不作为输入。

两个互不替代的判据
------------------
用户的要求是「确保 harness 没有出错和胡乱修改，且成功解决问题」。
这是**两件事**，混成一个总分就两头都测不准：

- **没乱改**（`audit`）：改动范围、是否削弱测试、是否移除保护、规模是否失控。
  这是**结构判据**，只看 diff 就能判，不需要跑起来。
- **修好了**（`recheck`）：同一批用例重跑，目标缺陷是否消失、有没有新缺陷。
  这是**效果判据**，必须真跑。

结构判据不过 → 效果再好也打回。因为「让测试变绿」和「把问题修好」
在只看结果时长得一模一样，只有看改动才能区分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, Sequence

from ai_butler.evaluation.defects import DefectBatch, defects_from_reports
from ai_butler.evaluation.platform import (
    EvaluationCase,
    EvaluationPlatform,
    PlatformReport,
)
from ai_butler.evaluation.redaction import Redactor
from ai_butler.evaluation.trace import AgentTrace
from ai_butler.harness.patch import (
    ALLOWED_SCOPE_BY_KIND,
    FORBIDDEN_PATHS,
    MAX_TOUCHED_LINES,
    parse_unified_diff,
)

__all__ = [
    "AuditFinding",
    "AuditSeverity",
    "ChangeAudit",
    "audit_change",
    "RecheckVerdict",
    "RecheckResult",
    "TraceProducer",
    "Rechecker",
]


# --------------------------------------------------------------------------- #
# 一、结构判据：改动审计
# --------------------------------------------------------------------------- #
class AuditSeverity(str, Enum):
    BLOCKING = "blocking"    # 直接打回，不看效果
    WARNING = "warning"      # 记录并转人工复核，可继续跑


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: AuditSeverity
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity.value,
                "detail": self.detail}


@dataclass(frozen=True)
class ChangeAudit:
    """一次改动的审计结论。`blocking` 非空即打回。"""

    findings: tuple[AuditFinding, ...] = ()
    paths: tuple[str, ...] = ()
    touched: int = 0

    @property
    def blocking(self) -> tuple[AuditFinding, ...]:
        return tuple(f for f in self.findings
                     if f.severity is AuditSeverity.BLOCKING)

    @property
    def passed(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "paths": list(self.paths),
                "touched": self.touched,
                "blocking": len(self.blocking),
                "findings": [f.as_dict() for f in self.findings]}


#: 被删除的断言 / 新增的跳过标记。这是「让测试变绿」最省力的两条路。
_DELETED_ASSERT = re.compile(r"^\s*assert\b")
_ADDED_SKIP = re.compile(
    r"^\s*(?:@?pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.skip\b|"
    r"unittest\.skip\b|@unittest\.skip)")

#: 删行里出现这些字样，说明可能移除了保护（表 10 第一条）。
_GUARD_HINTS: tuple[str, ...] = (
    "user_id", "user_scope", "row_level", "行级隔离",
    "require_user", "authenticate", "鉴权", "permission",
)


def audit_change(diff: str, *, kind: str, allowed_prefixes: Sequence[str] = ()) -> ChangeAudit:
    """审计一份 diff。**只看文本，不跑代码**，所以任何补丁都能先过这一关。

    这一关存在的意义：让「删掉断言」这类改动在**跑测试之前**就被拦下。
    等它跑绿了再判，已经晚了一步——那时看到的只是一个更宽松的测试集。
    """
    findings: list[AuditFinding] = []
    try:
        patches = parse_unified_diff(diff)
    except Exception as exc:                            # noqa: BLE001
        return ChangeAudit((AuditFinding(
            "unparsable", AuditSeverity.BLOCKING,
            f"补丁无法解析，无法审计：{exc}"),))

    scope = tuple(allowed_prefixes) or ALLOWED_SCOPE_BY_KIND.get(kind, ())

    for fp in patches.files:
        if any(fp.path == p.rstrip("/") or fp.path.startswith(p)
               for p in FORBIDDEN_PATHS):
            findings.append(AuditFinding(
                "forbidden_path", AuditSeverity.BLOCKING,
                f"{fp.path} 属全局禁改路径（改测试或评测判据会让缺陷「自己消失」）"))
        elif scope and not any(fp.path == p or fp.path.startswith(p) for p in scope):
            findings.append(AuditFinding(
                "out_of_scope", AuditSeverity.BLOCKING,
                f"{fp.path} 不在 {kind} 允许范围 {list(scope)} 内"))

        for line in _diff_lines(fp):
            body = line[1:].strip() if len(line) > 1 else ""
            if line.startswith("-") and not line.startswith("---"):
                if _DELETED_ASSERT.match(body):
                    findings.append(AuditFinding(
                        "assert_removed", AuditSeverity.BLOCKING,
                        f"{fp.path} 删除了断言：{body[:80]!r}。"
                        f"删断言能让测试变绿而缺陷仍在，属作弊"))
                if any(h in body.lower() for h in _GUARD_HINTS):
                    findings.append(AuditFinding(
                        "guard_maybe_removed", AuditSeverity.WARNING,
                        f"{fp.path} 删除了含保护标记的行：{body[:80]!r}，转人工复核"))
            if line.startswith("+") and not line.startswith("+++"):
                if _ADDED_SKIP.match(body):
                    findings.append(AuditFinding(
                        "skip_added", AuditSeverity.BLOCKING,
                        f"{fp.path} 新增跳过标记：{body[:80]!r}"))

    if patches.touched > MAX_TOUCHED_LINES:
        findings.append(AuditFinding(
            "size_blowup", AuditSeverity.BLOCKING,
            f"改动 {patches.touched} 行超过上限 {MAX_TOUCHED_LINES}：这是重写不是修复"))

    return ChangeAudit(tuple(findings), patches.paths, patches.touched)


def _diff_lines(fp: Any) -> Sequence[str]:
    out: list[str] = []
    for hunk in fp.hunks:
        out.extend(hunk.lines)
    return out


# --------------------------------------------------------------------------- #
# 二、效果判据：复评
# --------------------------------------------------------------------------- #
class TraceProducer(Protocol):
    """重新产出运行痕迹的端口。

    复评必须用**新痕迹**，不能复用修复前那份——那份正是「有问题」的证据。
    真实现由运行时注入（接真实管道重放），测试用桩。

    **实现者必须遵守的契约（踩过，会给出完全错误的结论）**
    ------------------------------------------------------
    补丁是落到**磁盘**上的，而 Python 的模块对象在进程里是**已加载的缓存**。
    如果复评在「应用补丁之前就已 import 过被测模块」的进程里执行，
    它跑的是旧代码，于是会稳定地报 `REJECTED_NO_EFFECT`——
    看起来像「改了半天没用」，实际上是**评了旧代码**。

    真实环境中这不成问题（服务会重启），但离线链路里补丁应用与复评同进程，
    必须由 producer 自己保证：**重启进程，或显式 reload 被测模块**。
    没有把握时宁可抛异常（复评会判 UNVERIFIED），
    也不要交出一份基于旧代码的「没修好」。
    """

    async def produce(self, case: EvaluationCase) -> Sequence[AgentTrace]: ...


class RecheckVerdict(str, Enum):
    """复评结论。**四态 + 一个「不知道」，没有布尔成功。**"""

    ACCEPTED = "accepted"                  # 目标修好、无回归、无阻断违规
    REJECTED_AUDIT = "rejected_audit"      # 乱改：触发阻断级审计发现
    REJECTED_REGRESSION = "rejected_regression"  # 改坏了：原来通过的现在不过
    REJECTED_NO_EFFECT = "rejected_no_effect"    # 白改：目标缺陷一条没消失
    UNVERIFIED = "unverified"              # 没能复评（无 producer / 全部报错）

    @property
    def accepted(self) -> bool:
        return self is RecheckVerdict.ACCEPTED


@dataclass(frozen=True)
class RecheckResult:
    verdict: RecheckVerdict
    reason: str
    audit: ChangeAudit | None = None
    case_count: int = 0
    fixed: tuple[str, ...] = ()
    regressed: tuple[str, ...] = ()
    still_broken: tuple[str, ...] = ()
    errored: tuple[str, ...] = ()
    #: 修复前后缺陷数，供看板与落账
    before_defects: int = 0
    after_defects: int = 0
    #: 新产出的缺陷（提示「改出了新问题」，与 regressed 区分：
    #: 后者是原有用例由过转过，前者是出现了以往没报过的问题）
    new_kinds: tuple[str, ...] = ()

    def as_dict(self, redactor: Redactor | None = None) -> dict[str, Any]:
        red = redactor or Redactor()
        payload = {
            "verdict": self.verdict.value,
            "accepted": self.verdict.accepted,
            "reason": self.reason,
            "audit": self.audit.as_dict() if self.audit else None,
            "case_count": self.case_count,
            "fixed": list(self.fixed),
            "regressed": list(self.regressed),
            "still_broken": list(self.still_broken),
            "errored": list(self.errored),
            "before_defects": self.before_defects,
            "after_defects": self.after_defects,
            "new_kinds": list(self.new_kinds),
        }
        safe = red.redact_deep(payload)
        red.assert_deep_clean(safe, where="修复复评结论")
        return safe


@dataclass
class Rechecker:
    """评测中台侧的复评器。**由评测中台调用，harness 不得调用。**"""

    platform: EvaluationPlatform
    producer: TraceProducer | None = None
    runs: int = 2

    async def recheck(self, *, cases: Sequence[EvaluationCase],
                      before_reports: Sequence[PlatformReport],
                      diff: str = "", kind: str = "",
                      allowed_prefixes: Sequence[str] = ()) -> RecheckResult:
        """复评。顺序是**先审计再复跑**：

        审计不过就不该浪费一次真实评测（复跑可能要调模型、花钱），
        更重要的是：乱改的补丁即使「效果变好」也不能接受，
        因为它的效果可能来自「把断言删了」。
        """
        audit = None
        if diff.strip():
            audit = audit_change(diff, kind=kind, allowed_prefixes=allowed_prefixes)
            if not audit.passed:
                return RecheckResult(
                    RecheckVerdict.REJECTED_AUDIT,
                    "改动审计未通过：" + "；".join(
                        f.detail for f in audit.blocking),
                    audit=audit, case_count=len(cases))

        before = {r.case_id: r for r in before_reports}
        missing = [c.case_id for c in cases if c.case_id not in before]
        if missing:
            # 没有修复前的基线，就无从比较「变好了没」。不拿「现在通过」顶替。
            return RecheckResult(
                RecheckVerdict.UNVERIFIED,
                f"缺少修复前基线报告，无法对比：{missing}", audit=audit,
                case_count=len(cases))

        if self.producer is None:
            # 没有重跑能力时如实标注，**绝不据「补丁已应用」推断修好了**
            return RecheckResult(
                RecheckVerdict.UNVERIFIED,
                "未注入 TraceProducer，无法重跑用例：改动是否有效未经证实",
                audit=audit, case_count=len(cases),
                before_defects=_count_defects(before_reports))

        fixed: list[str] = []
        regressed: list[str] = []
        still_broken: list[str] = []
        errored: list[str] = []
        after_reports: list[PlatformReport] = []

        for case in cases:
            try:
                traces = tuple(await self.producer.produce(case))
            except Exception as exc:                    # noqa: BLE001
                errored.append(f"{case.case_id}: {type(exc).__name__}: {exc}")
                continue
            if not traces:
                errored.append(f"{case.case_id}: 未产出任何痕迹")
                continue
            try:
                after = self.platform.evaluate(traces, case)
            except Exception as exc:                    # noqa: BLE001
                errored.append(f"{case.case_id}: 评测异常 {type(exc).__name__}: {exc}")
                continue
            after_reports.append(after)
            was_ok = before[case.case_id].passed
            now_ok = after.passed
            if now_ok and not was_ok:
                fixed.append(case.case_id)
            elif was_ok and not now_ok:
                regressed.append(case.case_id)
            elif not now_ok:
                still_broken.append(case.case_id)

        before_n = _count_defects(before_reports)
        after_batch = defects_from_reports(after_reports) if after_reports else DefectBatch()
        new_kinds = tuple(sorted({d.kind.value for d in after_batch.defects}))

        if regressed:
            verdict = RecheckVerdict.REJECTED_REGRESSION
            reason = (f"{len(regressed)} 个原本通过的用例在改动后不通过："
                      f"{regressed}。自动变更不得以牺牲已有正确性为代价")
        elif fixed:
            verdict = RecheckVerdict.ACCEPTED
            reason = (f"目标缺陷已消失（{len(fixed)} 个用例由不通过转为通过），"
                      f"无回归。审计：{'通过' if audit is None or audit.passed else '通过'}")
        elif errored and len(errored) == len(cases):
            verdict = RecheckVerdict.UNVERIFIED
            reason = f"全部用例复评失败，无法判定：{errored[:3]}"
        else:
            verdict = RecheckVerdict.REJECTED_NO_EFFECT
            reason = (f"改动后没有一个目标用例转为通过（仍不通过 {len(still_broken)} 个）")
            if diff.strip():
                # 这条提示是为一次真实踩坑加的：补丁应用与复评在同一进程里，
                # 被测模块仍是旧代码，于是稳定地报「没效果」——看着像修复无效，
                # 实际是评错了对象。不写出来，排查会走上完全错误的岔路。
                reason += ("。若改动已落盘而本进程在应用补丁前就已加载被测模块，"
                           "评的仍是旧代码：复评须在重启或新进程后执行"
                           "（见 TraceProducer 的契约）")

        return RecheckResult(
            verdict, reason, audit=audit, case_count=len(cases),
            fixed=tuple(fixed), regressed=tuple(regressed),
            still_broken=tuple(still_broken), errored=tuple(errored),
            before_defects=before_n, after_defects=len(after_batch.defects),
            new_kinds=new_kinds)


def _count_defects(reports: Sequence[PlatformReport]) -> int:
    return len(defects_from_reports(list(reports)).defects) if reports else 0
