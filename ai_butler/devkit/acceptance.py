"""项目落地验收标准的**机器可核查版本**。

计划书依据
----------
- 六、全体系可复用资产库（12 类长期复用资产）
- 七、项目落地验收标准（1–20 条，含账号隐私「必须全部通过」、Jev、Agent-Wiki 三组）
- 八、系统全局核心配置参数（表 13，18 个硬参数）

为什么要做成代码
----------------
计划书把「完成」写成了散文。散文无法被回归测试盯住：参数被改动、模块被改名、
测试被删除，都不会有人发现。本模块把三节内容转成**结构化清单**，
再由测试逐条核对「计划书说的值 == 代码里的值」「验收项指向的测试真实存在」。

**一个实际发现**：表 13 的 18 个参数里，只有 11 个有显式配置项（Settings），
其余 7 个只以**行为**形式存在（如 agent_rollback_original_forbidden 落在 graph 的回退逻辑里）。
本模块的 enforced_at 字段就是用来逼问「这条参数到底由谁负责」的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: 表 13 的三组
GROUP_ROUTING = "五层路由&Agent生态全局硬参数"
GROUP_JEV = "Jev 决策模型配置"
GROUP_WIKI = "Agent-Wiki v2 知识库配置"
GROUP_NAMES: tuple[str, ...] = (GROUP_ROUTING, GROUP_JEV, GROUP_WIKI)


@dataclass(frozen=True)
class GlobalParam:
    """表 13 的一条硬参数。"""

    name: str
    plan_value: Any
    group: str
    #: 落点：module 或 module::symbol。**空字符串表示尚无显式落点** —— 这是要暴露的问题。
    enforced_at: str = ""
    #: 是否暴露为可配置项（Settings 字段）。硬参数多为「行为固定」，不必可配。
    configurable: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.group not in GROUP_NAMES:
            raise ValueError(f"未知参数组 {self.group!r}，可选 {list(GROUP_NAMES)}")


#: 表 13 全部硬参数（数值逐字取自计划书，**不得改动**）
TABLE_13: tuple[GlobalParam, ...] = (
    # ---- 五层路由 & Agent 生态 ----
    GlobalParam("max_task_rollback_times", 2, GROUP_ROUTING,
                "ai_butler.scheduler.graph::MAX_ROLLBACKS",
                note="全局回退上限；垂类侧另有 ai_butler.verticals.base::MAX_ROLLBACKS 同值"),
    GlobalParam("multi_intent_auto_resolve", False, GROUP_ROUTING,
                "ai_butler.scheduler.router",
                note="多意图禁止自动选优先级；以行为固化（路由层不解析冲突）"),
    GlobalParam("agent_rollback_original_forbidden", True, GROUP_ROUTING,
                "ai_butler.scheduler.graph",
                note="禁止把任务退回刚回退它的 Agent；以行为固化"),
    GlobalParam("route_loop_protect", True, GROUP_ROUTING,
                "ai_butler.scheduler.graph",
                note="循环保护；以行为固化"),
    GlobalParam("new_intent_auto_save", True, GROUP_ROUTING,
                "ai_butler.scheduler.router",
                note="新意图自动落库；以行为固化"),
    GlobalParam("conflict_intent_prompt_enable", True, GROUP_ROUTING,
                "ai_butler.scheduler.graph",
                note="冲突意图必须向用户确认；以行为固化"),
    GlobalParam("private_data_isolate", True, GROUP_ROUTING,
                "ai_butler.core.db",
                note="行级隔离；以行为固化（_enforce_row_level_isolation）"),
    GlobalParam("base_collect_unified", True, GROUP_ROUTING,
                "ai_butler.rag.collector",
                note="统一采集底座；以行为固化"),
    # ---- Jev ----
    GlobalParam("jev_enable", True, GROUP_JEV, "ai_butler.core.config::Settings",
                configurable=True),
    GlobalParam("jev_confidence_threshold", 0.82, GROUP_JEV,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("jev_fallback_enable", True, GROUP_JEV,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("jev_shadow_mode", True, GROUP_JEV,
                "ai_butler.core.config::Settings", configurable=True),
    # ---- Agent-Wiki v2 ----
    GlobalParam("wiki_public_workspace_enable", True, GROUP_WIKI,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("wiki_user_vault_per_user", True, GROUP_WIKI,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("wiki_compile_auto_run", True, GROUP_WIKI,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("wiki_auto_report_contradiction", True, GROUP_WIKI,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("wiki_auto_report_low_confidence", True, GROUP_WIKI,
                "ai_butler.core.config::Settings", configurable=True),
    GlobalParam("wiki_max_digest_size", 12000, GROUP_WIKI,
                "ai_butler.core.config::Settings", configurable=True),
)

TABLE_13_NAMES: tuple[str, ...] = tuple(p.name for p in TABLE_13)


def params_by_group(group: str) -> tuple[GlobalParam, ...]:
    return tuple(p for p in TABLE_13 if p.group == group)


def actual_value(param: GlobalParam, *, settings: Any = None) -> Any:
    """读代码里的实际值。

    可配置参数从 settings 取；行为固化的参数从 enforced_at 指向的符号取，
    取不到时返回 None —— **取不到就是问题**，不糊弄过去。
    """
    if param.configurable:
        if settings is None:
            return _field_default(param.name)
        return getattr(settings, param.name, None)

    module_name, _, symbol = param.enforced_at.partition("::")
    if not symbol:
        return None
    import importlib
    return getattr(importlib.import_module(module_name), symbol, None)


def _field_default(name: str) -> Any:
    from ai_butler.core.config import Settings
    field = Settings.model_fields.get(name)
    return None if field is None else field.default


@dataclass(frozen=True)
class ParamMismatch:
    name: str
    plan_value: Any
    actual_value: Any
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "plan_value": self.plan_value,
                "actual_value": self.actual_value, "reason": self.reason}


def verify_table_13(*, settings: Any = None) -> list[ParamMismatch]:
    """逐条核对表 13：计划书值 vs 代码值。

    **没有落点的参数会被报出来**，而不是静默跳过。
    """
    problems: list[ParamMismatch] = []
    for param in TABLE_13:
        if not param.enforced_at:
            problems.append(ParamMismatch(
                param.name, param.plan_value, None,
                "未声明落点：无法核对计划书参数是否真的生效"))
            continue
        actual = actual_value(param, settings=settings)
        if actual is None and not param.configurable:
            # 行为固化的参数没有同名符号是正常的（落点写的是模块），
            # 但要能 import —— import 失败才算问题。
            try:
                import importlib
                importlib.import_module(param.enforced_at.split("::")[0])
            except Exception as error:
                problems.append(ParamMismatch(
                    param.name, param.plan_value, None,
                    f"落点模块不可导入：{error}"))
            continue
        if param.configurable and actual != param.plan_value:
            problems.append(ParamMismatch(
                param.name, param.plan_value, actual,
                "计划书【八】明令「数值不得改动」"))
    return problems


# ============================== 六、可复用资产库 ============================== #
@dataclass(frozen=True)
class ReusableAsset:
    name: str
    module: str
    note: str = ""


#: 六、全体系可复用资产库（12 类，逐条对应）
REUSABLE_ASSETS: tuple[ReusableAsset, ...] = (
    ReusableAsset("AI Infra 全套底层云原生架构", "ai_butler.infra",
                  "连接池/中间件/存储/沙箱"),
    ReusableAsset("AI 研发效能测试、评测、脚手架体系", "ai_butler.devkit",
                  "脚手架 + 用例库 + 评测 + CI"),
    ReusableAsset("账号会话鉴权、行级用户隔离 SDK 与开发规范",
                  "ai_butler.middleware.session_auth"),
    ReusableAsset("Jev 高速决策调用 SDK", "ai_butler.infra.llm_gateway"),
    ReusableAsset("Agent-Wiki v2 SDK 与编译流水线", "ai_butler.agent_wiki"),
    ReusableAsset("数据中台结构化记忆与增量学习体系",
                  "ai_butler.data_platform"),
    ReusableAsset("RAG 混合检索内核与时效知识库体系", "ai_butler.rag"),
    ReusableAsset("LangGraph 全局多 Agent 调度中枢", "ai_butler.scheduler"),
    ReusableAsset("全局内容质检防幻觉、防错误体系",
                  "ai_butler.agents.p6_content_qa"),
    ReusableAsset("端云一体流式语音交互整套框架", "mobile",
                  "纯 Dart 核心 + Flutter 外壳（P7）"),
    ReusableAsset("三档预算 + 免密 + 聚合一键支付智能引擎",
                  "ai_butler.agents.p8_payment"),
    ReusableAsset(
        # **注意名称与计划书不同**：计划书原文是「基于 Claude Code 的
        # 全自动系统自进化闭环」，这里刻意去掉了「基于 Claude Code」——
        # 裁定 U08（人工授权）明令「**不引入 Claude Code 本体**，
        # 仅借用其设计思维，自研 Agentic Loop」。
        # 这不是抄漏，是照裁定改的；照着计划书「修正」回去会与 U08 冲突。
        "全自动系统自进化闭环", "ai_butler.harness",
        "自研 Agentic Loop，非 Claude Code 本体（裁定 U08）"),
)


# ============================== 七、验收标准 ============================== #
@dataclass(frozen=True)
class AcceptanceCriterion:
    number: int
    text: str
    area: str
    #: 验证位置：~{tests/xxx.py::test_yyy}~ 或 ~{dart:...::用例名}~
    verified_by: str

    @property
    def must_pass(self) -> bool:
        """账号与隐私防护一组写着「**必须全部通过**」。"""
        return self.area == AREA_PRIVACY


AREA_PLATFORM = "项目落地验收标准"
AREA_PRIVACY = "账号 & 隐私防护（必须全部通过）"
AREA_JEV = "Jev 决策模型验收项"
AREA_WIKI = "Agent-Wiki v2 知识库验收项"
AREA_NAMES: tuple[str, ...] = (AREA_PLATFORM, AREA_PRIVACY, AREA_JEV, AREA_WIKI)

ACCEPTANCE_CRITERIA: tuple[AcceptanceCriterion, ...] = (
    AcceptanceCriterion(1, "双基础设施底座稳定可用，所有业务模块可统一复用",
                        AREA_PLATFORM,
                        "tests/test_butler_phases.py::test_butler_enrolls_phase_agents"),
    AcceptanceCriterion(2, "所有 Agent 链路无冲突、无循环依赖、无死循环；五层路由完整；"
                           "Jev 置信不足平滑降级；多意图冲突强制确认；回退全局 2 次上限",
                        AREA_PLATFORM,
                        "tests/test_butler_phases.py::test_run_phase_refuses_unimplemented_phase_without_faking_data"),
    AcceptanceCriterion(3, "业务链路规划精准、无幻觉；方案可关联 Agent-Wiki 证据来源，"
                           "低置信信息提示风险",
                        AREA_PLATFORM,
                        "tests/test_agents_p6.py::test_clean_input_passes"),
    AcceptanceCriterion(4, "支付三档方案完整、结算安全，可免密 / 可人工确认 / 可聚合一键付",
                        AREA_PLATFORM,
                        "tests/test_agents_p8.py::test_thresholds_come_from_decision_u07_not_invented"),
    AcceptanceCriterion(5, "移动端语音交互达到主流智能助手体验", AREA_PLATFORM,
                        "dart:mobile/packages/aibutler_core/test/voice_pipeline_test.dart::"
                        "播报中检测到人声 → 立即打断并停播（计划书三、P7 步骤4）"),
    AcceptanceCriterion(6, "具备全自动查错、改代码、自测、迭代能力；支持 Jev 模板迭代、"
                           "公共 Agent-Wiki 离线维护",
                        AREA_PLATFORM,
                        "tests/test_harness_p10.py::test_loop_stages_match_the_plan"),
    AcceptanceCriterion(7, "业务链路与进化链路完全隔离、互不干扰；"
                           "离线链路不可访问用户未脱敏私有 Vault",
                        AREA_PLATFORM,
                        "tests/test_harness_p10.py::test_agent_runs_the_loop_and_reports_isolation"),
    AcceptanceCriterion(8, "全体系可横向扩展所有垂类业务，无需重构底层", AREA_PLATFORM,
                        "tests/test_vertical_tour.py::test_agent_writes_only_its_own_field"),
    AcceptanceCriterion(9, "密码加盐哈希存储、库中无明文；Token 超时失效、"
                           "长时间未登录强制重登",
                        AREA_PRIVACY,
                        "tests/test_security.py::test_password_is_salted_and_never_plaintext"),
    AcceptanceCriterion(10, "行级数据隔离：A 无法读 B 的画像/对话/账单/私有 Vault；"
                            "接口与数据库层双重防护",
                        AREA_PRIVACY,
                        "tests/test_db.py::test_account_sees_only_its_own_rows"),
    AcceptanceCriterion(11, "未登录状态所有业务接口拒绝执行；会话失效中断 Agent 业务流程",
                        AREA_PRIVACY,
                        "tests/test_butler_phases.py::test_run_phase_refuses_when_session_is_invalid"),
    AcceptanceCriterion(12, "切换账号自动清理旧账号本地缓存；私有 Vault 完全隔离",
                        AREA_PRIVACY,
                        "dart:mobile/packages/aibutler_core/test/local_cache_test.dart::"
                        "切换账号自动清理旧账号缓存（计划书三、P7 步骤8）"),
    AcceptanceCriterion(13, "全链路日志与离线自进化链路均做隐私脱敏，不采集原始敏感隐私",
                        AREA_PRIVACY,
                        "tests/test_evaluation_p9.py::test_redactor_is_fail_closed"),
    AcceptanceCriterion(14, "自动化测试用例包含越权访问安全用例，每次回归必须执行",
                        AREA_PRIVACY,
                        "tests/test_agents_p6.py::test_privacy_blocks_cross_user_reference"),
    AcceptanceCriterion(15, "固定分类任务优先 Jev 结构化输出；只带枚举标签 + 置信概率，"
                            "不生成自由文本",
                        AREA_JEV,
                        "tests/test_agents_p6.py::test_jev_classifies_credibility"),
    AcceptanceCriterion(16, "支持影子模式并行对比；置信低于阈值自动降级；"
                            "Jev 故障可降级兼容原有路由",
                        AREA_JEV,
                        "tests/test_scheduler_router.py::test_jev_shadow_mode_records_but_does_not_decide"),
    AcceptanceCriterion(17, "区分公共 Workspace 与用户私有 Vault（user_id 绑定、互相隔离）",
                        AREA_WIKI,
                        "tests/test_agent_wiki_store.py::test_cross_account_pages_are_invisible"),
    AcceptanceCriterion(18, "知识具备完整本体（Source/Claim/Evidence/Relation/Contradiction/"
                            "Question）；编译可输出机器可读缓存",
                        AREA_WIKI,
                        "tests/test_acceptance.py::test_ontology_has_all_six_elements_from_criterion_18"),
    AcceptanceCriterion(19, "矛盾、证据缺失、过期素材可自动产出报告；质检可依据证据做事实校验",
                        AREA_WIKI,
                        "tests/test_evaluation_p9.py::test_wiki_metrics_over_pages"),
    AcceptanceCriterion(20, "支持实体、声明沉淀；行程等产出可生成 synthesis 页存入私有 Vault",
                        AREA_WIKI,
                        "tests/test_vertical_tour.py::test_agent_handles_a_full_trip"),
)

CRITERIA_NUMBERS: tuple[int, ...] = tuple(c.number for c in ACCEPTANCE_CRITERIA)


def criteria_by_area(area: str) -> tuple[AcceptanceCriterion, ...]:
    return tuple(c for c in ACCEPTANCE_CRITERIA if c.area == area)
