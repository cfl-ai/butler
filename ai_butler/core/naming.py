"""命名规范 —— 全项目标识的单一真值源。

计划书依据
----------
- 文档标题：《AI管家Agent生态体系项目计划书》（封面：管家 Agent 生态体系项目计划书）
- 三、P0-1「输出统一架构规范文档，全项目强制执行」
- 六、全体系可复用资产库（长期生态复用）
- 五、模块定位「点餐、售后等其他垂类直接复制本模块结构，替换业务逻辑、工具集即可快速接入」

命名派生规则（命名根取自计划书标题）
----------------------------------
| 标识       | 取值                   | 派生自                         |
| ROOT       | ai-butler              | 标题「AI管家」                  |
| ECOSYSTEM  | ai-butler-ecosystem    | 标题「AI管家 Agent 生态体系」    |
| PACKAGE    | ai_butler              | ROOT 的下划线形式               |
| NAMESPACE  | aibutler               | 资源前缀：Redis 键、容器名       |
| ENV_PREFIX | AIBUTLER_              | 应用级环境变量前缀              |
| DB_NAME    | ai_butler              | 与包名一致                      |

层名同样取自计划书自身用语：core（底座）/ infra（AI Infra 架构底座）/ middleware（中间件）。

模块层级名（本轮新增）
----------------------
Python 的**包名与模块名只能用 ASCII**，所以目录叫 `data_platform`，
但人读的层级名必须就是计划书里那个项目名——「数据中台」，不叫「数据平台模块」。
两套名字必须一一对应且都可机检，否则文档写中文、代码写英文，
时间一长就会变成互相认不出的两套体系（MEMORY E-44 的另一种形态）。

故在此登记三张表，值**一律取自计划书原句**，并由守护测试逐条回查原文：

| 表 | 键 | 值 | 计划书出处 |
| MODULE_TITLES | 包目录名 | 该包的层级中文名 | 三、各阶段标题 / P0 级基础设施层 |
| SUBAGENT_TITLES | 子 Agent 机读 key | 计划书点名的中文名 | 三、P4「包含子 Agent：设备感知、天气感知…」 |
| CROSSCUT_TITLES | 横切 Agent 机读 key | 中文名 | 三、P3「智能规划」「质检校验」 |
| USER_AGENT_TITLES | **用户指令新增** Agent 的 key | 中文名 | **用户原话**（非计划书），须有对应人工授权裁定 |

USER_AGENT_TITLES 单独一张表，是为了**不冒充计划书用语**：
计划书没点名的东西混进 CROSSCUT_TITLES，时间一长就分不清哪些名字
有计划书依据、哪些是后加的。分开登记 + 守护测试强制对应人工授权裁定，
新增用户指令 Agent 时出处永远可查。

机读 key 保持英文不变：它是 registry 字典键、State 字段前缀、
agent_results 的索引，改中文会牵动全链路；中文名只做**展示层**。

可调整
------
后期改名只需改本文件或设置 AIBUTLER_ROOT / AIBUTLER_ECOSYSTEM / AIBUTLER_NAMESPACE，
业务代码不出现任何硬编码标识。

可复用
------
垂类资源统一走 vertical_key("travel", ...) / vertical_container("travel", ...)。
计划书五要求点餐、售后等垂类复用同一套结构，因此新增垂类**不新增命名规则**。
"""

from __future__ import annotations

import os

ROOT: str = os.environ.get("AIBUTLER_ROOT", "ai-butler")
ECOSYSTEM: str = os.environ.get("AIBUTLER_ECOSYSTEM", f"{ROOT}-ecosystem")
PACKAGE: str = "ai_butler"
NAMESPACE: str = os.environ.get("AIBUTLER_NAMESPACE", "aibutler").strip().lower()
ENV_PREFIX: str = "AIBUTLER_"
DB_NAME: str = "ai_butler"


# --------------------------------------------------------------------------- #
# 模块层级名
# --------------------------------------------------------------------------- #

#: 包目录 → 阶段编号。**只对确实存在独立包的阶段登记**；
#: 其余阶段（P4/P5/P6/P8 等）实现就在 agents/ 下的单文件里，不在此虚造一个包。
#: 中文名不写在这里，从 phases 真值源派生，避免同一名字存两份（E-44）。
PHASE_PACKAGES: dict[str, str] = {
    "infra": "P0-1",
    "devkit": "P0-2",
    "data_platform": "P1",
    "material_center": "P2",
    "scheduler": "P3",
    "evaluation": "P9",
    "harness": "P10",
}

#: 非阶段包的层级中文名。值取自计划书原句，由守护测试回查原文。
#: - core：三、P0-1「统一全项目底层架构…所有业务模块统一复用」→ 底座
#: - middleware：三、P0-1「开发全局中间件」「会话管理中间件」→ 中间件
#: - agents：三、P4「Mops 多智能体集群」→ 阶段 Agent 层
#: - verticals：五「旅游 Agent 属于下垂类业务执行单元」→ 垂类模块
MODULE_TITLES: dict[str, str] = {
    "core": "底座内核",
    "middleware": "中间件",
    "agents": "阶段 Agent 层",
    "verticals": "垂类业务模块",
    "mobile": "语音交互移动端系统",
    #: 三、P0-1「Agent‑Wiki v2 运行时存储层」+ 配套规范《Agent‑Wiki 知识库开发规范》
    "agent_wiki": "Agent-Wiki 知识库",
    #: 三、P2「统一检索 API」：混合检索、检索路由与对话编排都在这个包里。
    #: 与 material_center（P2 的素材库本体）分开登记，是因为它被 P2/P3/P9
    #: 共用，硬塞进 PHASE_PACKAGES 会让「统一检索 API」这一层名字消失。
    "retrieval": "统一检索 API",
}

#: P4 子 Agent 的中文名。计划书原文（三、P4「包含子 Agent」）：
#: 「设备感知、天气感知、时间进度、增量抽取、路线方案、备选预案、代码迭代 Agent」
#: 原文里「Agent」只出现在最后一项，是七项共用的总称，故统一不加后缀——
#: 与阶段命名同一口径：P1 就叫「数据中台」，不叫「数据中台 Agent」。
SUBAGENT_TITLES: dict[str, str] = {
    "device_perception_agent": "设备感知",
    "weather_perception_agent": "天气感知",
    "time_progress_agent": "时间进度",
    "incremental_extraction_agent": "增量抽取",
    "route_plan_agent": "路线方案",
    "contingency_plan_agent": "备选预案",
    "code_iteration_agent": "代码迭代",
}

#: 不属于某个阶段的 Agent：横切设施 + 垂类业务单元。
#: planner 出自三、P3「智能规划」；qa 出自三、P6 与流程图「质检校验」；
#: butler 出自封面「管家 Agent」；tour 出自五「旅游 Agent 属于下垂类业务执行单元」。
CROSSCUT_TITLES: dict[str, str] = {
    "planner_agent": "智能规划 Agent",
    "qa_agent": "质检校验 Agent",
    "butler_agent": "管家 Agent",
    "tour_agent": "旅游 Agent",
}

#: **用户指令新增**的 Agent（计划书未点名）。中文名来自用户原话，
#: 每条必须在 core/decisions.py 有 source=人工授权 的对应裁定，
#: 且中文名出现在该裁定的 resolution 里（由守护测试机检）。
#: 2026-10-23：用户「再补充添加一个评测中台 agent」→ 裁定 U55。
USER_AGENT_TITLES: dict[str, str] = {
    "eval_platform_agent": "评测中台 Agent",
}


def module_title(package: str) -> str:
    """包目录 → 计划书层级中文名。

    有对应阶段的包**从 phases 派生**，不在本文件另存一份中文名——
    否则 phases.title 一改，这里就悄悄过期（E-44）。
    """
    code = PHASE_PACKAGES.get(package)
    if code:
        from ai_butler.core.phases import phase     # 延迟导入：避免 import 环
        return phase(code).display
    try:
        return MODULE_TITLES[package]
    except KeyError as exc:
        known = sorted({*PHASE_PACKAGES, *MODULE_TITLES})
        raise KeyError(f"未登记的包目录 {package!r}，已登记 {known}") from exc


def agent_title(key: str) -> str:
    """Agent 机读 key → 中文名。横切 Agent 与 P4 子 Agent 都查得到。"""
    for table in (CROSSCUT_TITLES, SUBAGENT_TITLES, USER_AGENT_TITLES):
        if key in table:
            return table[key]
    # 阶段 Agent 的 key 就是 phases 里的 key，中文名同样派生
    from ai_butler.core.phases import PHASE_BY_KEY
    spec = PHASE_BY_KEY.get(key)
    if spec is not None:
        return spec.display
    raise KeyError(f"未登记的 Agent key {key!r}")


def redis_key(*parts: object) -> str:
    """统一资源键：{NAMESPACE}:{part}:{part}..."""
    return ":".join([NAMESPACE, *[str(p) for p in parts]])


def vertical_key(vertical: str, *parts: object) -> str:
    """垂类命名空间。新增垂类只传垂类名，不新增命名规则（计划书五）。"""
    return redis_key(vertical, *parts)


def container(suffix: str) -> str:
    return f"{NAMESPACE}-{suffix}"


def env(name: str) -> str:
    return f"{ENV_PREFIX}{name}"


def describe() -> dict[str, str]:
    """供自检与文档生成使用。"""
    return {
        "ROOT": ROOT, "ECOSYSTEM": ECOSYSTEM, "PACKAGE": PACKAGE,
        "NAMESPACE": NAMESPACE, "ENV_PREFIX": ENV_PREFIX, "DB_NAME": DB_NAME,
    }
