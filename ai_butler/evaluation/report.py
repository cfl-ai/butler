"""P9 迭代评估报告自动输出。

计划书依据
----------
三、P9「迭代评估报告自动输出，**包含 Jev、知识库质量报告**」
三、P9 触发时机「系统后台**常态化持续运行**」

报告是**返回值**，不落盘成项目文档——项目内 md 只允许计划书点名的那几类，
评估报告由调用方按需落库（P1 的 review/audit scope）或推送给看板。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ai_butler.evaluation.redaction import Redactor

REPORT_SECTIONS: tuple[str, ...] = ("jev", "wiki", "cases", "dataset", "finetune")


def build_report(*, board: Mapping[str, Any], deposit: Mapping[str, Any],
                 dataset: Mapping[str, Any], pipeline: Mapping[str, Any],
                 runs: Sequence[Mapping[str, Any]], generated_at: str,
                 redactor: Redactor | None = None) -> dict[str, Any]:
    """组装迭代评估报告。Jev 与知识库质量各成一段（计划书明确要求「包含」）。"""
    red = redactor or Redactor()
    metrics = dict(board.get("metrics") or {})

    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "sections": list(REPORT_SECTIONS),
        # ---- Jev 质量报告 ----
        "jev": {
            "intent_hit_rate": metrics.get("jev_intent_hit_rate"),
            "multi_intent_accuracy": metrics.get("multi_intent_accuracy"),
            "error_cases": int((deposit.get("by_category") or {}).get("jev_error", 0)),
        },
        # ---- 知识库质量报告 ----
        "wiki": {
            "claim_avg_confidence": metrics.get("wiki_claim_avg_confidence"),
            "evidence_missing_ratio": metrics.get("wiki_evidence_missing_ratio"),
            "contradiction_count": metrics.get("wiki_contradiction_count"),
            "stale_material_ratio": metrics.get("wiki_stale_material_ratio"),
            "conflict_cases": int((deposit.get("by_category") or {}).get("wiki_conflict", 0)),
            "scope": board.get("scope", ""),
        },
        "cases": {"total": deposit.get("total", 0),
                  "good": deposit.get("good", 0),
                  "bad": deposit.get("bad", 0),
                  "support_ratio": deposit.get("support_ratio", 0.0)},
        "dataset": {"name": dataset.get("name", ""), "count": dataset.get("count", 0),
                    "sources": list(dataset.get("sources") or []),
                    "carries_user_id": bool(dataset.get("carries_user_id"))},
        "finetune": {"runs": [dict(r) for r in runs],
                     "training_in_ci": pipeline.get("training_in_ci", False)},
    }
    safe = red.redact_deep(payload)
    red.assert_deep_clean(safe, where="迭代评估报告")
    return safe


def render_markdown(report: Mapping[str, Any]) -> str:
    """把报告渲染成可读文本（中文标点、无 emoji）。"""
    jev = report.get("jev") or {}
    wiki = report.get("wiki") or {}
    cases = report.get("cases") or {}
    dataset = report.get("dataset") or {}

    def _pct(node: Any) -> str:
        if not isinstance(node, Mapping):
            return "无数据"
        value = node.get("value")
        return f"{float(value):.2%}" if isinstance(value, (int, float)) else "无数据"

    lines = [
        "# 迭代评估报告",
        "",
        f"生成时间：{report.get('generated_at', '')}",
        "",
        "## 一、Jev 质量",
        f"- 意图命中率：{_pct(jev.get('intent_hit_rate'))}",
        f"- 多意图识别准确率：{_pct(jev.get('multi_intent_accuracy'))}",
        f"- Jev 错误案例：{jev.get('error_cases', 0)} 条",
        "",
        "## 二、知识库质量",
        f"- 统计范围：{wiki.get('scope') or '未标注'}",
        f"- 声明平均置信度：{_pct(wiki.get('claim_avg_confidence'))}",
        f"- 证据缺失占比：{_pct(wiki.get('evidence_missing_ratio'))}",
        f"- 矛盾数量：{(wiki.get('contradiction_count') or {}).get('value', '无数据')}",
        f"- 过期素材占比：{_pct(wiki.get('stale_material_ratio'))}",
        f"- Wiki 冲突案例：{wiki.get('conflict_cases', 0)} 条",
        "",
        "## 三、日志沉淀",
        f"- 案例总数：{cases.get('total', 0)}（优质 {cases.get('good', 0)} / "
        f"错误 {cases.get('bad', 0)}）",
        f"- 优质占比：{float(cases.get('support_ratio') or 0.0):.2%}",
        "",
        "## 四、SFT 数据集",
        f"- 数据集：{dataset.get('name', '')}，样本 {dataset.get('count', 0)} 条",
        f"- 来源：{'、'.join(dataset.get('sources') or []) or '无'}",
        f"- 是否携带用户身份：{'是（异常）' if dataset.get('carries_user_id') else '否'}",
        "",
        "## 五、增量微调",
    ]
    for run in report.get("finetune", {}).get("runs", []):
        mark = "执行" if run.get("should_run") else "跳过"
        lines.append(f"- [{mark}] {run.get('target')}：{run.get('reason')}")
    if not report.get("finetune", {}).get("runs"):
        lines.append("- 本轮无微调计划")
    return "\n".join(lines)
