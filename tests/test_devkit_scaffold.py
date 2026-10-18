"""P0-2 脚手架测试：生成的代码必须真的能跑。

计划书依据：三、P0-2「搭建 Agent 标准化脚手架：一键生成目录、Class、状态注册、单元测试；
脚手架自动注入 user_id 上下文；提供 Agent-Wiki 页面脚手架自动生成实体 / 声明元数据」
"""

from __future__ import annotations

import importlib.util
import py_compile
import sys

import pytest

from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.devkit import scaffold, testkit


def _load_generated(path):
    """直接按文件加载生成的 Agent 模块（其内部 import 仍指向项目内的基类）。"""
    spec = importlib.util.spec_from_file_location("generated_demo_agent", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generated_demo_agent"] = module
    spec.loader.exec_module(module)
    return module


def test_scaffold_generates_directory_class_and_test(workdir):
    result = scaffold.scaffold_agent(
        "weather_perception", root=workdir, description="天气感知 Agent",
        reads=("task_id",), writes=("weather_snapshot",))

    names = sorted(p.name for p in result.files)
    assert names == ["__init__.py", "agent.py", "test_agent_weather_perception.py"]
    assert result.class_name == "WeatherPerceptionAgent"

    pkg = workdir / "ai_butler" / "agents" / "weather_perception"
    assert (pkg / "agent.py").is_file()
    assert (workdir / "tests" / "test_agent_weather_perception.py").is_file()


def test_generated_code_compiles_and_actually_runs(workdir):
    result = scaffold.scaffold_agent(
        "weather_perception", root=workdir, description="天气感知 Agent",
        reads=("task_id",), writes=("weather_snapshot",))

    for path in result.files:
        py_compile.compile(str(path), doraise=True)          # 语法必须正确

    module = _load_generated(result.files[1])
    agent = module.WeatherPerceptionAgent()
    assert agent.name == "weather_perception"
    assert agent.writes == ("weather_snapshot",)

    state = testkit.make_state(user_id="u-9", session_id="s-9")
    updates = agent(state)
    testkit.assert_writes_within_scope(agent, updates)
    testkit.assert_contract_suite(agent)                     # 生成的 Agent 直接满足 P0-2 契约


def test_generated_agent_pulls_user_id_from_state_not_from_construction(workdir):
    """计划书三、P4：user_id 不允许硬编码、不能为空。"""
    result = scaffold.scaffold_agent(
        "weather_perception", root=workdir, writes=("weather_snapshot",))
    module = _load_generated(result.files[1])
    agent = module.WeatherPerceptionAgent()

    import inspect
    source = inspect.getsource(module)
    assert "self.require_user_id(state)" in source
    assert "user_id=" not in source.split("def run")[0]       # 不得作为构造参数注入


def test_scaffold_rejects_bad_inputs(workdir):
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold_agent("Bad-Name", root=workdir, writes=("x",))
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold_agent("no_writes", root=workdir, writes=())
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold_agent("bad_default", root=workdir, writes=("x",), default_write="y")

    scaffold.scaffold_agent("once_only", root=workdir, writes=("x",))
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.scaffold_agent("once_only", root=workdir, writes=("x",))


# ------------------------- Agent-Wiki 页面脚手架 ------------------------- #
def test_wiki_page_scaffold_generates_entity_and_claim_metadata(settings, workdir):
    store = WikiStore(workdir / "wiki", settings=settings)

    scaffold.scaffold_wiki_entity(store, entity_id="ent-1", name="西湖",
                                  entity_type="景点", now="2026-10-16T00:00:00Z")
    page = store.read_page("ent-1")
    assert page is not None and page.type == "entity"
    assert page.extra["entity_type"] == "景点"

    scaffold.scaffold_wiki_claim(
        store, claim_id="clm-1", subject_id="ent-1", object_id="ent-2",
        description="西湖春季最佳", confidence=0.93, evidence_ids=("ev-1",),
        now="2026-10-16T00:00:00Z")
    claim = store.read_page("clm-1")
    assert claim.type == "claim" and claim.confidence == 0.93
    assert claim.extra["evidence_ids"] == ["ev-1"]
    assert claim.status == "TRUE"                            # U17 默认状态


def test_wiki_page_scaffold_respects_vault_isolation(settings, workdir):
    """脚手架不得成为绕过隔离的后门：写入 A 的 Vault，B 必须看不到。"""
    store = WikiStore(workdir / "wiki", settings=settings)

    scaffold.scaffold_wiki_entity(store, entity_id="v-1", name="偏好",
                                  entity_type="用户偏好", user_id="user-A")
    scaffold.scaffold_wiki_claim(
        store, claim_id="v-claim", subject_id="v-1", object_id="season:spring",
        description="本人偏好春季", confidence=0.9, evidence_ids=("ev-1",), user_id="user-A")

    assert store.read_page("v-1", user_id="user-A") is not None
    assert store.read_page("v-claim", user_id="user-A") is not None
    assert store.read_page("v-1", user_id="user-B") is None
    assert store.read_page("v-claim", user_id="user-B") is None

    # 作用域只由 user_id 一个入口决定，脚手架无法制造「页面归属与目标库不一致」
    import inspect
    sig = inspect.signature(scaffold.scaffold_wiki_claim)
    assert list(sig.parameters).count("user_id") == 1
