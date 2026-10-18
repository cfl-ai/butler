"""P0-2 Agent 的能力面必须**实测**，不能是写死的字符串。

缺陷背景
--------
p0_devkit.py 原先在 digest 里写死了一串能力名：

    "capabilities": ["scaffold", "testkit", "prompt_registry",
                     "scenario_library", "evaluation", "log_query"]

它声称拥有这 6 项，但其中三项从没被 import 过。
**模块被改名或删掉，digest 依然报「有」。** 这正是「声明 ≠ 能力」，
只不过这次出在本项目自己的代码里。

现在改成 _probe_capabilities() 逐个真 import，缺的进
capabilities_missing；本文件盯着那个字段。
"""

from __future__ import annotations

from ai_butler.agents.p0_devkit import _DEVKIT_MODULES, _probe_capabilities
from ai_butler.agents.state import new_state
from ai_butler.core.config import Settings


def test_every_declared_capability_module_actually_imports():
    loaded, missing = _probe_capabilities()
    assert not missing, f"以下 P0-2 能力模块 import 不进来：{missing}"
    assert loaded == list(_DEVKIT_MODULES)


def test_reported_capabilities_are_not_silently_shrunk():
    """实测结果不能比声明的清单少 —— 少了就是有模块悄悄没了。"""
    loaded, _ = _probe_capabilities()
    assert set(loaded) == set(_DEVKIT_MODULES), (
        "digest 报的能力面与声明的清单不一致："
        f"声明 {sorted(_DEVKIT_MODULES)}，实测 {sorted(loaded)}"
    )


def test_probe_really_detects_a_missing_module(monkeypatch):
    """**给探测本身做的验收**：塞一个不存在的模块进去，必须落到 missing 里。

    没有这一条，上面两条无法证明探测真的在工作 ——
    一个永远返回「全都在」的实现同样能让它们变绿。
    """
    monkeypatch.setattr(
        "ai_butler.agents.p0_devkit._DEVKIT_MODULES",
        tuple(_DEVKIT_MODULES) + ("does_not_exist_module",),
    )
    loaded, missing = _probe_capabilities()
    assert missing == ["does_not_exist_module"], (
        f"探测没能发现不存在的模块，missing={missing}"
    )
    assert set(loaded) == set(_DEVKIT_MODULES)


def test_digest_reports_both_loaded_and_missing():
    """digest 必须同时给出「有哪些」和「缺哪些」，不能只报好消息。"""
    from ai_butler.agents.p0_devkit import DevkitAgent

    agent = DevkitAgent(settings=Settings())
    digest = agent.run(new_state(user_id="u", session_id="s"))["p0_devkit_digest"]

    assert "capabilities" in digest
    assert "capabilities_missing" in digest, (
        "digest 必须把缺失的能力也报出来 —— 只报好消息等于没报"
    )
    assert digest["capabilities_missing"] == []
    assert set(digest["capabilities"]) == set(_DEVKIT_MODULES)


def test_digest_still_reports_real_library_facts():
    """顺带确认 digest 里那些**本来就是实测**的字段还在。"""
    from ai_butler.agents.p0_devkit import DevkitAgent

    agent = DevkitAgent(settings=Settings())
    digest = agent.run(new_state(user_id="u", session_id="s"))["p0_devkit_digest"]

    assert digest["case_total"] > 0
    assert digest["case_pending"] == []
    assert digest["scenario_categories"], "场景类别应为实测结果，不能为空"
