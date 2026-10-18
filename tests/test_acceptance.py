"""项目落地验收标准的核查测试。

计划书依据：六、可复用资产库 / 七、验收标准（20 条）/ 八、表 13（18 个硬参数）

这组测试的作用是**把计划书的「完成」定义变成回归可查的东西**：
参数被改动、模块被改名、验收项指向的测试被删除，都会在这里变红。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from ai_butler.agent_wiki import ontology
from ai_butler.devkit.acceptance import (
    ACCEPTANCE_CRITERIA,
    AREA_JEV,
    AREA_NAMES,
    AREA_PLATFORM,
    AREA_PRIVACY,
    AREA_WIKI,
    CRITERIA_NUMBERS,
    GROUP_JEV,
    GROUP_NAMES,
    GROUP_ROUTING,
    GROUP_WIKI,
    REUSABLE_ASSETS,
    TABLE_13,
    TABLE_13_NAMES,
    criteria_by_area,
    params_by_group,
    verify_table_13,
)

ROOT = Path(__file__).resolve().parents[1]


# ============================== 八、表 13 ============================== #
def test_table_13_has_every_plan_parameter():
    """表 13 的三组共 18 个参数，一个都不能少。"""
    assert len(TABLE_13) == 18
    assert set(GROUP_NAMES) == {GROUP_ROUTING, GROUP_JEV, GROUP_WIKI}
    assert len(params_by_group(GROUP_ROUTING)) == 8
    assert len(params_by_group(GROUP_JEV)) == 4
    assert len(params_by_group(GROUP_WIKI)) == 6
    assert len(set(TABLE_13_NAMES)) == len(TABLE_13_NAMES), "参数名不得重复"


def test_table_13_values_match_the_plan_verbatim():
    """关键数值逐字取自计划书；改动即失败。"""
    values = {p.name: p.plan_value for p in TABLE_13}
    assert values["max_task_rollback_times"] == 2
    assert values["multi_intent_auto_resolve"] is False
    assert values["agent_rollback_original_forbidden"] is True
    assert values["route_loop_protect"] is True
    assert values["new_intent_auto_save"] is True
    assert values["conflict_intent_prompt_enable"] is True
    assert values["private_data_isolate"] is True
    assert values["base_collect_unified"] is True
    assert values["jev_enable"] is True
    assert values["jev_confidence_threshold"] == 0.82
    assert values["jev_fallback_enable"] is True
    assert values["jev_shadow_mode"] is True
    assert values["wiki_public_workspace_enable"] is True
    assert values["wiki_user_vault_per_user"] is True
    assert values["wiki_compile_auto_run"] is True
    assert values["wiki_auto_report_contradiction"] is True
    assert values["wiki_auto_report_low_confidence"] is True
    assert values["wiki_max_digest_size"] == 12000


def test_every_table_13_param_declares_where_it_is_enforced():
    """**没有落点的参数必须被报出来**，而不是静默跳过。"""
    missing = [p.name for p in TABLE_13 if not p.enforced_at]
    assert not missing, f"这些表 13 参数未声明落点：{missing}"


def test_table_13_matches_the_code(settings):
    """计划书值 == 代码值。这是本模块存在的意义。"""
    problems = verify_table_13(settings=settings)
    assert not problems, "表 13 与实现不一致：\n" + "\n".join(
        f"{m.name}: 计划 {m.plan_value!r}，实际 {m.actual_value!r}（{m.reason}）"
        for m in problems)


def test_configurable_params_really_are_settings_fields():
    for param in TABLE_13:
        if not param.configurable:
            continue
        assert param.name in _settings_cls().model_fields, \
            f"{param.name} 标为可配置，但 Settings 里没有该字段"


def _settings_cls():
    """返回 Settings **类本身**（pydantic 的 model_fields 挂在类上）。"""
    from ai_butler.core.config import Settings
    return Settings


def test_non_configurable_params_are_frozen_behaviour_not_dead_entries():
    """行为固化的参数：落点模块必须真的能导入。"""
    for param in TABLE_13:
        if param.configurable:
            continue
        module = param.enforced_at.split("::")[0]
        importlib.import_module(module)


# ============================== 六、可复用资产库 ============================== #
def test_reusable_asset_library_has_twelve_entries():
    assert len(REUSABLE_ASSETS) == 12


def test_every_reusable_asset_module_imports():
    """资产清单指向的模块必须真实存在——改名/删除会在这里变红。"""
    problems: list[str] = []
    for asset in REUSABLE_ASSETS:
        try:
            importlib.import_module(asset.module)
        except Exception as error:
            problems.append(f"{asset.name} -> {asset.module}: {error}")
    assert not problems, "可复用资产模块不可导入：\n" + "\n".join(problems)


def test_asset_12_name_deliberately_differs_from_the_plan_per_u08():
    """第 12 条资产的名称与计划书**刻意不同**，依据是裁定 U08。

    计划书原文：「**基于 Claude Code 的**全自动系统自进化闭环」。
    裁定 U08（人工授权）：「**不引入 Claude Code 本体**；仅借用其设计思维…
    自研 Agentic Loop」。所以名称去掉了「基于 Claude Code」。

    这条测试防的是「有人照着计划书把名字改回去」——
    那样会与 U08 冲突，而**改回去原本不会有任何测试变红**：
    资产名称此前没有任何约束，备注也是空的。
    """
    from ai_butler.core import decisions as d

    asset = REUSABLE_ASSETS[11]
    assert "Claude Code" not in asset.name, (
        "资产名称不应写 Claude Code —— 裁定 U08 明令不引入其本体")
    assert "U08" in asset.note, "名称偏离计划书必须在备注里写明依据"
    assert asset.module == "ai_butler.harness"

    # 依据本身也要还在：裁定被删或被改，这条就失效了
    u08 = next(x for x in d.DECISIONS if getattr(x, "key") == "U08")
    assert "不引入 Claude Code 本体" in u08.resolution, (
        "裁定 U08 的内容变了 —— 资产名称的依据需要重新确认")


def test_the_twelve_assets_cover_every_phase_group():
    """12 类资产应覆盖底座、调度、知识库、质检、语音、支付、自进化各面。"""
    modules = " ".join(a.module for a in REUSABLE_ASSETS)
    for needle in ("infra", "devkit", "scheduler", "agent_wiki", "data_platform",
                   "rag", "p6_content_qa", "p8_payment", "harness", "mobile"):
        assert needle in modules, f"资产库缺少 {needle} 相关条目"


# ============================== 七、验收标准 ============================== #
def test_twenty_acceptance_criteria_are_numbered_1_to_20():
    assert CRITERIA_NUMBERS == tuple(range(1, 21))


def test_criteria_are_partitioned_into_the_four_plan_groups():
    assert len(AREA_NAMES) == 4
    areas = {c.area for c in ACCEPTANCE_CRITERIA}
    assert areas == set(AREA_NAMES)
    assert len(criteria_by_area(AREA_PLATFORM)) == 8
    assert len(criteria_by_area(AREA_PRIVACY)) == 6
    assert len(criteria_by_area(AREA_JEV)) == 2
    assert len(criteria_by_area(AREA_WIKI)) == 4


def test_privacy_group_is_marked_must_pass():
    """计划书原文：账号 & 隐私防护验收标准（**必须全部通过**）。"""
    privacy = criteria_by_area(AREA_PRIVACY)
    assert all(c.must_pass for c in privacy)
    assert len(privacy) == 6
    assert not any(c.must_pass for c in criteria_by_area(AREA_PLATFORM))


def test_every_criterion_points_at_a_real_test():
    """验收项不得与实现漂移：引用的测试必须真实存在。"""
    problems: list[str] = []
    for case in ACCEPTANCE_CRITERIA:
        ref = case.verified_by
        external = ref.startswith("dart:")
        body = ref[5:] if external else ref
        file_part, _, func = body.partition("::")
        path = ROOT / file_part.strip()
        if not path.is_file():
            problems.append(f"验收{case.number} 引用的文件不存在：{file_part}")
            continue
        if not func:
            continue
        text = path.read_text(encoding="utf-8")
        name = func.strip() if external else re.split(r"[（(]", func)[0].strip()
        needle = f"test('{name}'" if external else f"def {name}("
        if needle not in text:
            problems.append(f"验收{case.number} 引用的测试不存在：{file_part}::{name}")
    assert not problems, "验收标准与实现不一致：\n" + "\n".join(problems)


def test_acceptance_criteria_all_have_text():
    for case in ACCEPTANCE_CRITERIA:
        assert len(case.text) >= 10, f"验收{case.number} 描述过短"
        assert case.verified_by, f"验收{case.number} 未标注验证位置"


# ============================== 验收 18 的本体完整性 ============================== #
#: 计划书验收 18 点名的六类本体要素，以及本体实际导出的常量名
CRITERION_18_ELEMENTS: tuple[tuple[str, str], ...] = (
    ("Source 来源", "SOURCE"),
    ("Claim 声明", "CLAIM"),
    ("Evidence 证据", "EVIDENCE"),
    ("Relation 关系", "RELATION"),
    ("Contradiction 矛盾", "CONTRADICTION"),
    ("Question 开放问题", "QUESTION"),
)


def test_ontology_has_all_six_elements_from_criterion_18():
    """验收 18 前半段：本体必须含六类要素。

    **这条以前没有覆盖** —— 是制作验收清单时才发现的缺口，因此补齐。
    """
    for label, constant in CRITERION_18_ELEMENTS:
        page_type = getattr(ontology, constant, None)
        assert page_type, f"本体缺少 {label}"
        assert page_type in ontology.PAGE_TYPES, f"{label} 未登记进 PAGE_TYPES"


def test_ontology_declares_required_fields_for_every_element():
    """每类要素都有必填字段约束，不是空壳。"""
    for _, constant in CRITERION_18_ELEMENTS:
        page_type = getattr(ontology, constant)
        assert page_type in ontology.REQUIRED_EXTRA, f"{page_type} 未声明必填字段"
        assert ontology.REQUIRED_EXTRA[page_type], f"{page_type} 必填字段为空"


def test_ontology_also_covers_synthesis_for_criterion_20():
    """验收 20 要求行程产出可生成为 synthesis 页存入私有 Vault。"""
    assert ontology.SYNTHESIS in ontology.PAGE_TYPES
    assert ontology.REQUIRED_EXTRA.get(ontology.SYNTHESIS)


#: 计划书本体的**完整**清单（原文见《Agent-Wiki 知识库开发规范》§1 的逐字引用）。
#: 注意：验收 18 只点了其中六项，所以**不能只照验收 18 查** ——
#: 六项 + synthesis = 七项，Entity 会整个落空。
PLAN_ONTOLOGY_ELEMENTS: tuple[str, ...] = (
    "ENTITY", "SOURCE", "CLAIM", "EVIDENCE",
    "RELATION", "CONTRADICTION", "QUESTION", "SYNTHESIS",
)


def test_ontology_covers_every_element_the_plan_defines():
    """按计划书原文的**八**要素守，而不是按验收 18 的六项。

    这正是先前漏掉 Entity 的原因：验收 18 列了六项、验收 20 提了 synthesis，
    两条合起来看着像齐了，实则少一个 —— 而少了不会报错。
    """
    for constant in PLAN_ONTOLOGY_ELEMENTS:
        page_type = getattr(ontology, constant, None)
        assert page_type, f"本体缺少计划书定义的要素 {constant}"
        assert page_type in ontology.PAGE_TYPES, f"{constant} 未登记进 PAGE_TYPES"
    assert len(ontology.PAGE_TYPES) == len(PLAN_ONTOLOGY_ELEMENTS), (
        f"PAGE_TYPES 与计划书八要素数量不符：{ontology.PAGE_TYPES}"
    )


def test_every_element_declares_required_fields_including_entity():
    """必填字段要覆盖**全部**要素。

    上面那条只遍历了验收 18 的六项，Entity 的字段约束因此从未被检查过。
    """
    for constant in PLAN_ONTOLOGY_ELEMENTS:
        page_type = getattr(ontology, constant)
        assert ontology.REQUIRED_EXTRA.get(page_type), f"{page_type} 未声明必填字段"
    # Entity 的字段来自计划书「实体 Entity」的定义
    assert ontology.REQUIRED_EXTRA[ontology.ENTITY] == ("name", "entity_type")


def test_ontology_compiles_machine_readable_output():
    """验收 18 后半段：编译可输出机器可读缓存（由编译流水线用例覆盖）。"""
    from ai_butler.agent_wiki import compiler
    importlib.import_module("ai_butler.agent_wiki.compiler")
    assert compiler is not None
