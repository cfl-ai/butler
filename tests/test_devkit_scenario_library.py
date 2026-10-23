"""P0-2 全场景测试用例库测试。

计划书依据：三、P0-2「建立全场景测试用例库：正常、边界、异常、弱网、缺失数据、矛盾需求」
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from ai_butler.devkit import scenario_library as sl
from ai_butler.devkit.testkit import SCENARIOS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def library():
    return sl.default_library()


def test_library_covers_all_six_plan_scenarios(library):
    assert library.missing_categories() == [], "计划书要求的六类场景存在空白"
    coverage = library.coverage()
    assert set(coverage) == set(SCENARIOS)
    assert all(n > 0 for n in coverage.values()), coverage


def test_plan_named_special_cases_are_all_registered(library):
    """计划书三、P0-2 表格逐字点名的专项测试集合，缺一不可。"""
    ids = {c.case_id for c in library.cases}
    for required in ("SEC-001", "SEC-002", "JEV-001", "JEV-002", "JEV-003",
                     "WIKI-001", "WIKI-002", "WIKI-003", "WIKI-004"):
        assert required in ids, f"缺少计划书点名的专项用例 {required}"


def test_pending_cases_are_explicit_and_never_claim_coverage(library):
    """计划书点名但尚未实现的用例必须显式标 pending，不得混入已覆盖。

    不硬编码具体编号：待实现清单为空（全部落地）也是合法状态，
    真正要守住的不变量是「凡标 pending 者必须写明补齐阶段且不得声称已实现」。
    """
    for case in library.pending_cases():
        assert case.note, f"{case.case_id} 标为待实现时必须说明补齐阶段"
        assert not case.implemented_at, f"{case.case_id} 不得同时声称已实现"
        assert case.status == sl.PENDING


def test_jev003_multi_intent_conflict_is_now_implemented(library):
    """JEV-003 曾在 P0-2 阶段挂账，P3 场景调度落地后必须转为已实现。"""
    case = next(c for c in library.cases if c.case_id == "JEV-003")
    assert case.status != sl.PENDING, "JEV-003 已由 P3 实现，不得继续挂账"
    assert case.implemented_at, "JEV-003 必须指向真实测试"
    assert "multi_intent_auto_resolve=False" in case.plan_ref


def test_every_registered_case_points_at_a_real_test(library):
    """用例库不得与实现漂移：每条**已实现**用例引用的测试函数必须真实存在。"""
    problems: list[str] = []
    for case in library.cases:
        if case.status == sl.PENDING:
            continue
        ref = case.implemented_at
        if not ref:
            problems.append(f"{case.case_id} 未标注实现位置")
            continue
        # 两种引用形式：
        #   tests/xxx.py::test_name          —— Python 用例
        #   dart:mobile/.../xxx_test.dart::用例名 —— 移动端 Dart 用例（P7）
        # 三种引用形式：
        #   tests/xxx.py::test_name           —— Python 用例
        #   dart:.../xxx_test.dart::用例名     —— Dart 测试
        #   dart-script:.../xxx.dart          —— Dart **验证脚本**（main() 形式，不是 test()）
        external = ref.startswith("dart:") or ref.startswith("dart-script:")
        script = ref.startswith("dart-script:")
        if script:
            body = ref[len("dart-script:"):]
        elif external:
            body = ref[len("dart:"):]
        else:
            body = ref
        file_part, _, func = body.partition("::")
        path = ROOT / file_part.strip()
        if not path.is_file():
            problems.append(f"{case.case_id} 引用的文件不存在：{file_part}")
            continue
        if func:
            # Python 侧引用允许在函数名后带说明（如 test_x（中文备注）），故截断取函数名；
            # Dart 侧引用的就是**用例名字面量**，含中文括号也必须原样比对，不能截断。
            name = func.strip() if external else re.split(r"[（(]", func)[0].strip()
            text = path.read_text(encoding="utf-8")
            if script:
                if "void main(" not in text:
                    problems.append(
                        f"{case.case_id} 引用的 Dart 脚本没有 main()：{file_part}")
            elif external:
                # Dart 有两种用例声明形式：
                #   test('用例名', () async { ... })        —— 纯 Dart
                #   testWidgets('用例名', (tester) async {}) —— Flutter 组件测试
                # 只认 test( 会把组件测试全判成「不存在」（这一版就踩过）。
                if not any(f"{fn}('{name}'" in text
                           for fn in ("test", "testWidgets")):
                    problems.append(
                        f"{case.case_id} 引用的 Dart 用例不存在：{file_part}::{name}")
            elif f"def {name}(" not in text:
                problems.append(f"{case.case_id} 引用的测试不存在：{file_part}::{name}")
    assert not problems, "用例库与实现不一致：\n" + "\n".join(problems)


def test_register_rejects_unknown_category(library):
    with pytest.raises(sl.ScenarioError):
        sl.ScenarioLibrary().register(
            sl.ScenarioCase("X-1", "随便一类", "m", "d", "三、P0-2"))


def test_register_rejects_duplicate_and_missing_plan_ref():
    lib = sl.ScenarioLibrary()
    lib.register(sl.ScenarioCase("A-1", sl.NORMAL, "m", "d", "三、P0-2",
                                 implemented_at="tests/test_x.py::test_y"))
    with pytest.raises(sl.ScenarioError):
        lib.register(sl.ScenarioCase("A-1", sl.NORMAL, "m", "d", "三、P0-2",
                                     implemented_at="tests/test_x.py::test_y"))
    with pytest.raises(sl.ScenarioError):
        lib.register(sl.ScenarioCase("A-2", sl.NORMAL, "m", "d", ""))


def test_register_rejects_status_inconsistency():
    """已实现却无实现位置、待实现却无补齐说明——都不允许登记。"""
    with pytest.raises(sl.ScenarioError):
        sl.ScenarioLibrary().register(
            sl.ScenarioCase("B-1", sl.NORMAL, "m", "d", "三、P0-2"))          # 已实现但无位置
    with pytest.raises(sl.ScenarioError):
        sl.ScenarioLibrary().register(
            sl.ScenarioCase("B-2", sl.NORMAL, "m", "d", "三、P0-2",
                            status=sl.PENDING))                                # 待实现但无 note
    with pytest.raises(sl.ScenarioError):
        sl.ScenarioLibrary().register(
            sl.ScenarioCase("B-3", sl.NORMAL, "m", "d", "三、P0-2", status="随便"))


def test_text_export_lists_every_category_and_case(library):
    text = library.to_text()
    for name in SCENARIOS:
        assert name in text
    for case in library.cases:
        assert case.case_id in text


def test_module_coverage_is_reported(library):
    modules = library.by_module()
    for expected in ("账号隐私隔离", "Jev 决策", "Agent-Wiki 证据绑定", "四环境隔离", "三层网络隔离"):
        assert expected in modules, expected


# ------------------------------ CI 流水线 ------------------------------ #
def test_ci_workflow_is_valid_yaml_and_covers_plan_requirements():
    """计划书三、P0-2「制定 Git 分支规范、CI 自动构建、自动部署流水线」。"""
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    assert doc["name"] == "CI"
    assert "push" in doc[True] or "on" in doc
    triggers = doc.get(True, doc.get("on"))
    assert "main" in triggers["push"]["branches"]

    job = doc["jobs"]["test"]
    assert job["runs-on"]
    services = job["services"]
    assert services["postgres"]["image"].endswith("pgvector:pg16")
    assert services["redis"]["image"].startswith("redis:")

    script = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "python-version" in str(job["steps"])
    assert "pytest" in script
    assert "scenario_library" in script, "CI 必须校验全场景用例库覆盖"
    assert "export_for_harness" in script, "CI 必须导出评测数据供离线链路消费"
