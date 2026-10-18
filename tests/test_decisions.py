"""未定义项裁定台账测试。

计划书依据：Skill-05 要求未定义项不得由 AI 自行填补；
用户指示「所有未定义项可直接检索 GitHub 中开源项目数据进行借鉴」。
本测试保证每一条裁定都可追溯、且 License 边界被显式记录。
"""

from __future__ import annotations

import re

from ai_butler.core import decisions as d


def test_every_decision_is_traceable():
    for item in d.DECISIONS:
        assert item.key and item.title and item.phase, item.key
        assert item.plan_quote, f"{item.key} 缺少计划书原文片段"
        assert item.resolution, f"{item.key} 缺少裁定内容"
        if item.status == d.RESOLVED:
            assert item.source in ("人工授权", "开源借鉴", "文档推导", "实现参数"), item.key
            assert item.decided_at, f"{item.key} 缺少裁定日期"


def test_open_source_borrowings_have_references_and_license_notes():
    for item in d.DECISIONS:
        if item.source != "开源借鉴":
            continue
        assert item.references, f"{item.key} 标为开源借鉴却没有出处 URL"
        assert item.license_note, f"{item.key} 缺少 License 说明"
        assert all(r.startswith("https://") for r in item.references), item.key


def test_copyleft_sources_are_design_only():
    """已采用来源若为 AGPL / GPL / 无 LICENSE，只能标 design_only，不得复用代码或数据。

    只看 license_note（已采用来源）；excluded_note 记录的是被排除备选的许可警告，
    不影响本项的可复用性判定。
    """
    copyleft = re.compile(r"AGPL|GPL|无 LICENSE|未声明", re.I)
    for item in d.DECISIONS:
        if item.license_note and copyleft.search(item.license_note):
            assert item.code_reuse == "design_only", (
                f"{item.key} 已采用的来源含传染性/未声明许可，必须标 design_only"
            )


def test_code_reuse_values_are_meaningful():
    """code_reuse 不得被误用：开源借鉴项必须明确 allowed 或 design_only；
    人工授权/实现参数项应标 n/a（无外部来源可复用）。"""
    for item in d.DECISIONS:
        assert item.code_reuse in ("allowed", "design_only", "n/a"), item.key
        if item.source == "开源借鉴":
            assert item.code_reuse in ("allowed", "design_only"), (
                f"{item.key} 标为开源借鉴，必须明确代码复用边界"
            )
        elif item.source in ("人工授权", "实现参数", "文档推导"):
            assert item.code_reuse == "n/a", f"{item.key} 无外部来源，应为 n/a"


def test_document_derived_items_quote_the_plan():
    """文档推导类裁定必须引用计划书原文，且不得冒充人工授权。"""
    derived = [i for i in d.DECISIONS if i.source == "文档推导"]
    assert derived, "至少 U24 应为文档推导"
    for item in derived:
        assert item.plan_quote, f"{item.key} 缺少推导依据的原文片段"
        assert "计划书" in item.resolution or "原文" in item.resolution, item.key


def test_excluded_alternatives_do_not_downgrade_reuse():
    """被排除的备选不应把本项的可复用性判死。"""
    item = d.get("U07")
    assert "AGPL" in item.excluded_note
    assert item.code_reuse == "allowed"


def test_no_duplicate_keys_and_pending_is_explicit():
    keys = [i.key for i in d.DECISIONS]
    assert len(keys) == len(set(keys)), "裁定编号重复"
    for item in d.pending():
        assert item.status == d.PENDING and item.resolution


def test_known_human_authorized_items_are_present():
    for key in ("U04", "U08", "U09", "U10", "U12"):
        assert d.get(key).source == "人工授权"


def test_open_source_backed_items_are_present():
    for key in ("U01", "U02", "U03", "U07", "U11", "U15", "U16", "U17", "U18", "U19"):
        assert d.get(key).source == "开源借鉴", key
