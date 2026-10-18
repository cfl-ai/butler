"""管家 Agent 与阶段 Agent 测试。

计划书依据：封面「管家 Agent 生态体系项目计划书」+ 一、项目概述「场景调度 Agent 全局唯一总指挥」
+ 三、P3「统一入口」+ 三、P8「顶层管家」+ 三、P4「无跨模块越权调用」
"""

from __future__ import annotations

import pytest

from ai_butler.agents.base import SubAgent
from ai_butler.core import phases as phases_mod
from ai_butler.agents.butler import ButlerAgent, ButlerError, ButlerResult
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CapabilityError,
)
from ai_butler.agents.p0_ai_infra import InfraAgent
from ai_butler.agents.p0_devkit import DevkitAgent
from ai_butler.agents.p1_data_platform import DataPlatformAgent
from ai_butler.agents.p2_material_center import MaterialCenterAgent
from ai_butler.agents.p3_scene_scheduler import SceneSchedulerAgent
from ai_butler.agents.phase import PhaseAgent, PhaseNotImplemented
from ai_butler.agents.phases_bootstrap import build_butler, implemented_phase_agents
from ai_butler.agents.state import new_state
from ai_butler.core import phases
from ai_butler.core.phases import BUTLER_KEY, PHASES


def _call(agent, state):
    """统一调用：Agent 的 run 可能是同步或异步（见 agents/base.resolve）。"""
    import asyncio

    from ai_butler.agents.base import resolve

    return asyncio.run(resolve(agent(state)))


def _run_phase(butler, code, state):
    """阶段 Agent 的 run 可能是同步或异步，run_phase 已统一为异步入口。"""
    import asyncio

    return asyncio.run(butler.run_phase(code, state))


# ============================== 真值源 ============================== #
def test_every_plan_phase_is_registered_with_its_original_title():
    """每个阶段都是一项 Agent，命名与标题取自计划书原文。"""
    codes = [p.code for p in PHASES]
    assert codes == ["P0-1", "P0-2", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8",
                     "P9", "P10"]
    by_code = {p.code: p for p in PHASES}
    assert by_code["P1"].title == "数据中台｜全系统唯一可信数据源"
    assert by_code["P3"].title == "场景调度 Agent｜业务全局总指挥"
    assert by_code["P4"].title == "Mops 多智能体集群｜感知与基础执行单元"
    assert by_code["P10"].title == "Harness 自进化闭环"
    assert all(p.core_capability for p in PHASES)


def test_phase_keys_are_unique_and_lookupable():
    keys = [p.key for p in PHASES]
    assert len(keys) == len(set(keys))
    assert phases.phase("P2").key == "p2_material_center"
    assert phases.agent_key("P3") == "p3_scene_scheduler"
    with pytest.raises(KeyError):
        phases.phase("P99")


def test_implemented_and_planned_partition_is_exhaustive():
    done = set(p.code for p in phases.implemented_phases())
    todo = set(p.code for p in phases.planned_phases())
    assert done | todo == set(p.code for p in PHASES)
    assert done & todo == set()
    # 这一处**故意写死**：阶段状态一旦变化，必须有人有意识地确认并更新本行，
    # 而不是让测试自动跟随、悄悄把「新落地」当成理所当然。
    assert done == {"P0-1", "P0-2", "P1", "P2", "P3", "P4", "P5", "P6", "P8", "P9", "P10"}, \
        "已落地集合变化须同步本测试"


# ============================== 阶段 Agent ============================== #
@pytest.mark.parametrize("cls,code", [
    (InfraAgent, "P0-1"), (DevkitAgent, "P0-2"), (DataPlatformAgent, "P1"),
    (MaterialCenterAgent, "P2"), (SceneSchedulerAgent, "P3"),
])
def test_phase_agent_metadata_comes_from_the_truth_source(cls, code):
    spec = phases.phase(code)
    assert cls.name == spec.key, "注册名必须等于真值源的 agent_key"
    assert cls.phase_code == code
    assert cls.writes, "阶段 Agent 必须声明自有写入字段"
    assert spec.display in cls.responsibilities
    manifest = cls.manifest()
    assert manifest["implemented"] is True and manifest["code"] == code


def test_unknown_phase_code_is_rejected_at_class_definition():
    with pytest.raises(KeyError):
        class _Ghost(PhaseAgent):
            phase_code = "P42"
            writes = ("x",)

            def run(self, state):
                return {}


# ============================== 管家：登记 ============================== #
def test_butler_enrolls_phase_agents():
    butler = ButlerAgent()
    specs = butler.enroll_all(implemented_phase_agents())
    expected = [p.key for p in phases.implemented_phases()]
    assert [s.name for s in specs] == expected
    assert [a.phase_code for a in butler.phase_agents()] == \
        [p.code for p in phases.implemented_phases()]


def test_phase_agent_name_cannot_drift_at_definition_time():
    """命名由真值源**强制**：子类即便把 name 写错也会被覆盖。

    这比「注册时校验」更强——非法状态压根不可表达。
    """
    class _Renamed(PhaseAgent):
        phase_code = "P1"                 # 真值源要求叫 p1_data_platform
        name = "data_platform"            # 有意写错
        writes = ("p1_digest",)

        def run(self, state):
            return {}

    assert _Renamed.name == "p1_data_platform"


def test_butler_still_rejects_a_name_mutated_after_definition():
    """纵深防御：类级强制被绕过后（实例改名），登记时仍会拦下。"""
    agent = DataPlatformAgent()
    agent.name = "data_platform"
    with pytest.raises(ButlerError) as ei:
        ButlerAgent().enroll(agent)
    assert "计划书真值源不一致" in str(ei.value)


def test_butler_rejects_non_phase_agents():
    """业务子 Agent 归 P3 管辖，不得混进阶段名册。"""
    class _Business(SubAgent):
        name = "hotel_agent"
        writes = ("hotel",)

        def run(self, state):
            return {"hotel": 1}

    with pytest.raises(ButlerError) as ei:
        ButlerAgent().enroll(_Business())
    assert "不是阶段 Agent" in str(ei.value)


def test_butler_enforces_duplicate_phase_registration():
    butler = ButlerAgent()
    butler.enroll(DataPlatformAgent())
    with pytest.raises(Exception):
        butler.enroll(DataPlatformAgent())


# ============================== 管家：台账与管控 ============================== #
def test_ledger_lists_planned_phases_instead_of_hiding_them():
    """未落地的阶段照样列出，implemented=False —— 不允许出现假象。"""
    butler = build_butler()
    ledger = butler.ledger()
    assert len(ledger) == len(PHASES) == 12
    assert [r["code"] for r in ledger] == [p.code for p in PHASES]
    planned = [r for r in ledger if not r["implemented"]]
    assert {r["code"] for r in planned} == {p.code for p in phases.planned_phases()}
    assert planned, "还应有未落地阶段——全部落地时本用例要改写，不能静默变成空断言"
    assert all(r["enrolled"] is False for r in planned)
    assert all(r["responsibilities"] for r in ledger)


def test_coverage_reports_both_done_and_planned():
    coverage = build_butler().coverage()
    assert coverage["butler"] == BUTLER_KEY
    assert coverage["phases_total"] == len(PHASES)
    assert coverage["phases_enrolled"] == coverage["phases_implemented"]
    assert coverage["planned"] == [p.code for p in phases.planned_phases()]


def test_govern_blocks_requests_without_identity_or_session():
    butler = build_butler()
    no_user = butler.govern({"session_id": "s", "login_status": True, "user_id": ""})
    assert no_user.granted is False and no_user.code == "NO_USER_ID"

    expired = butler.govern({"user_id": "u-1", "session_id": "s", "login_status": False})
    assert expired.granted is False and expired.code == ButlerAgent.LOGIN_REQUIRED
    assert "会话失效" in expired.detail

    ok = butler.govern({"user_id": "u-1", "session_id": "s", "login_status": True})
    assert ok.granted is True


def test_run_phase_refuses_unimplemented_phase_without_faking_data():
    """调未落地阶段必须抛错，且把计划书要求的能力写清楚，绝不返回空壳数据。"""
    butler = build_butler()
    state = new_state(user_id="user-A", session_id="s1")
    # 目标阶段**从真值源取**，不再写死——否则每落地一个阶段这里都要改一次
    pending = phases_mod.planned_phases()
    assert pending, "已无未落地阶段，本项检查需改为「全部阶段均可放行」的验证"
    target = pending[0]
    with pytest.raises(PhaseNotImplemented) as ei:
        _run_phase(butler, target.code, state)   # noqa: PT012 — 断言在 with 之外

    message = str(ei.value)
    assert target.title in message, "报错要指出是哪个阶段"
    assert target.core_capability in message, "报错里要带上计划书该阶段的核心能力"


def test_run_phase_refuses_when_session_is_invalid():
    butler = build_butler()
    state = new_state(user_id="user-A", session_id="s1", login_status=False)
    result = _run_phase(butler, "P1", state)
    assert result.granted is False and result.code == ButlerAgent.LOGIN_REQUIRED


# ============================== 管家：能力治理 ============================== #
def test_tools_are_clipped_to_each_phases_declared_capabilities():
    """计划书三、P4「无跨模块越权调用」：阶段 Agent 只拿到自己声明的能力。"""
    sentinel = object()
    butler = build_butler(data_sdk=sentinel, search_service=sentinel)
    p1_tools = butler.tools_of("P1")
    assert p1_tools.has(CAP_DATA_READ) is True
    assert p1_tools.has(CAP_RETRIEVAL) is False
    with pytest.raises(CapabilityError):
        p1_tools.require(CAP_RETRIEVAL)

    p2_tools = butler.tools_of("P2")
    assert p2_tools.has(CAP_RETRIEVAL) is True
    assert p2_tools.has(CAP_DATA_READ) is False

    p3_tools = butler.tools_of("P3")
    assert p3_tools.granted == (), "P3 阶段视图不直接持有外部能力"


def test_tools_of_unimplemented_phase_is_refused():
    """取「当前第一个未落地阶段」，这样用例不会因阶段推进而过期。"""
    target = phases.planned_phases()[0]
    with pytest.raises(PhaseNotImplemented):
        build_butler().tools_of(target.code)


def test_provide_rejects_unknown_capability():
    with pytest.raises(ButlerError):
        build_butler().provide("telepathy", object())


# ============================== 阶段 Agent 实跑 ============================== #
class _StubSDK:
    async def read_profile(self, *, user_id, limit=200):
        return [{"attribute": "偏好出行"}]

    async def read_knowledge(self, *, user_id=None, limit=200):
        return [{"subject": "西湖"}, {"subject": "黄山"}]


def test_p1_agent_reports_the_accounts_data_footprint():
    butler = build_butler(data_sdk=_StubSDK())
    result = _run_phase(butler, "P1", new_state(user_id="user-A", session_id="s1"))
    digest = result.payload["p1_digest"]
    assert digest["user_id"] == "user-A"
    assert digest["profile_rows"] == 1 and digest["knowledge_rows"] == 2
    assert digest["readonly"] is True, "计划书三、P1「所有 Agent 只读，禁止直写库」"


class _StubHit:
    def __init__(self, evidence):
        self.evidence_ids = evidence


class _StubResponse:
    def __init__(self, hits):
        self.hits = hits
        self.count = len(hits)
        self.searched_scopes = ["public", "vault:user-A"]
        self.reranked = True


class _StubSearch:
    def search(self, query, *, user_id, top_k=None):
        return _StubResponse([_StubHit(["ev-1"]), _StubHit([])])


def test_p2_agent_reports_retrieval_scopes_and_evidence_coverage():
    butler = build_butler(search_service=_StubSearch())
    state = new_state(user_id="user-A", session_id="s1", utterance="西湖开放时间")
    digest = _run_phase(butler, "P2", state).payload["p2_digest"]
    assert digest["hits"] == 2
    assert digest["evidence_coverage"] == pytest.approx(0.5)
    assert digest["searched_scopes"] == ["public", "vault:user-A"]


def test_p3_agent_exposes_the_frozen_routing_and_rollback_facts():
    digest = _run_phase(build_butler(), "P3",
                        new_state(user_id="user-A", session_id="s1")).payload["p3_digest"]
    assert digest["layers"] == ["hard_rule", "jev", "vector", "vertical_model",
                                "llm_fallback"]
    assert digest["max_rollbacks"] == 2


def test_p0_agents_report_real_infrastructure_facts():
    butler = build_butler()
    infra = _run_phase(butler, "P0-1",
                            new_state(user_id="u", session_id="s")).payload["p0_infra_digest"]
    assert infra["environments_isolated"] == ["dev", "test", "staging", "prod"]
    assert infra["network_zones"] == ["edge", "internal", "sandbox"]

    devkit = _run_phase(butler, "P0-2",
                              new_state(user_id="u", session_id="s")).payload["p0_devkit_digest"]
    assert devkit["case_total"] >= 174
    assert devkit["case_pending"] == [], "用例库不应有待实现项"


def test_phase_agents_only_write_their_own_field():
    butler = build_butler(data_sdk=_StubSDK())
    result = _run_phase(butler, "P1", new_state(user_id="user-A", session_id="s1"))
    assert set(result.payload) <= set(DataPlatformAgent.writes)


# ============================== 统一入口 ============================== #
def test_handle_requires_an_attached_scheduler():
    import asyncio
    with pytest.raises(ButlerError):
        asyncio.run(build_butler().handle(user_id="u", session_id="s",
                                          utterance="x", labels=("a",)))


def test_handle_blocks_before_touching_the_scheduler():
    """会话失效时管家先拦下，调度器根本不该被调用。"""
    import asyncio

    called: list[str] = []

    class _SpyScheduler:
        async def run(self, **kwargs):
            called.append("run")
            raise AssertionError("不该被调用")

    butler = build_butler()
    butler.attach_scheduler(_SpyScheduler())
    result = asyncio.run(butler.handle(user_id="u", session_id="s", utterance="x",
                                       labels=("a",), login_status=False))
    assert result.granted is False and called == []


def test_snapshot_describes_the_butler():
    snap = build_butler().snapshot()
    assert snap["butler"] == BUTLER_KEY
    assert snap["scheduler_attached"] is False
    assert len(snap["enrolled_agents"]) == len(phases.implemented_phases())
    assert snap["coverage"]["phases_total"] == len(PHASES)
