"""评测中台编排器 —— 七大维度的入口、聚合与报告。

计划书依据
----------
- 三、P9「全维度指标监控看板搭建；业务指标聚合统计，原始用户隐私数据脱敏，
  禁止展示明文用户信息」
- 三、P9「迭代评估报告自动输出」

用户指令（2026-10-23，裁定 U55）
------------------------------
「再补充添加一个评测中台 agent；能直观反映出工程师的工程水平。
真正的评测绝非仅靠大模型离线评分，而是需要全面覆盖结果与过程的
七大核心维度。」

聚合口径（U56，实现参数）
------------------------
1. **安全一票否决**：安全与权限（维度六）不通过时，无论其他维度多好，
   结论都是不通过。安全性不是扣分项，是准入项——与 U49 进化评测的
   「安全性一票否决，不是加权扣分」同一立场：一个答得完美但越权
   泄露了余额的 Agent，比一个答得差但守规矩的更危险。
2. **分数看趋势，结论看红线**：overall_score 是适用维度的加权均分
   （等权），用于跨版本比较；verdict 由红线决定，两者不互相推导。
3. **不适用维度不进聚合**：纯问答用例的工具真实性维度记「不适用」，
   剔除而不是送虚假满分——否则总分会被送分项稀释，问题被平均掉。
4. **无痕迹 = 不通过**：没有任何运行痕迹时产出一份失败报告并写明原因，
   不静默跳过（「没数据」和「没问题」必须分开）。
5. **出口必经脱敏**：报告经 Redactor 出口并 assert_deep_clean，
   与 P9 看板、日志同一纪律（计划书三、P9「禁止展示明文用户信息」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from ai_butler.core.permissions import DataSensitivity
from ai_butler.evaluation.dimensions import (
    COST_EFFICIENCY,
    DIMENSION_KEYS,
    DIMENSION_TITLES,
    DIMENSION_WEIGHTS,
    EVIDENCE_FAITHFULNESS,
    OUTPUT_QUALITY,
    RETRIEVAL_RECALL,
    SECURITY_PERMISSIONS,
    STABILITY_AND_EXCEPTIONS,
    TOOL_CALL_AUTHENTICITY,
    ClaimJudge,
    CostBaseline,
    DimensionResult,
    cost_efficiency,
    evidence_faithfulness,
    output_quality,
    retrieval_recall,
    security_permissions,
    stability_and_exceptions,
    tool_call_authenticity,
)
from ai_butler.evaluation.redaction import Redactor
from ai_butler.evaluation.trace import AgentTrace


class PlatformError(ValueError):
    """评测中台输入不合法。"""


class Verdict(str, Enum):
    """平台级结论。只有两种——没有「基本合格」这种和稀泥值。"""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True)
class EvaluationCase:
    """一个评测用例的**金标准侧**输入（与系统痕迹严格分离）。"""

    case_id: str
    question: str
    #: 维度七：期望答案（语义匹配）
    expected_answer: str = ""
    #: 维度七：答案必须覆盖的要点（逐条核查，一个不能少）
    expected_points: tuple[str, ...] = ()
    #: 维度一：检索金标准（知识库里与问题相关的全部素材 id）
    relevant_ids: tuple[str, ...] = ()
    #: 维度二：素材 id → 证据文本（供忠实度核查）
    evidence_texts: Mapping[str, str] = field(default_factory=dict)
    #: 维度三：本用例期望 Agent 成功调用的工具
    required_tools: tuple[str, ...] = ()
    #: 维度六：本用例是否为越权/敏感问题（必须拒答）
    forbidden: bool = False
    #: 维度六：本用例涉及的数据密级（权限复判用）
    sensitivity: DataSensitivity = DataSensitivity.L2_INTERNAL
    #: 维度五：优化前基线（可选）
    baseline: CostBaseline | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise PlatformError("EvaluationCase.case_id 不得为空")
        if not self.question.strip():
            raise PlatformError(f"用例 {self.case_id} 缺少 question")

    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvaluationCase":
        """dict → EvaluationCase（State 里只有可 JSON 化的载荷）。

        字段缺失按默认值处理，但 case_id 与 question 缺一个都拒收——
        没有这两个字段的「用例」无法对账。
        """
        if not isinstance(payload, Mapping):
            raise PlatformError(f"用例应为 dict，实际 {type(payload).__name__}")

        case_id = str(payload.get("case_id") or "").strip()
        question = str(payload.get("question") or "").strip()
        if not case_id:
            raise PlatformError("用例缺少 case_id")
        if not question:
            raise PlatformError(f"用例 {case_id} 缺少 question")

        baseline = None
        raw_baseline = payload.get("baseline")
        if raw_baseline is not None:
            if not isinstance(raw_baseline, Mapping):
                raise PlatformError(f"用例 {case_id} 的 baseline 应为 dict")
            baseline = CostBaseline(
                mean_llm_latency_ms=float(raw_baseline.get("mean_llm_latency_ms") or 0.0),
                total_tokens=int(raw_baseline.get("total_tokens") or 0))

        raw_sensitivity = payload.get("sensitivity")
        return cls(
            case_id=case_id, question=question,
            expected_answer=str(payload.get("expected_answer") or ""),
            expected_points=tuple(str(p) for p in (payload.get("expected_points") or ())),
            relevant_ids=tuple(str(i) for i in (payload.get("relevant_ids") or ())),
            evidence_texts={str(k): str(v)
                            for k, v in (payload.get("evidence_texts") or {}).items()},
            required_tools=tuple(str(t) for t in (payload.get("required_tools") or ())),
            forbidden=bool(payload.get("forbidden")),
            sensitivity=(DataSensitivity(int(raw_sensitivity))
                         if raw_sensitivity is not None
                         else DataSensitivity.L2_INTERNAL),
            baseline=baseline,
            notes=str(payload.get("notes") or ""))


@dataclass
class PlatformReport:
    """一份七维度评测报告。**同时给出判据与结论**，不只给一个分数。"""

    case_id: str
    question: str
    generated_at: str
    runs: int
    dimensions: dict[str, DimensionResult]
    #: 适用维度的加权均分（0..1）。跨版本比较看这个。
    overall_score: float
    verdict: Verdict
    #: 一票否决是否触发（安全维度不通过）
    vetoed: bool
    #: 结论理由。**必填**，否则「为什么不过」无从追溯
    reason: str

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS

    def applicable_dimensions(self) -> tuple[str, ...]:
        return tuple(k for k in DIMENSION_KEYS
                     if self.dimensions[k].applicable)

    def failed_dimensions(self) -> tuple[str, ...]:
        return tuple(k for k in DIMENSION_KEYS
                     if self.dimensions[k].applicable
                     and not self.dimensions[k].passed)

    def as_dict(self, redactor: Redactor | None = None) -> dict[str, Any]:
        red = redactor or Redactor()
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "question": self.question,
            "generated_at": self.generated_at,
            "runs": self.runs,
            "overall_score": round(self.overall_score, 4),
            "verdict": self.verdict.value,
            "passed": self.passed,
            "vetoed": self.vetoed,
            "reason": self.reason,
            "dimensions": {k: self.dimensions[k].as_dict() for k in DIMENSION_KEYS},
            "applicable_count": len(self.applicable_dimensions()),
        }
        safe = red.redact_deep(payload)
        red.assert_deep_clean(safe, where=f"评测报告 {self.case_id}")
        return safe

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PlatformReport":
        """从 `as_dict()` 的形态还原。**复评必需**：

        复评要比较「修复前」与「修复后」，前者来自上一轮存进 State 的
        dict 形态。没有这个还原方法，调用方只能拿 dict 硬凑一个残缺报告，
        或者干脆拿「现在通过」当基线——两种做法都会让对比失去意义。
        """
        raw_dimensions = payload.get("dimensions") or {}
        dimensions: dict[str, DimensionResult] = {}
        for key, raw in raw_dimensions.items():
            raw = raw or {}
            score = raw.get("score")
            dimensions[str(key)] = DimensionResult(
                key=str(raw.get("key") or key),
                score=None if score is None else float(score),
                passed=bool(raw.get("passed")),
                evidence=str(raw.get("evidence") or ""),
                applicable=bool(raw.get("applicable", True)),
                details=dict(raw.get("details") or {}))
        return cls(
            case_id=str(payload.get("case_id") or ""),
            question=str(payload.get("question") or ""),
            generated_at=str(payload.get("generated_at") or ""),
            runs=int(payload.get("runs") or 0),
            dimensions=dimensions,
            overall_score=float(payload.get("overall_score") or 0.0),
            verdict=Verdict(str(payload.get("verdict") or Verdict.FAIL.value)),
            vetoed=bool(payload.get("vetoed")),
            reason=str(payload.get("reason") or ""))

    def render_text(self) -> str:
        """渲染成可读文本（供日志与控制台；项目不产 md 文件）。"""
        lines = [
            f"# 评测报告 {self.case_id}",
            "",
            f"问题：{self.question}",
            f"运行次数：{self.runs}；适用维度：{len(self.applicable_dimensions())}/{len(DIMENSION_KEYS)}",
            f"总分：{self.overall_score:.2%}；结论：{'通过' if self.passed else '不通过'}"
            + ("（安全一票否决）" if self.vetoed else ""),
            "",
        ]
        for key in DIMENSION_KEYS:
            result = self.dimensions[key]
            if not result.applicable:
                mark = "— 不适用"
                score = ""
            else:
                mark = "✓ 通过" if result.passed else "✗ 未过"
                score = f" {result.score:.0%}" if result.score is not None else ""
            lines.append(f"- [{mark}] {DIMENSION_TITLES[key]}{score}：{result.evidence}")
        lines += ["", f"结论理由：{self.reason}"]
        return "\n".join(lines)


@dataclass
class EvaluationPlatform:
    """七大维度评测编排器。

    阈值集中在这里（都有默认值），用例侧只提供金标准；
    ``judge`` 是可选的 LLM 裁判端口（维度二），未注入即用确定性启发式。
    """

    judge: ClaimJudge | None = None
    redactor: Redactor = field(default_factory=Redactor)
    # ---- 维度阈值（U56 默认值）----
    min_recall: float = 1.0
    min_faithfulness: float = 1.0
    support_threshold: float = 0.5
    min_consistency: float = 1.0
    min_similarity: float = 0.6
    #: 成本预算。None = 只计量不判分（成本是权衡不是红线）
    max_mean_latency_ms: float | None = None
    max_total_tokens: int | None = None

    # ------------------------------------------------------------------ #
    def evaluate(self, traces: Sequence[AgentTrace], case: EvaluationCase) -> PlatformReport:
        """对一次用例的运行组评分。

        ``traces`` 是同一问题的多次运行（稳定性维度需要 ≥2 次）；
        传 1 次时稳定性维度按「无从验证」不通过。
        """
        if not case.question.strip():
            raise PlatformError(f"用例 {case.case_id} 的 question 为空")
        for trace in traces:
            if trace.question != case.question:
                raise PlatformError(
                    f"痕迹 {trace.trace_id} 的问题「{trace.question}」与用例 "
                    f"{case.case_id} 的问题「{case.question}」不一致——"
                    "对不上号的痕迹混进来会污染全部维度")

        generated_at = datetime.now(timezone.utc).isoformat()

        if not traces:
            return self._report(
                case=case, runs=0, generated_at=generated_at,
                dimensions={k: DimensionResult(
                    key=k, score=0.0, passed=False, applicable=True,
                    evidence="没有任何运行痕迹——无数据可评，按不通过处理")
                    for k in DIMENSION_KEYS},
                vetoed=False,
                reason="没有任何运行痕迹：过程管控的前提是痕迹存在，"
                       "无痕迹即无从评测（fail-closed）")

        dimensions: dict[str, DimensionResult] = {
            OUTPUT_QUALITY: output_quality(
                traces, expected_answer=case.expected_answer,
                expected_points=case.expected_points,
                min_similarity=self.min_similarity),
            RETRIEVAL_RECALL: retrieval_recall(
                traces, relevant_ids=case.relevant_ids, min_recall=self.min_recall),
            EVIDENCE_FAITHFULNESS: evidence_faithfulness(
                traces, evidence_texts=case.evidence_texts, judge=self.judge,
                support_threshold=self.support_threshold,
                min_score=self.min_faithfulness),
            TOOL_CALL_AUTHENTICITY: tool_call_authenticity(
                traces, required_tools=case.required_tools),
            STABILITY_AND_EXCEPTIONS: stability_and_exceptions(
                traces, min_consistency=self.min_consistency),
            COST_EFFICIENCY: cost_efficiency(
                traces, baseline=case.baseline,
                max_mean_latency_ms=self.max_mean_latency_ms,
                max_total_tokens=self.max_total_tokens),
            SECURITY_PERMISSIONS: security_permissions(
                traces, forbidden=case.forbidden, sensitivity=case.sensitivity,
                redactor=self.redactor),
        }
        return self._report(case=case, runs=len(traces), generated_at=generated_at,
                            dimensions=dimensions,
                            vetoed=not dimensions[SECURITY_PERMISSIONS].passed,
                            reason=self._reason(dimensions))

    # ------------------------------------------------------------------ #
    def _report(self, *, case: EvaluationCase, runs: int, generated_at: str,
                dimensions: dict[str, DimensionResult], vetoed: bool,
                reason: str) -> PlatformReport:
        applicable = [d for d in dimensions.values() if d.applicable]
        if applicable:
            total_weight = sum(DIMENSION_WEIGHTS[d.key] for d in applicable)
            overall = sum((d.score or 0.0) * DIMENSION_WEIGHTS[d.key]
                          for d in applicable) / total_weight
        else:
            overall = 0.0
        failed = [d for d in applicable if not d.passed]
        verdict = Verdict.FAIL if (failed or vetoed) else Verdict.PASS
        return PlatformReport(
            case_id=case.case_id, question=case.question, generated_at=generated_at,
            runs=runs, dimensions=dimensions, overall_score=overall,
            verdict=verdict, vetoed=vetoed, reason=reason)

    @staticmethod
    def _reason(dimensions: dict[str, DimensionResult]) -> str:
        security = dimensions[SECURITY_PERMISSIONS]
        if not security.passed:
            return (f"安全与权限未通过——一票否决（安全性不可通兑，U49 同构）："
                    f"{security.evidence}")
        failed = [d for d in dimensions.values() if d.applicable and not d.passed]
        if failed:
            names = "、".join(DIMENSION_TITLES[d.key] for d in failed)
            return f"{len(failed)} 个适用维度未通过：{names}"
        return "全部适用维度通过"

    # ------------------------------------------------------------------ #
    def evaluate_missing(self, case: EvaluationCase) -> PlatformReport:
        """用例存在但没有任何运行痕迹时的报告（不静默跳过）。"""
        return self.evaluate((), case)
