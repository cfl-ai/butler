"""P0-2 自动化评测测试。

计划书依据：三、P0-2「开发自动化评测脚本：幻觉率、准确率、Token 消耗、循环次数、
重规划成功率；新增评测指标：Jev 决策命中率、知识库声明置信度、证据完备度」
"""

from __future__ import annotations

import json

from ai_butler.devkit.evaluation import Evaluator, RunRecord, aggregate, load_records


def _rec(**kw) -> RunRecord:
    base = {"task_id": "t", "user_id": "u-1"}
    base.update(kw)
    return RunRecord(**base)


def test_all_eight_plan_metrics_are_computed():
    records = [
        _rec(task_id="t1", answer="A", expected="A", claims_total=4, claims_unsupported=1,
             prompt_tokens=100, completion_tokens=50, loop_count=3,
             replan_attempted=True, replan_succeeded=True,
             jev_source="jev", jev_label="旅游", jev_expected_label="旅游",
             claim_confidences=[0.9, 0.8], evidence_bound=3, evidence_required=4),
        _rec(task_id="t2", answer="B", expected="A", claims_total=6, claims_unsupported=2,
             prompt_tokens=200, completion_tokens=100, loop_count=5,
             replan_attempted=True, replan_succeeded=False,
             jev_source="jev", jev_label="点餐", jev_expected_label="旅游",
             claim_confidences=[0.6], evidence_bound=1, evidence_required=6),
    ]
    r = Evaluator().evaluate(records)

    assert r.total_runs == 2
    assert r.hallucination_rate == 3 / 10              # 1. 幻觉率
    assert r.accuracy == 1 / 2                         # 2. 准确率
    assert (r.prompt_tokens, r.completion_tokens, r.total_tokens) == (300, 150, 450)  # 3. Token
    assert (r.total_loops, r.max_loops) == (8, 5)      # 4. 循环次数
    assert r.replan_success_rate == 1 / 2              # 5. 重规划成功率
    assert r.jev_hit_rate == 1 / 2                     # 6. Jev 决策命中率
    assert round(r.avg_claim_confidence, 6) == round((0.9 + 0.8 + 0.6) / 3, 6)  # 7. 声明置信度
    assert r.evidence_completeness == 4 / 10           # 8. 证据完备度


def test_empty_input_yields_zeros_not_division_errors():
    r = aggregate([])
    assert r.total_runs == 0
    for name in ("hallucination_rate", "accuracy", "replan_success_rate",
                 "jev_hit_rate", "avg_claim_confidence", "evidence_completeness"):
        assert getattr(r, name) == 0.0, name


def test_jev_hit_rate_only_counts_records_where_jev_actually_decided():
    """降级（fallback）的样本不参与命中率，否则会稀释指标。"""
    records = [
        _rec(jev_source="jev", jev_label="旅游", jev_expected_label="旅游"),
        _rec(jev_source="fallback", jev_label="旅游", jev_expected_label="旅游"),
        _rec(jev_source="jev", jev_label="点餐", jev_expected_label=None),
    ]
    r = Evaluator().evaluate(records)
    assert r.counted["jev_judged"] == 1 and r.jev_hit_rate == 1.0


def test_accuracy_definition_is_pluggable():
    """计划书未定义准确率口径 → 默认完全相等，可由调用方覆盖。"""
    records = [_rec(answer="杭州三日游", expected="杭州 3 日游")]
    assert Evaluator().evaluate(records).accuracy == 0.0

    loose = Evaluator(is_correct=lambda r: r.answer.replace("三", "3").replace(" ", "")
                      == r.expected.replace(" ", ""))
    assert loose.evaluate(records).accuracy == 1.0


def test_report_text_covers_all_eight_metrics():
    text = aggregate([_rec(answer="A", expected="A", claims_total=1)]).to_text()
    for label in ("幻觉率", "准确率", "Token 消耗", "循环次数",
                  "重规划成功率", "Jev 决策命中率", "知识库声明置信度", "证据完备度"):
        assert label in text, label


def test_harness_export_strips_user_identity(workdir):
    """计划书三、P10：评测集合不含用户敏感原始数据。"""
    records = [_rec(task_id="t1", user_id="13800001111", answer="x")]
    out = Evaluator().export_for_harness(records, path=workdir / "export.json")
    payload = json.loads(open(out, encoding="utf-8").read())

    assert "user_id" not in payload[0], "离线链路不得携带用户身份"
    assert "user_ref" in payload[0]
    assert "13800001111" not in json.dumps(payload, ensure_ascii=False)


def test_records_roundtrip_through_disk(workdir):
    records = [_rec(task_id="t1", answer="A", expected="A", loop_count=2)]
    path = workdir / "records.json"
    path.write_text(json.dumps([r.as_dict() for r in records], ensure_ascii=False),
                    encoding="utf-8")
    loaded = load_records(path)
    assert loaded[0].task_id == "t1" and loaded[0].loop_count == 2
    assert aggregate(loaded).accuracy == 1.0
