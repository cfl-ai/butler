"""管家 Agent 的默认装配。

计划书依据
----------
- 三、P3「绑定所有 Skill 工具、子 Agent、业务流程统一入口」
- 计划书封面「管家 Agent 生态体系项目计划书」

只登记**已落地**的阶段 Agent；未落地阶段由 ButlerAgent.ledger() 以
implemented=False 显式列出，不注册空壳实例。
"""

from __future__ import annotations

from typing import Any, Mapping

from ai_butler.agents.butler import ButlerAgent
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_JEV,
    CAP_LLM,
    CAP_PAYMENT,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
    CAP_WIKI_WRITE,
)
from ai_butler.agents.p0_ai_infra import InfraAgent
from ai_butler.agents.p0_devkit import DevkitAgent
from ai_butler.agents.p1_data_platform import DataPlatformAgent
from ai_butler.agents.p2_material_center import MaterialCenterAgent
from ai_butler.agents.p3_scene_scheduler import SceneSchedulerAgent
from ai_butler.agents.p4_mops import MopsClusterAgent
from ai_butler.agents.p5_dynamic_event_operation import DynamicEventAgent
from ai_butler.agents.p6_content_qa import ContentQaAgent
from ai_butler.agents.p8_smart_payment import PaymentAgent
from ai_butler.agents.p9_evaluation import EvaluationAgent
from ai_butler.agents.p10_harness import HarnessAgent


def implemented_phase_agents(settings: Any = None) -> list[Any]:
    """当前已落地的阶段 Agent 实例。每落地一个阶段就加到这里。"""
    return [
        InfraAgent(settings=settings),
        DevkitAgent(settings=settings),
        DataPlatformAgent(settings=settings),
        MaterialCenterAgent(settings=settings),
        SceneSchedulerAgent(settings=settings),
        MopsClusterAgent(settings=settings),
        DynamicEventAgent(settings=settings),
        ContentQaAgent(settings=settings),
        PaymentAgent(settings=settings),
        EvaluationAgent(settings=settings),
        HarnessAgent(settings=settings),
    ]


def build_butler(*, settings: Any = None, data_sdk: Any = None,
                 search_service: Any = None, wiki_store: Any = None,
                 llm_gateway: Any = None, jev_client: Any = None,
                 payment_gateway: Any = None,
                 scheduler: Any = None) -> ButlerAgent:
    """按当前已落地的阶段装配管家 Agent。缺失的能力不装——阶段 Agent 取用时才会报错，
    这样「能力没接」不会被误当成「阶段没做」。"""
    butler = ButlerAgent(settings=settings)
    for capability, impl in ((CAP_DATA_READ, data_sdk),
                             (CAP_RETRIEVAL, search_service),
                             (CAP_WIKI_READ, wiki_store),
                             (CAP_WIKI_WRITE, wiki_store),
                             (CAP_LLM, llm_gateway),
                             (CAP_JEV, jev_client),
                             (CAP_PAYMENT, payment_gateway)):
        if impl is not None:
            butler.provide(capability, impl)
    butler.enroll_all(implemented_phase_agents(settings))
    if scheduler is not None:
        butler.attach_scheduler(scheduler)
    return butler
