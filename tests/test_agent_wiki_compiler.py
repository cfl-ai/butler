"""Agent-Wiki v2 编译流水线测试。

计划书依据：一、项目概述（三份机器可读缓存）；三、P0-1 编译流水线；
三、P2 过期归档；七、19 矛盾/证据缺失/过期素材报告；八、wiki_* 6 项参数。
"""

from __future__ import annotations

import json

from ai_butler.agent_wiki.compiler import CLAIMS_JSONL, DIGEST_JSON, PAGES_JSON, WikiCompiler
from ai_butler.agent_wiki.ontology import (
    CLAIM,
    CONTRADICTION,
    EVIDENCE,
    SOURCE,
    new_page,
)
from ai_butler.agent_wiki.workspace import WikiStore


def _store(settings, workdir, **over):
    return WikiStore(workdir, settings=settings.model_copy(update=over))


def _claim(page_id, *, user_id=None, confidence=0.9, status="TRUE", evidence_ids=("ev-1",)):
    return new_page(CLAIM, page_id, title=page_id, confidence=confidence, user_id=user_id,
                    status=status, claim_type="fact", description=f"{page_id} 描述",
                    subject_id="entity:西湖", object_id="entity:春季",
                    evidence_ids=list(evidence_ids))


def _seed(store, *, user_id=None):
    store.write_page(new_page(SOURCE, "src-1", url="https://example.com/a",
                              user_id=user_id, status="active"), user_id=user_id)
    store.write_page(new_page(SOURCE, "src-old", url="https://example.com/old",
                              user_id=user_id, status="archived"), user_id=user_id)
    store.write_page(new_page(EVIDENCE, "ev-1", user_id=user_id, source_id="src-1",
                              source_text="西湖春季最佳", text_unit_id="tu-1"), user_id=user_id)
    store.write_page(_claim("claim-high", user_id=user_id, confidence=0.95), user_id=user_id)
    store.write_page(_claim("claim-low", user_id=user_id, confidence=0.40), user_id=user_id)
    store.write_page(_claim("claim-noev", user_id=user_id, confidence=0.9,
                            evidence_ids=()), user_id=user_id)
    # U16：矛盾不删旧声明
    store.write_page(new_page(CLAIM, "claim-conflict", user_id=user_id, confidence=0.8,
                              status="SUSPECTED", claim_type="fact", description="春季非最佳",
                              subject_id="entity:西湖", object_id="entity:秋季",
                              evidence_ids=["ev-1"],
                              invalid_at=None, contradicts=["claim-high"]), user_id=user_id)
    store.write_page(new_page(CONTRADICTION, "ctr-1", user_id=user_id,
                              left_id="claim-high", right_id="claim-conflict",
                              reason="最佳季节冲突"), user_id=user_id)


def test_compile_produces_the_three_plan_artifacts(settings, workdir):
    """一、项目概述：agent-digest.json / claims.jsonl / pages.json。"""
    store = _store(settings, workdir)
    _seed(store)
    report = WikiCompiler(store).compile_workspace()

    assert report.page_count == 8
    assert report.claim_count == 4
    # 该用例的 claim 都缺/带证据混合，仅 claim-noev 缺证据
    assert report.missing_evidence == ["claim-noev"]
    for name in (PAGES_JSON, CLAIMS_JSONL, DIGEST_JSON):
        assert name in report.outputs
    out = workdir / "workspace" / "_compiled"
    assert (out / PAGES_JSON).exists()
    assert (out / CLAIMS_JSONL).exists()
    assert (out / DIGEST_JSON).exists()


def test_claims_jsonl_rows_carry_u17_fields(settings, workdir):
    store = _store(settings, workdir)
    _seed(store)
    WikiCompiler(store).compile_workspace()
    lines = (workdir / "workspace" / "_compiled" / CLAIMS_JSONL).read_text(
        encoding="utf-8").strip().splitlines()
    rows = [json.loads(x) for x in lines]
    assert {r["claim_id"] for r in rows} == {"claim-high", "claim-low", "claim-noev", "claim-conflict"}
    for r in rows:
        for field in ("claim_id", "claim_type", "description", "subject_id", "object_id",
                      "status", "confidence", "valid_from", "valid_to",
                      "invalid_at", "contradicts", "evidence_ids", "user_id"):
            assert field in r, f"claims.jsonl 缺少 U17 字段 {field}"
        assert r["status"] in ("TRUE", "FALSE", "SUSPECTED", "ARCHIVED")


def test_contradiction_is_reported_and_old_claim_survives(settings, workdir):
    """U16：矛盾不删除旧声明，保留时间线。"""
    store = _store(settings, workdir)
    _seed(store)
    report = WikiCompiler(store).compile_workspace()
    assert "ctr-1" in report.contradictions
    assert "claim-high" in report.contradictions          # 被指向的一方也在报告中
    assert store.read_page("claim-high") is not None, "旧声明不得被删除"


def test_low_confidence_uses_plan_threshold_not_a_new_number(settings, workdir):
    """低置信报告沿用【八】jev_confidence_threshold=0.82，不新造阈值。"""
    store = _store(settings, workdir)
    _seed(store)
    report = WikiCompiler(store).compile_workspace()
    assert "claim-low" in report.low_confidence
    assert "claim-high" not in report.low_confidence
    assert settings.jev_confidence_threshold == 0.82


def test_archived_source_is_reported(settings, workdir):
    """三、P2：过期来源标记为 status:archived。"""
    store = _store(settings, workdir)
    _seed(store)
    report = WikiCompiler(store).compile_workspace()
    assert "src-old" in report.archived
    assert "src-1" not in report.archived


def test_vault_compiles_separately_from_workspace(settings, workdir):
    store = _store(settings, workdir)
    _seed(store, user_id=None)
    _seed(store, user_id="user-A")
    compiler = WikiCompiler(store)
    assert compiler.compile_workspace().claim_count == 4
    vault_report = compiler.compile_vault("user-A")
    assert vault_report.claim_count == 4 and vault_report.user_id == "user-A"
    assert (workdir / "vault" / "user-A" / "_compiled" / CLAIMS_JSONL).exists()
    assert not (workdir / "vault" / "workspace").exists()   # 作用域目录不混


def test_digest_respects_plan_size_limit(settings, workdir):
    """八、wiki_max_digest_size = 12000 字节上限；超限裁剪并显式标记 truncated。"""
    assert settings.wiki_max_digest_size == 12000

    store = _store(settings, workdir)
    for i in range(80):
        store.write_page(_claim(f"c{i:03d}", confidence=0.5 + i / 1000), user_id=None)
    report = WikiCompiler(store).compile_workspace()
    assert report.digest_bytes <= 12000

    tiny = _store(settings, workdir, wiki_max_digest_size=400)
    report2 = WikiCompiler(tiny).compile_workspace()
    assert report2.digest_bytes <= 400
    assert report2.digest_truncated is True
    digest = json.loads((workdir / "workspace" / "_compiled" / DIGEST_JSON).read_text(encoding="utf-8"))
    assert digest["truncated"] is True and digest["claim_count"] < 80
    # 裁剪按置信度从高到低保留
    assert digest["claims"][0]["claim_id"] == "c079"


def test_auto_run_flag_disables_writing_cache_files(settings, workdir):
    store = _store(settings, workdir, wiki_compile_auto_run=False)
    _seed(store)
    report = WikiCompiler(store).compile_workspace()
    assert report.outputs == {}
    assert not (workdir / "workspace" / "_compiled").exists()
    assert report.contradictions, "报告仍应产出，只是不落盘"
