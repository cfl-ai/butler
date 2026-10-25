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


def implemented_phase_agents(settings: Any = None, *,
                             harness_sandbox: Any = None,
                             harness_coder: Any = None,
                             harness_patch_root: str | None = None) -> list[Any]:
    """当前已落地的阶段 Agent 实例。每落地一个阶段就加到这里。

    P10 的三件套**必须由运行时注入**，不能在这里 new：

    - `harness_sandbox`：沙箱是 AI Infra 的能力（U34），本机只提供进程级适配器；
    - `harness_coder`：代码创作端口（U78）。**不给就不改代码**——这是刻意的默认，
      「自动改源码」是整条链路里风险最高的动作，不该因为装配顺手就默认打开；
    - `harness_patch_root`：补丁落盘根目录，须与沙箱 root 一致，否则
      「改的地方」和「测的地方」不是一个目录。
    """
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
        HarnessAgent(settings=settings, sandbox=harness_sandbox,
                     coder=harness_coder, patch_root=harness_patch_root),
    ]


def build_butler(*, settings: Any = None, data_sdk: Any = None,
                 search_service: Any = None, wiki_store: Any = None,
                 llm_gateway: Any = None, jev_client: Any = None,
                 payment_gateway: Any = None,
                 scheduler: Any = None,
                 harness_sandbox: Any = None,
                 harness_coder: Any = None,
                 harness_patch_root: str | None = None) -> ButlerAgent:
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
    butler.enroll_all(implemented_phase_agents(
        settings, harness_sandbox=harness_sandbox,
        harness_coder=harness_coder, harness_patch_root=harness_patch_root))
    if scheduler is not None:
        butler.attach_scheduler(scheduler)
    return butler
