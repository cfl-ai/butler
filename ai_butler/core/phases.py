"""阶段真值源 —— 每个阶段项目本身就是一项 Agent。

计划书依据
----------
- 计划书封面：「**管家 Agent 生态体系项目计划书**」
- 一、项目概述「业务层：**场景调度 Agent 全局唯一总指挥**」
- 三、P8「由**顶层管家**统一调度所有支付流程」
- 三、P0-1「输出统一架构规范文档，全项目强制执行」
- 六、全体系可复用资产库

设计口径
--------
生态体系由**管家 Agent 统一管辖**；每个阶段（P0-1、P0-2、P1…P10）各自是一项 Agent，
命名直接取自计划书该阶段的原文标题，不另造名字。
未实现的阶段**照样登记**，但 ~{implemented=False}~，绝不假装已具备。

可调整 / 可复用
--------------
改名只改本文件；新增垂类不新增阶段，复用既有阶段 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 管家 Agent 自身的标识（管辖者，不属于被管辖的阶段）
BUTLER_KEY = "butler_agent"
BUTLER_DISPLAY = "管家 Agent"

LAYER_BASE = "P0 基础设施层"
LAYER_BUSINESS = "业务层"
LAYER_EVOLUTION = "离线进化层"


@dataclass(frozen=True)
class PhaseSpec:
    code: str                 # 计划书里的阶段编号，如 "P1"
    title: str                # 计划书该阶段的原文标题（含副标题）
    key: str                  # 机读标识 / 注册表键
    display: str              # 人读名称
    layer: str                # 所属层
    core_capability: str      # 计划书该阶段「核心能力 / 建设目标」原句
    implemented: bool         # 是否已落地（未落地也登记，但不假装）
    note: str = ""            # 完成度说明：把「做了一半」如实讲清楚，避免非黑即白


#: 全部阶段，逐条对应计划书原文标题（顺序即计划书顺序）
PHASES: tuple[PhaseSpec, ...] = (
    PhaseSpec(
        code="P0-1", title="AI Infra 架构底座（全生态通用底层）",
        key="p0_ai_infra", display="P0-1 AI Infra 架构底座 Agent", layer=LAYER_BASE,
        core_capability="统一全项目底层架构、网络、容器、安全、网关、环境、沙箱",
        implemented=True),
    PhaseSpec(
        code="P0-2", title="AI 研发效能提升平台（开发 & 测试 & 迭代底座）",
        key="p0_devkit", display="P0-2 AI 研发效能提升平台 Agent", layer=LAYER_BASE,
        core_capability="标准化 Agent 开发、自动化测试、自动化评测、版本管理、Prompt 管理",
        implemented=True),
    PhaseSpec(
        code="P1", title="数据中台｜全系统唯一可信数据源",
        key="p1_data_platform", display="P1 数据中台 Agent", layer=LAYER_BUSINESS,
        core_capability="结构化存储、增量学习、权重衰减、冲突校验、版本溯源",
        implemented=True),
    PhaseSpec(
        code="P2", title="素材中心 RAG 知识库｜事实依据底层",
        key="p2_material_center", display="P2 素材中心 RAG 知识库 Agent", layer=LAYER_BUSINESS,
        core_capability="自动化采集、清洗、混合检索、时效更新、素材溯源、防幻觉支撑",
        implemented=True),
    PhaseSpec(
        code="P3", title="场景调度 Agent｜业务全局总指挥",
        key="p3_scene_scheduler", display="P3 场景调度 Agent", layer=LAYER_BUSINESS,
        core_capability="全局状态管理、任务分发、工具管控、循环熔断、资源节流",
        implemented=True),
    PhaseSpec(
        code="P4", title="Mops 多智能体集群｜感知与基础执行单元",
        key="p4_mops", display="P4 Mops 多智能体集群 Agent", layer=LAYER_BUSINESS,
        core_capability="设备感知、天气感知、时间进度、增量抽取、路线方案、备选预案、代码迭代",
        implemented=True),
    PhaseSpec(
        code="P5", title="动态事件运营 Agent｜核心智能方案生成引擎",
        key="p5_dynamic_event", display="P5 动态事件运营 Agent", layer=LAYER_BUSINESS,
        core_capability="多约束推理、动态重规划、流式输出、场景自适应",
        implemented=True),
    PhaseSpec(
        code="P6", title="内容质检 Agent｜全局防错、防幻、防假",
        key="p6_content_qa", display="P6 内容质检 Agent", layer=LAYER_BUSINESS,
        core_capability="事实校验、冲突检测、幻觉拦截、账单核验、人工审核兜底",
        implemented=True),
    PhaseSpec(
        code="P7", title="语音交互移动端系统｜端云一体交互入口",
        key="p7_mobile_voice", display="P7 语音交互移动端 Agent", layer=LAYER_BUSINESS,
        core_capability="离线唤醒、随时打断、双输入、流式文字、实时 TTS、设备采集、本地归档",
        # 如实标注：框架与业务编排已完成并经验证，但**端侧语音推理未接入**，
        # 而「离线唤醒 / 实时 TTS」正是 P7 的核心能力，缺了就不能算落地。
        implemented=False,
        note="移动端工程与纯 Dart 业务核心已完成并通过 dart test（36 项）；"
             "端侧语音**从引擎到随包能力已全部就位**：Python 侧 4/4 检出、Dart 侧 4/4 检出；"
             "APK 三 ABI release + debug 均构建成功，内含 sherpa-onnx 原生栈、"
             "清单 9 条权限（含 RECORD_AUDIO）、foregroundServiceType=microphone 的常驻唤醒服务、"
             "随包的唤醒词模型（U06，4.82MB）与真实录音；以上均由 "
             "scripts/verify_android_artifact.py 机检（含第三方商标检查）；"
             "**仍未完成的只有运行时层**（裁定 U23）：用户是否真的授权、"
             "前台服务在 Android 14+ 能否长期存活、实际功耗、兜里能否稳定唤醒；"
             "模拟器不可用已实测（WHPX 未启用需重启、可用内存仅 2.25GB），故不以此替代真机"),
    PhaseSpec(
        code="P8", title="智能支付 Agent｜全生态统一预算与结算体系",
        key="p8_payment", display="P8 智能支付 Agent", layer=LAYER_BUSINESS,
        core_capability="三档智能预算、账单聚合、免密支付、人工确认、一键批量结算、消费画像回写",
        implemented=True),
    PhaseSpec(
        code="P9", title="指标评估与增量微调体系｜持续优化体系",
        key="p9_evaluation", display="P9 指标评估与增量微调 Agent", layer=LAYER_BUSINESS,
        core_capability="全维度指标监控看板、日志沉淀与案例分类、领域 SFT 数据集、周期性增量微调",
        implemented=True),
    PhaseSpec(
        code="P10", title="Harness 自进化闭环",
        key="p10_harness", display="P10 Harness 自进化 Agent", layer=LAYER_EVOLUTION,
        core_capability="全自动查错、自检、改代码、自测、迭代升级，与业务链路完全隔离",
        implemented=True),
)

PHASE_BY_CODE: dict[str, PhaseSpec] = {p.code: p for p in PHASES}
PHASE_BY_KEY: dict[str, PhaseSpec] = {p.key: p for p in PHASES}


def phase(code: str) -> PhaseSpec:
    try:
        return PHASE_BY_CODE[code]
    except KeyError as exc:
        known = [p.code for p in PHASES]
        raise KeyError(f"未知阶段 {code!r}，可选 {known}") from exc


def agent_key(code: str) -> str:
    """阶段 → 注册表键（计划书标题派生）。"""
    return phase(code).key


def implemented_phases() -> tuple[PhaseSpec, ...]:
    return tuple(p for p in PHASES if p.implemented)


def planned_phases() -> tuple[PhaseSpec, ...]:
    """尚未落地的阶段。**必须显式暴露**，不得混入已实现。"""
    return tuple(p for p in PHASES if not p.implemented)


def describe() -> list[dict[str, object]]:
    return [{"code": p.code, "title": p.title, "agent": p.key, "display": p.display,
             "layer": p.layer, "implemented": p.implemented, "note": p.note}
            for p in PHASES]
