"""P0-2 Prompt 版本库测试。

计划书依据：三、P0-2「搭建 Prompt 版本库：保存、对比、回滚、A/B 测试；
同步管理 Jev 决策 Question 模板版本」
"""

from __future__ import annotations

import pytest

from ai_butler.core.storage import FileStore
from ai_butler.devkit.prompt_registry import (
    DEFAULT_AB_SPLIT,
    JEV_TEMPLATE_PREFIX,
    PromptError,
    PromptRegistry,
)


@pytest.fixture()
def registry(settings, workdir):
    return PromptRegistry(FileStore(workdir / "storage", settings=settings))


def test_save_assigns_monotonic_versions(registry):
    v1 = registry.save("planner.system", "你是行程规划助手。", note="初版")
    v2 = registry.save("planner.system", "你是行程规划助手，需标注置信度。", note="加置信度")
    assert (v1.version, v2.version) == (1, 2)
    assert registry.get("planner.system").version == 2
    assert registry.get("planner.system", 1).template == "你是行程规划助手。"
    assert len(registry.history("planner.system")) == 2
    assert v1.sha256 != v2.sha256


def test_save_rejects_empty_input(registry):
    with pytest.raises(PromptError):
        registry.save("", "x")
    with pytest.raises(PromptError):
        registry.save("n", "")


def test_diff_shows_the_change(registry):
    registry.save("p", "line1\nline2\n")
    registry.save("p", "line1\nline2-changed\n")
    text = registry.diff("p", 1, 2)
    assert "-line2" in text and "+line2-changed" in text


def test_rollback_creates_new_version_without_deleting_history(registry):
    registry.save("p", "v1 内容")
    registry.save("p", "v2 内容")
    rolled = registry.rollback("p", 1, note="回滚")

    assert rolled.version == 3
    assert rolled.template == "v1 内容"
    assert rolled.rolled_back_from == 1
    assert [v.version for v in registry.history("p")] == [1, 2, 3], "历史不得被删除"


def test_ab_assignment_is_stable_per_user(registry):
    registry.save("exp", "A 版本")
    registry.save("exp", "B 版本")

    first = registry.ab_assign("exp", user_id="u-777")
    for _ in range(20):
        assert registry.ab_assign("exp", user_id="u-777") == first, "同一用户必须稳定落组"

    groups = {registry.ab_assign("exp", user_id=f"u-{i}") for i in range(200)}
    assert groups == {"A", "B"}, "两个分组都应被覆盖到"


def test_ab_requires_user_id(registry):
    """计划书：禁止不带用户身份的裸访问。"""
    with pytest.raises(PromptError):
        registry.ab_assign("exp", user_id="")


def test_ab_versions_maps_groups_to_the_last_two_versions(registry):
    registry.save("exp", "old")
    registry.save("exp", "new")
    mapping = registry.ab_versions("exp")
    assert mapping["A"].template == "old" and mapping["B"].template == "new"
    with pytest.raises(PromptError):
        registry.ab_versions("只有一版")


def test_jev_question_templates_are_managed_under_one_prefix(registry):
    """计划书三、P0-2：同步管理 Jev 决策 Question 模板版本。"""
    registry.save_jev_question("intent", "判定主意图：{{question}}")
    registry.save_jev_question("intent", "判定主意图与候选列表：{{question}}")
    registry.save_jev_question("conflict", "判定是否多意图冲突：{{question}}")
    registry.save("planner.system", "无关的普通 Prompt")

    assert registry.jev_question_names() == [
        f"{JEV_TEMPLATE_PREFIX}conflict", f"{JEV_TEMPLATE_PREFIX}intent"]
    assert len(registry.history(f"{JEV_TEMPLATE_PREFIX}intent")) == 2
    assert "planner.system" in registry.names()


def test_default_ab_split_is_an_implementation_parameter(registry):
    """计划书未定义分流比例 → 作为实现参数显式登记，可覆盖。"""
    assert DEFAULT_AB_SPLIT == {"A": 1, "B": 1}
    registry.save("p", "a")
    registry.save("p", "b")
    assert registry.ab_assign("p", user_id="u", split={"A": 1, "B": 9}) in ("A", "B")
    with pytest.raises(PromptError):
        registry.ab_assign("p", user_id="u", split={"A": 0, "B": 1})
