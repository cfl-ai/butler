"""P0-2 自动化评测脚本。

计划书依据
----------
- 三、P0-2「开发自动化评测脚本：幻觉率、准确率、Token 消耗、循环次数、重规划成功率；
  新增评测指标：Jev 决策命中率、知识库声明置信度、证据完备度」
- 三、P0-2「对接 Harness 自进化体系，评测数据自动流入迭代闭环」
- 三、P9「全维度指标监控看板；新增监控指标：Jev 意图命中率、多意图识别准确率；
  Agent-Wiki 指标：声明平均置信度、证据缺失占比、矛盾数量、过期素材占比」

指标口径中计划书未定义的部分
--------------------------
「准确率」计划书未给出判定口径 → 由调用方传入 is_correct 判定函数，
默认实现为「answer 与 expected 完全相等」。该默认属实现参数，不写死语义。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Sequence


@dataclass
class RunRecord:
    """单次任务运行记录。字段按计划书点名的 8 项指标设计。"""

    task_id: str
    user_id: str

    # 准确率（口径由 is_correct 决定）
    answer: str = ""
    expected: str | None = None

    # 幻觉率：无证据支撑的命题占比
    claims_total: int = 0
    claims_unsupported: int = 0

    # Token 消耗
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # 循环次数（计划书三、P3「全局计数器、熔断轮次」）
    loop_count: int = 0

    # 重规划成功率
    replan_attempted: bool = False
    replan_succeeded: bool = False

    # Jev 决策命中率
    jev_source: str | None = None            # "jev" | "fallback"
    jev_label: str | None = None
    jev_expected_label: str | None = None
    jev_confidence: float | None = None

    # 知识库声明置信度 / 证据完备度
    claim_confidences: list[float] = field(default_factory=list)
    evidence_bound: int = 0
    evidence_required: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalReport:
    total_runs: int
    hallucination_rate: float
    accuracy: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_tokens_per_run: float
    total_loops: int
    avg_loops_per_run: float
    max_loops: int
    replan_success_rate: float
    jev_hit_rate: float
    avg_claim_confidence: float
    evidence_completeness: float
    counted: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_text(self) -> str:
        """纯文本评估报告（计划书 P9「迭代评估报告自动输出」）。

        刻意不输出 Markdown：本项目约定只有计划书点名的文档才落 md。
        """
        lines = [
            "===== 自动化评测报告 =====",
            f"样本数：{self.total_runs}",
            f"1. 幻觉率            ：{self.hallucination_rate:.4f}"
            f"（{self.counted.get('unsupported_claims', 0)}/{self.counted.get('claims', 0)} 无证据命题）",
            f"2. 准确率            ：{self.accuracy:.4f}"
            f"（{self.counted.get('correct', 0)}/{self.counted.get('judged', 0)} 判定样本）",
            f"3. Token 消耗        ：{self.total_tokens}"
            f"（prompt {self.prompt_tokens} + completion {self.completion_tokens}，"
            f"均 {self.avg_tokens_per_run:.1f}/次）",
            f"4. 循环次数          ：{self.total_loops}"
            f"（均 {self.avg_loops_per_run:.2f}，峰 {self.max_loops}）",
            f"5. 重规划成功率      ：{self.replan_success_rate:.4f}"
            f"（{self.counted.get('replan_ok', 0)}/{self.counted.get('replan_tried', 0)} 次尝试）",
            f"6. Jev 决策命中率    ：{self.jev_hit_rate:.4f}"
            f"（{self.counted.get('jev_hit', 0)}/{self.counted.get('jev_judged', 0)} 次判定）",
            f"7. 知识库声明置信度  ：{self.avg_claim_confidence:.4f}"
            f"（{self.counted.get('claims_scored', 0)} 条声明）",
            f"8. 证据完备度        ：{self.evidence_completeness:.4f}"
            f"（{self.counted.get('evidence_bound', 0)}/{self.counted.get('evidence_required', 0)} 条证据）",
            "=========================",
        ]
        return "\n".join(lines)


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def default_is_correct(record: RunRecord) -> bool | None:
    """默认判定：answer 与 expected 完全相等。expected 为空则不参与统计。"""
    if record.expected is None:
        return None
    return record.answer == record.expected


class Evaluator:
    def __init__(self, *, is_correct: Callable[[RunRecord], bool | None] | None = None) -> None:
        self.is_correct = is_correct or default_is_correct

    def evaluate(self, records: Sequence[RunRecord]) -> EvalReport:
        claims = sum(r.claims_total for r in records)
        unsupported = sum(r.claims_unsupported for r in records)
        prompt = sum(r.prompt_tokens for r in records)
        completion = sum(r.completion_tokens for r in records)
        loops = sum(r.loop_count for r in records)

        judged = correct = 0
        for r in records:
            verdict = self.is_correct(r)
            if verdict is None:
                continue
            judged += 1
            correct += 1 if verdict else 0

        tried = sum(1 for r in records if r.replan_attempted)
        replan_ok = sum(1 for r in records if r.replan_attempted and r.replan_succeeded)

        jev_judged = jev_hit = 0
        for r in records:
            if r.jev_expected_label is None or r.jev_source != "jev":
                continue
            jev_judged += 1
            jev_hit += 1 if r.jev_label == r.jev_expected_label else 0

        confidences = [c for r in records for c in r.claim_confidences]
        evidence_bound = sum(r.evidence_bound for r in records)
        evidence_required = sum(r.evidence_required for r in records)

        return EvalReport(
            total_runs=len(records),
            hallucination_rate=_ratio(unsupported, claims),
            accuracy=_ratio(correct, judged),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            avg_tokens_per_run=_ratio(prompt + completion, len(records)),
            total_loops=loops,
            avg_loops_per_run=_ratio(loops, len(records)),
            max_loops=max((r.loop_count for r in records), default=0),
            replan_success_rate=_ratio(replan_ok, tried),
            jev_hit_rate=_ratio(jev_hit, jev_judged),
            avg_claim_confidence=_ratio(sum(confidences), len(confidences)),
            evidence_completeness=_ratio(evidence_bound, evidence_required),
            counted={
                "claims": claims, "unsupported_claims": unsupported,
                "judged": judged, "correct": correct,
                "replan_tried": tried, "replan_ok": replan_ok,
                "jev_judged": jev_judged, "jev_hit": jev_hit,
                "claims_scored": len(confidences),
                "evidence_bound": evidence_bound, "evidence_required": evidence_required,
            },
        )

    # ---------------- 对接 Harness 自进化体系 ----------------
    def export_for_harness(self, records: Sequence[RunRecord], *, path) -> str:
        """导出评测数据供离线自进化链路消费。

        计划书三、P0-2「对接 Harness 自进化体系，评测数据自动流入迭代闭环」；
        计划书三、P10「评测集合不含用户敏感原始数据」——因此导出时
        **user_id 做不可逆摘要**，只保留可关联性，不保留身份。
        """
        import hashlib
        from pathlib import Path

        payload = []
        for r in records:
            item = r.as_dict()
            item["user_ref"] = hashlib.sha256(r.user_id.encode("utf-8")).hexdigest()[:16]
            item.pop("user_id", None)          # 离线链路不携带用户身份
            payload.append(item)

        target = Path(path)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target)


def load_records(path) -> list[RunRecord]:
    from pathlib import Path

    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    known = set(RunRecord.__dataclass_fields__)
    return [RunRecord(**{k: v for k, v in item.items() if k in known}) for item in raw]


def aggregate(records: Iterable[RunRecord]) -> EvalReport:
    return Evaluator().evaluate(list(records))
