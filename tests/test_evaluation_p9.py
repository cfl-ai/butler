"""P9 指标评估与增量微调体系测试。

计划书依据：三、P9 六项开发内容 + 触发时机
裁定：U28（LLaMA-Factory + LoRA/QLoRA）、U29（Alpaca 格式）、
      U30（Presidio + 正则兜底）、U31（指标口径自持）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, SOURCE, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.agents.base import resolve
from ai_butler.agents.capabilities import CAP_DATA_READ, CAP_WIKI_READ, grant
from ai_butler.agents.p9_evaluation import SCOPE_PUBLIC, EvaluationAgent
from ai_butler.agents.state import new_state
from ai_butler.core import decisions
from ai_butler.evaluation.cases import (
    CASE_BAD,
    CASE_CATEGORIES,
    CASE_GOOD,
    CASE_KINDS,
    CATEGORY_GENERAL,
    CATEGORY_JEV_ERROR,
    CATEGORY_WIKI_CONFLICT,
    CaseError,
    CaseRecord,
    deposit_from_logs,
)
from ai_butler.evaluation.dataset import (
    ALLOWED_SOURCES,
    FORBIDDEN_SOURCES,
    DatasetError,
    SftDataset,
    SftSample,
    assert_offline_source,
    dataset_info_entry,
)
from ai_butler.evaluation.finetune import (
    METHOD_LORA,
    METHOD_QLORA,
    METHODS,
    TARGET_JEV_TEMPLATE,
    TARGET_VERTICAL_MODEL,
    TARGETS,
    FinetuneConfig,
    FinetunePipeline,
    PipelineError,
)
from ai_butler.evaluation.metrics import (
    JEV_INTENT_HIT_RATE,
    METRIC_KEYS,
    MULTI_INTENT_ACCURACY,
    WIKI_CLAIM_AVG_CONFIDENCE,
    WIKI_CONTRADICTION_COUNT,
    WIKI_EVIDENCE_MISSING_RATIO,
    WIKI_STALE_MATERIAL_RATIO,
    JeVObservation,
    build_board,
    claim_evidence_ids,
    jev_intent_hit_rate,
    multi_intent_accuracy,
    wiki_claim_avg_confidence,
    wiki_contradiction_count,
    wiki_evidence_missing_ratio,
    wiki_stale_material_ratio,
)
from ai_butler.evaluation.redaction import (
    PLACEHOLDER,
    RedactionError,
    Redactor,
    contains_plaintext_identity,
)
from ai_butler.evaluation.report import REPORT_SECTIONS, build_report, render_markdown

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def _call(agent, state):
    return asyncio.run(resolve(agent(state)))


# ============================== 台账 ============================== #
def test_p9_decisions_are_registered_and_resolved():
    """P9 的开发内容在原文里没有定义项，必须先登记台账再动手。

    U50（歧义样本的学习回流口径）同样挂在 P9 阶段下 ——
    2026-10-19 新增该条时本断言没有同步，守护红了两周没人发现，
    是「写了没接上」的又一形态（E-65）：**新增台账条目必须同步这里的集合**。
    """
    keys = {d.key for d in decisions.DECISIONS if d.phase == "P9"}
    # U76 = 评测缺陷单与处置路由（2026-10-25 补，见 evaluation/defects.py）
    assert keys == {"U28", "U29", "U30", "U31", "U50", "U55", "U56", "U76"}
    for key in keys:
        assert decisions.get(key).status == decisions.RESOLVED
    assert not decisions.pending(), "台账不应有未裁定项"


# ============================== 脱敏（U30 / 三、P9）============================== #
def test_redactor_masks_all_required_kinds():
    redactor = Redactor()
    raw = "用户 user-A 手机 13812345678 邮箱 alice@example.com 身份证 11010119900307123X"
    out = redactor.redact(raw)
    assert "user-A" not in out and "13812345678" not in out
    assert "alice@example.com" not in out and "11010119900307123X" not in out
    assert PLACEHOLDER.format(kind="手机号") in out
    assert redactor.residual_kinds(out) == ()


def test_redactor_is_fail_closed():
    """无法证明已脱敏时必须报错，而不是放行。"""
    redactor = Redactor()
    with pytest.raises(RedactionError) as ei:
        redactor.assert_clean("还有 user-B 没脱")
    assert "禁止展示明文用户信息" in str(ei.value)


def test_safe_text_redacts_then_verifies():
    redactor = Redactor()
    assert "user-A" not in redactor.safe_text("来自 user-A 的偏好")


def test_redact_deep_walks_structures_and_detects_residue():
    redactor = Redactor()
    payload = {"a": ["user-A", {"b": "13812345678"}], "c": 1}
    out = redactor.redact_deep(payload)
    assert out["a"][0] == PLACEHOLDER.format(kind="账号标识")
    assert out["c"] == 1
    redactor.assert_deep_clean(out)

    with pytest.raises(RedactionError):
        redactor.assert_deep_clean({"a": [{"b": "user-Z"}]})


def test_model_layer_is_reported_honestly():
    """未真正接上模型层就如实返回 False，不冒充做了模型级脱敏。"""
    assert Redactor().model_layer_active is False
    assert Redactor(analyzer=None).model_layer_active is False
    # 注入一个分析器才算启用（端口注入，项目不内置 presidio 依赖）
    assert Redactor(analyzer=object()).model_layer_active is True


def test_contains_plaintext_identity_helper():
    assert contains_plaintext_identity("联系 13812345678") is True
    assert contains_plaintext_identity("普通文本") is False


# ============================== 指标（三、P9 + U31）============================== #
def test_metric_keys_are_a_frozen_contract():
    """口径一旦变化历史就不可比，故 key 是稳定契约。"""
    assert METRIC_KEYS == (JEV_INTENT_HIT_RATE, MULTI_INTENT_ACCURACY,
                           WIKI_CLAIM_AVG_CONFIDENCE, WIKI_EVIDENCE_MISSING_RATIO,
                           WIKI_CONTRADICTION_COUNT, WIKI_STALE_MATERIAL_RATIO)


def test_jev_intent_hit_rate():
    obs = [JeVObservation("travel", "travel"), JeVObservation("travel", "food")]
    value = jev_intent_hit_rate(obs)
    assert value.value == 0.5 and value.sample_size == 2


def test_jev_hit_rate_without_samples_is_zero_not_crash():
    assert jev_intent_hit_rate([]).value == 0.0


def test_multi_intent_accuracy_only_counts_multi_samples():
    """关键口径：把单意图样本混进来会把这个指标稀释得毫无意义。"""
    obs = [JeVObservation("a", "a", multi_intent=False),
           JeVObservation("b", "b", multi_intent=True, detected_multi=True),
           JeVObservation("c", "c", multi_intent=True, detected_multi=False)]
    value = multi_intent_accuracy(obs)
    assert value.sample_size == 2 and value.value == 0.5


def test_multi_intent_accuracy_without_multi_samples():
    obs = [JeVObservation("a", "a")]
    assert multi_intent_accuracy(obs).sample_size == 0


def test_wiki_metrics_over_pages(settings, workdir):
    """走真实存储往返：生产里页面都是从磁盘读出来的，构造方式必须一致。"""
    store = WikiStore(workdir / "wiki-metrics", settings=settings)
    store.write_page(new_page(CLAIM, "c1", confidence=0.8, claim_type="fact",
                              description="d", subject_id="a", object_id="b",
                              evidence_ids=["e1"]))
    store.write_page(new_page(CLAIM, "c2", confidence=0.4, claim_type="fact",
                              description="d", subject_id="a", object_id="b",
                              evidence_ids=[], contradicts=["c1"]))
    store.write_page(new_page(EVIDENCE, "e1", confidence=0.9, source_id="s",
                              source_text="t", text_unit_id="u"))
    pages = store.list_pages()
    assert len(pages) == 3

    assert wiki_claim_avg_confidence(pages).value == pytest.approx(0.6)
    assert wiki_evidence_missing_ratio(pages).value == pytest.approx(0.5)
    assert wiki_contradiction_count(pages).value == 1.0


def test_contradicts_prefers_top_level_then_falls_back():
    """顶层优先：P6 踩过「读 extra 导致矛盾永远检不出」的坑，顺序不能反。"""
    from ai_butler.evaluation.metrics import page_contradicts
    bare = new_page(CLAIM, "c9", confidence=0.8, claim_type="fact", description="d",
                    subject_id="a", object_id="b", evidence_ids=["e"])
    assert page_contradicts(bare) == ()
    bare.contradicts = ["c1"]
    assert page_contradicts(bare) == ("c1",)


def test_wiki_stale_ratio_uses_updated_at():
    # new_page 用 now= 同时设置 created_at / updated_at（传 updated_at 会落进 extra）
    fresh = new_page(EVIDENCE, "e1", confidence=0.9, source_id="s",
                     source_text="t", text_unit_id="u", now=NOW.isoformat())
    old = new_page(EVIDENCE, "e2", confidence=0.9, source_id="s",
                   source_text="t", text_unit_id="u",
                   now=(NOW - timedelta(days=400)).isoformat())
    value = wiki_stale_material_ratio([fresh, old], now=NOW, stale_days=180)
    assert value.value == pytest.approx(0.5)


def test_evidence_ids_are_read_from_extra_not_top_level():
    """证据 id 存在 extra 里；写成顶层属性会让「证据缺失占比」恒为 100%。"""
    page = new_page(CLAIM, "c1", confidence=0.8, claim_type="fact", description="d",
                    subject_id="a", object_id="b", evidence_ids=["e1"])
    assert not hasattr(page, "evidence_ids")
    assert claim_evidence_ids(page) == ("e1",)
    assert wiki_evidence_missing_ratio([page]).value == 0.0


def test_board_has_all_six_metrics_and_is_redacted():
    board = build_board(observations=[], pages=[],
                        now=NOW, generated_at=NOW.isoformat(), redactor=Redactor())
    payload = board.as_dict()
    assert set(payload["metrics"]) == set(METRIC_KEYS)
    assert payload["redaction"]["regex_layer"] is True
    assert payload["redaction"]["model_layer"] is False


# ============================== 案例分类（三、P9）============================== #
def test_jev_and_wiki_cases_have_independent_categories():
    """计划书点名「Jev 错误、Wiki 冲突案例独立分类」，不能被归进 general。"""
    assert CASE_KINDS == ("good", "bad")
    assert set(CASE_CATEGORIES) == {CATEGORY_JEV_ERROR, CATEGORY_WIKI_CONFLICT,
                                    CATEGORY_GENERAL}
    from ai_butler.evaluation.cases import classify
    assert classify(is_good=False, jev_mismatch=True) == (CASE_BAD, CATEGORY_JEV_ERROR)
    assert classify(is_good=False, wiki_conflict=True) == (CASE_BAD, CATEGORY_WIKI_CONFLICT)
    assert classify(is_good=False) == (CASE_BAD, CATEGORY_GENERAL)

    # Jev 优先于 Wiki（两者同时命中时）
    assert classify(is_good=False, jev_mismatch=True,
                    wiki_conflict=True)[1] == CATEGORY_JEV_ERROR


def test_case_record_validates_enums():
    with pytest.raises(CaseError):
        CaseRecord(case_id="x", kind="meh", category=CATEGORY_GENERAL, summary="")
    with pytest.raises(CaseError):
        CaseRecord(case_id="x", kind=CASE_GOOD, category="meh", summary="")


def test_deposit_redacts_on_the_way_out():
    pool = deposit_from_logs([
        {"case_id": "c1", "is_good": True, "summary": "用户 user-A 很满意"},
        {"case_id": "c2", "is_good": False, "jev_mismatch": True,
         "summary": "Jev 判错", "payload": {"phone": "13812345678"}},
    ])
    assert pool.good_count == 1 and pool.bad_count == 1
    assert len(pool.by_category(CATEGORY_JEV_ERROR)) == 1

    payload = pool.as_dict(Redactor())
    assert "user-A" not in str(payload)
    assert "13812345678" not in str(payload)


def test_support_ratio():
    pool = deposit_from_logs([{"is_good": True}, {"is_good": True}, {"is_good": False}])
    assert pool.support_ratio() == pytest.approx(2 / 3)


# ============================== SFT 数据集（三、P9 + U29）============================== #
def test_forbidden_source_is_rejected_not_filtered():
    """计划书「禁止把私有 Vault 原始隐私放入训练集」——是**拒绝**，不是过滤。"""
    assert FORBIDDEN_SOURCES == ("private_vault",)
    assert ALLOWED_SOURCES == ("public_workspace",)
    with pytest.raises(DatasetError) as ei:
        SftSample(instruction="i", output="o", source="private_vault")
    assert "禁止把私有 Vault 原始隐私放入训练集" in str(ei.value)


def test_unknown_source_is_rejected_by_whitelist():
    with pytest.raises(DatasetError) as ei:
        SftSample(instruction="i", output="o", source="some_other_place")
    assert "不在白名单" in str(ei.value)


def test_sample_must_not_carry_user_id():
    with pytest.raises(DatasetError) as ei:
        SftSample(instruction="i", output="o", user_id="user-A")
    assert "剥离用户身份隐私" in str(ei.value)


def test_sample_requires_instruction_and_output():
    with pytest.raises(DatasetError):
        SftSample(instruction="  ", output="o")
    with pytest.raises(DatasetError):
        SftSample(instruction="i", output="")


def test_alpaca_format_matches_llamafactory():
    sample = SftSample(instruction="算总价", input="车 3000，书 20", output="3020",
                       system="你是助理", history=(("你好", "你好呀"),))
    row = sample.to_alpaca(Redactor())
    assert row["instruction"] == "算总价" and row["input"] == "车 3000，书 20"
    assert row["output"] == "3020" and row["system"] == "你是助理"
    assert row["history"] == [["你好", "你好呀"]]


def test_optional_fields_omitted_when_empty():
    row = SftSample(instruction="i", output="o").to_alpaca(Redactor())
    assert "system" not in row and "history" not in row


def test_dataset_info_entry_matches_u29_columns():
    entry = dataset_info_entry(name="jev_sft", file_name="jev_sft.json")
    assert entry["jev_sft"]["columns"] == {"prompt": "instruction", "query": "input",
                                          "response": "output", "system": "system",
                                          "history": "history"}


def test_dataset_redacts_rows_and_reports_no_identity():
    dataset = SftDataset(name="d")
    dataset.add(SftSample(instruction="联系 13812345678", output="好的 user-A"))
    payload = dataset.as_dict(Redactor())
    assert payload["count"] == 1 and payload["carries_user_id"] is False
    assert "13812345678" not in str(payload) and "user-A" not in str(payload)


def test_offline_link_must_not_touch_private_vault():
    """计划书三、P10「离线链路严禁访问未脱敏用户私有 Vault」。"""
    with pytest.raises(DatasetError):
        assert_offline_source(scope="vault")
    assert_offline_source(scope="public") is None


# ============================== 微调流水线（U28）============================== #
def test_methods_and_targets_follow_the_plan_and_u28():
    assert METHODS == ("lora", "qlora")
    assert TARGETS == (TARGET_JEV_TEMPLATE, TARGET_VERTICAL_MODEL)


def test_config_shape_and_qlora_quantization():
    cfg = FinetuneConfig(target=TARGET_JEV_TEMPLATE, base_model="Qwen2.5-7B",
                         dataset_name="jev_sft")
    payload = cfg.as_dict()
    assert payload["stage"] == "sft" and payload["finetuning_type"] == METHOD_LORA
    assert "quantization_bit" not in payload

    qlora = FinetuneConfig(target=TARGET_JEV_TEMPLATE, base_model="Qwen2.5-7B",
                           dataset_name="jev_sft", method=METHOD_QLORA)
    assert qlora.as_dict()["quantization_bit"] == 4


def test_config_rejects_unknown_values():
    with pytest.raises(PipelineError):
        FinetuneConfig(target="nope", base_model="m", dataset_name="d")
    with pytest.raises(PipelineError):
        FinetuneConfig(target=TARGET_JEV_TEMPLATE, base_model="", dataset_name="d")


def test_pipeline_skips_when_not_due():
    pipeline = FinetunePipeline(base_models={t: "m" for t in TARGETS}, interval_days=7)
    runs = pipeline.plan(now=NOW, last_run_at=NOW - timedelta(days=1),
                         sample_counts={t: 999 for t in TARGETS})
    assert all(not r.should_run for r in runs)
    assert "未到周期" in runs[0].reason


def test_pipeline_refuses_to_train_on_too_few_samples():
    """用很少的数据反复微调只会把模型带偏，所以明确不跑而不是尽力而为。"""
    pipeline = FinetunePipeline(base_models={t: "m" for t in TARGETS}, min_samples=200)
    runs = pipeline.plan(now=NOW, last_run_at=None,
                         sample_counts={TARGET_JEV_TEMPLATE: 10,
                                        TARGET_VERTICAL_MODEL: 500})
    by = {r.target: r for r in runs}
    assert by[TARGET_JEV_TEMPLATE].should_run is False
    assert "样本不足" in by[TARGET_JEV_TEMPLATE].reason
    assert by[TARGET_VERTICAL_MODEL].should_run is True
    assert by[TARGET_VERTICAL_MODEL].config["dataset"] == TARGET_VERTICAL_MODEL


def test_pipeline_reports_missing_base_model():
    pipeline = FinetunePipeline(base_models={}, min_samples=1)
    runs = pipeline.plan(now=NOW, last_run_at=None,
                         sample_counts={t: 10 for t in TARGETS})
    assert all(not r.should_run for r in runs)
    assert "未配置" in runs[0].reason


def test_pipeline_declares_training_is_not_run_in_ci():
    payload = FinetunePipeline(base_models={}).as_dict()
    assert payload["training_in_ci"] is False
    assert "U28" in payload["note"]


# ============================== 报告（三、P9 步骤6）============================== #
def test_report_contains_jev_and_wiki_sections():
    report = build_report(
        board={"metrics": {}, "scope": "public"},
        deposit={"total": 3, "good": 2, "bad": 1, "support_ratio": 2 / 3,
                 "by_category": {CATEGORY_JEV_ERROR: 1, CATEGORY_WIKI_CONFLICT: 1}},
        dataset={"name": "d", "count": 5, "sources": ["public_workspace"]},
        pipeline={"training_in_ci": False}, runs=[], generated_at=NOW.isoformat(),
        redactor=Redactor())
    assert report["sections"] == list(REPORT_SECTIONS)
    assert report["jev"]["error_cases"] == 1
    assert report["wiki"]["conflict_cases"] == 1
    assert report["wiki"]["scope"] == "public"


def test_report_markdown_renders():
    report = build_report(board={"metrics": {}}, deposit={}, dataset={},
                          pipeline={}, runs=[], generated_at=NOW.isoformat())
    text = render_markdown(report)
    assert "Jev 质量" in text and "知识库质量" in text
    assert "是否携带用户身份：否" in text


def test_report_is_redacted():
    report = build_report(
        board={"metrics": {}}, deposit={"records": [{"summary": "user-A 的案例"}]},
        dataset={}, pipeline={}, runs=[], generated_at=NOW.isoformat(),
        redactor=Redactor())
    assert "user-A" not in str(report)


# ============================== 阶段 Agent ============================== #
@pytest.fixture()
def wiki(settings, workdir):
    store = WikiStore(workdir / "wiki", settings=settings)
    store.write_page(new_page(CLAIM, "c-pub", confidence=0.9, claim_type="fact",
                              description="d", subject_id="a", object_id="b",
                              evidence_ids=["e-1"], updated_at=NOW.isoformat()))
    return store


def _agent(wiki, **over):
    available = {CAP_WIKI_READ: over.get("wiki", wiki),
                 CAP_DATA_READ: over.get("sdk")}
    return EvaluationAgent(settings=over.get("settings"),
                           tools=grant(EvaluationAgent, available),
                           analyzer=over.get("analyzer"))


def _state(**extra):
    state = new_state(user_id="user-A", session_id="s1")
    state.update(extra)
    return state


def test_agent_produces_board_cases_dataset_pipeline_and_report(wiki):
    out = _call(_agent(wiki), _state(
        jev_observations=[{"expected_intent": "travel", "actual_intent": "travel"},
                          {"expected_intent": "travel", "actual_intent": "food",
                           "multi_intent": True, "detected_multi": True}],
        log_rows=[{"is_good": True}, {"is_good": False, "jev_mismatch": True}],
        sft_samples=[{"instruction": "i", "output": "o"}],
        sample_counts={TARGET_JEV_TEMPLATE: 500, TARGET_VERTICAL_MODEL: 500},
        base_models={TARGET_JEV_TEMPLATE: "m1", TARGET_VERTICAL_MODEL: "m2"}))
    payload = out["p9_evaluation"]
    assert set(payload["board"]["metrics"]) == set(METRIC_KEYS)
    assert payload["cases"]["total"] == 2
    assert payload["dataset"]["count"] == 1
    assert len(payload["runs"]) == 2
    assert "jev" in payload["report"] and "wiki" in payload["report"]


def test_agent_board_defaults_to_public_scope(wiki):
    """系统级看板不聚合私有 Vault——小样本聚合本身就可能反推个体。"""
    payload = _call(_agent(wiki), _state())["p9_evaluation"]
    assert payload["scope"] == SCOPE_PUBLIC


def test_agent_refuses_private_vault_samples(wiki):
    with pytest.raises(DatasetError):
        _call(_agent(wiki), _state(sft_samples=[
            {"instruction": "i", "output": "o", "scope": "vault"}]))


def test_agent_does_not_fabricate_dataset_from_private_content(wiki):
    wiki.write_page(new_page(CLAIM, "c-priv", confidence=0.9, user_id="user-B",
                             claim_type="fact", description="B 的私事",
                             subject_id="a", object_id="b", evidence_ids=["e"]),
                    user_id="user-B")
    payload = _call(_agent(wiki), _state())["p9_evaluation"]
    # 公共看板只看到公共页面，看不到 user-B 的私有 Claim
    assert payload["board"]["metrics"]["wiki_claim_avg_confidence"]["sample_size"] == 1


def test_agent_writes_only_its_own_field(wiki):
    out = _call(_agent(wiki), _state())
    assert set(out) == {"p9_evaluation"}


def test_agent_reports_redaction_layers(wiki):
    payload = _call(_agent(wiki), _state())["p9_evaluation"]
    assert payload["redaction"] == {"regex_layer": True, "model_layer": False}


def test_regex_layer_flag_is_derived_not_hardcoded(monkeypatch):
    """正则层标志必须**随实际规则变化**。

    这条是给上面那条做的补充：上面只断言默认情况下是 True，
    一个恒返回 True 的实现同样能过。这里把规则表清空，
    标志就必须变成 False —— 否则说明它是写死的。

    （原先 p9 的 digest 里正是写死的 regex_layer: True：
    规则没了也照样报「已脱敏」。）
    """
    from ai_butler.evaluation import redaction as redaction_module

    monkeypatch.setattr(redaction_module, "_PATTERNS", ())
    empty = redaction_module.Redactor()
    assert empty.regex_layer_active is False, (
        "规则表已清空却仍报 True —— 这个标志是写死的，不是推导的"
    )


def test_agent_reports_derived_regex_flag(wiki, monkeypatch):
    """把规则表清空后，**Agent 报出来的**也必须是 False。"""
    from ai_butler.evaluation import redaction as redaction_module

    monkeypatch.setattr(redaction_module, "_PATTERNS", ())
    payload = _call(_agent(wiki), _state())["p9_evaluation"]
    assert payload["redaction"]["regex_layer"] is False, (
        "Agent 的 digest 没有跟着 redactor 走 —— 说明那里是写死的"
    )


def test_agent_skips_when_session_is_invalid(wiki):
    assert _agent(wiki)(_state(login_status=False)) == {}


def test_p9_is_now_marked_implemented():
    from ai_butler.core import phases
    spec = phases.phase("P9")
    assert spec.implemented is True
    assert spec.title == "指标评估与增量微调体系｜持续优化体系"
