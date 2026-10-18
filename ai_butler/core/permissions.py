"""三层权限分级与风控闸门。

计划书依据
----------
- 三、P4「无跨模块越权调用」「所有子 Agent 只读全局状态、仅写入自有字段」
- 三、P0-1「手机号、位置、支付信息等隐私字段独立加密存储」
- 一、项目概述「区分公共 Workspace 与用户私有 Vault」
- 三、P8「付款动作必须经人工确认」
- 七、验收 9–14（哈希存密码、行级隔离、未登录拒绝、切换清缓存、全链路脱敏、越权用例回归）

未定义项裁定
------------
计划书只规定了「不得越权」「公私二分」「是否登录」「付款需人工确认」，
**没有规定分级**。以下分级的取值为裁定 **U43（Agent 能力级）/ U44（数据密级）/
U45（用户角色与审批分档）/ U51（合成规则）**，来源与复用边界见 `core/decisions.py`。

三层各自管一件事
----------------
| 层 | 回答的问题 | 取值 |
|---|---|---|
| Agent 能力级 | 这个 Agent **能做什么动作** | L0 只读 / L1 写自有 / L2 副作用 / L3 高危 |
| 数据密级 | 这份数据**多敏感、怎么存怎么传** | L1 公开 / L2 内部 / L3 敏感 / L4 机密 |
| 用户角色级 | 这个人**是什么身份** | auditor / member / admin / owner |

**密级与公私域正交，不是替代关系**：作用域（public / vault）决定「谁能看见」，
密级决定「看见之后怎么处理」。一份 public 素材也可能是 L3 敏感（例如公开的含手机号页面）。

合成规则（U51）
--------------
三层**取最严者**（deny overrides），不做加权、不做投票。
理由：权限决策里「严格」的代价是麻烦，「宽松」的代价是泄露，二者不对称。

**权限不可传递**（U51）：A Agent 调 B Agent 时，B 必须按**自身**权限重新校验，
不得继承 A 的权限 —— 否则调用链就是一条借道提权的梯子。
本模块靠 `recheck_as()` 强制：调用方必须显式声明被调用者的身份。

默认拒绝
--------
未登记的组合一律拒绝。`grant()` 只做加法，没有「默认放行」的分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Mapping


class PermissionError(RuntimeError):
    """权限拒绝。**一律抛出**，绝不静默降级为「放行但记一笔」。"""


# ================================================================== #
# 第一层：Agent 能力级（U43）                                          #
# ================================================================== #
class CapabilityLevel(IntEnum):
    """Agent 能做什么动作。

    与 `SubAgent.capabilities` 的分工：
    `capabilities` 声明**能拿到哪些工具**（装配层裁剪工具面），
    本枚举声明**拿到之后能用到什么程度**。两者都要过。
    """

    L0_READ = 0        # 只读：可检索、可推理，不可写任何字段
    L1_WRITE_OWN = 1   # 写自有字段：对应 SubAgent.writes 的现有语义
    L2_SIDE_EFFECT = 2  # 可发起副作用：调外部工具、下单、发消息
    L3_CRITICAL = 3    # 高危：支付执行、删数据、改权限、批量外传


#: 各能力级默认需要的审批档（U45）。L3 一律双人，无例外。
#: **按「本动作需要哪一级」查表，不是按「Agent 是哪一级」** ——
#: 一个 L3 的支付 Agent 做只读检索，依然只需要 L0 的审批档。
DEFAULT_TIER_BY_LEVEL: Mapping[CapabilityLevel, int] = {
    CapabilityLevel.L0_READ: 0,
    CapabilityLevel.L1_WRITE_OWN: 0,
    CapabilityLevel.L2_SIDE_EFFECT: 1,
    CapabilityLevel.L3_CRITICAL: 2,
}


# ================================================================== #
# 第二层：数据密级（U44）                                              #
# ================================================================== #
class DataSensitivity(IntEnum):
    """数据敏感程度。决定存储/传输/外传策略，与公私域作用域正交。"""

    L1_PUBLIC = 1       # 可对外公开
    L2_INTERNAL = 2     # 仅内部可见，需传输加密
    L3_SENSITIVE = 3    # 手机号、位置、账单 —— 需存储加密
    L4_SECRET = 4       # 凭据、支付要素 —— 端到端加密 + 强制审计


#: 各密级的最低处理要求（U44 四级表）。
#: `may_enter_offline_link`：能否进入**脱敏前**的离线进化链路。
#: P10 红线：L3 及以上不得进 —— 脱敏后另说，但脱敏前的通路必须堵死。
SENSITIVITY_RULES: Mapping[DataSensitivity, dict[str, bool]] = {
    DataSensitivity.L1_PUBLIC: {"encrypt_transit": False, "encrypt_storage": False,
                                "audit_access": False, "may_enter_offline_link": True},
    DataSensitivity.L2_INTERNAL: {"encrypt_transit": True, "encrypt_storage": False,
                                  "audit_access": False, "may_enter_offline_link": True},
    DataSensitivity.L3_SENSITIVE: {"encrypt_transit": True, "encrypt_storage": True,
                                   "audit_access": True, "may_enter_offline_link": False},
    DataSensitivity.L4_SECRET: {"encrypt_transit": True, "encrypt_storage": True,
                                "audit_access": True, "may_enter_offline_link": False},
}


def requires_encryption(level: DataSensitivity, *, at_rest: bool) -> bool:
    """该密级是否需要加密（传输 / 存储）。"""
    rule = SENSITIVITY_RULES[level]
    return rule["encrypt_storage"] if at_rest else rule["encrypt_transit"]


def may_enter_offline_link(level: DataSensitivity) -> bool:
    """能否进入**脱敏前**的离线进化链路（P10 红线）。

    这是单向阀：一旦有 L3/L4 数据想进离线链路，必须在入口就被拦下。
    脱敏**之后**的数据走另一条通道，与本判定无关。
    """
    return SENSITIVITY_RULES[level]["may_enter_offline_link"]


# ================================================================== #
# 第三层：用户角色级（U45）                                            #
# ================================================================== #
class UserRole(IntEnum):
    """用户在本账号内的角色。数值越大权限越高，但**不做加权合成**（见 U51）。"""

    AUDITOR = 0   # 只读：可看审计日志，不可改任何配置
    MEMBER = 1    # 日常使用
    ADMIN = 2     # 可管本账号下 Agent 配置
    OWNER = 3     # 账号主体


#: 各角色被允许的最高能力级。取最严者原则：角色只是**上限**，不是授权。
ROLE_CEILING: Mapping[UserRole, CapabilityLevel] = {
    UserRole.AUDITOR: CapabilityLevel.L0_READ,
    UserRole.MEMBER: CapabilityLevel.L1_WRITE_OWN,
    UserRole.ADMIN: CapabilityLevel.L2_SIDE_EFFECT,
    UserRole.OWNER: CapabilityLevel.L3_CRITICAL,
}


# ================================================================== #
# 审批分档（U45）                                                     #
# ================================================================== #
class ApprovalTier(IntEnum):
    """按**副作用**定审批边界，而不是按「这个 Agent 重要不重要」。"""

    TIER0_NONE = 0    # 免审批：低风险可逆（建任务、打标签、检索）
    TIER1_SINGLE = 1  # 单人确认：用户可见或改数据（付款、改画像）
    TIER2_DUAL = 2    # 双人/强校验：高危（删数据、改权限、批量外传）


@dataclass(frozen=True)
class AgentIdentity:
    """Agent 身份（U43「身份与归属」）。权限判定的主体之一。

    `key` 用 `core.phases` 的阶段键（如 "p1_data_platform"），
    或垂类的 `vertical:*`，保持与注册表同一套命名。
    """

    key: str
    level: CapabilityLevel = CapabilityLevel.L0_READ

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise PermissionError("AgentIdentity.key 不得为空 —— 无主体的权限无从判定")


@dataclass(frozen=True)
class PermissionRequest:
    """一次权限申请。三层齐备才构成一次可判定的请求。

    `required` 是**本动作**需要的能力级，由**调用方显式声明**，不由布尔开关推导。
    """

    agent: AgentIdentity
    role: UserRole
    sensitivity: DataSensitivity
    #: 本动作所需的能力级。检索=L0 / 写自有字段=L1 / 调外部工具=L2 / 支付执行·删数据·改权限=L3。
    #: **必须显式给出**：曾因用「是否带副作用」这个布尔量推导，
    #: 导致 L3 高危动作（删数据、改权限）落不进判定，角色天花板形同虚设。
    required: CapabilityLevel = CapabilityLevel.L1_WRITE_OWN


@dataclass(frozen=True)
class PermissionVerdict:
    allowed: bool
    reason: str
    tier: ApprovalTier
    #: 三层各自的判定。**拒绝时也要填**，否则排查时只知道「被拒了」不知道「哪层拒的」（E-53）。
    by_layer: dict[str, bool] = field(default_factory=dict)

    def raise_if_denied(self, *, action: str) -> None:
        if self.allowed:
            return
        failed = [k for k, ok in self.by_layer.items() if not ok]
        raise PermissionError(
            f"拒绝执行「{action}」：{self.reason}"
            f"（未通过的层：{failed or '—'}）"
        )


def evaluate(request: PermissionRequest) -> PermissionVerdict:
    """三层合成判定。**取最严者**（U51 deny-overrides）。

    三层独立算出各自的结论，任一拒绝即拒绝。
    """
    by_layer: dict[str, bool] = {}
    needed = request.required

    # 第一层：Agent 自身能力够不够
    by_layer["agent_capability"] = request.agent.level >= needed

    # 第二层：角色天花板压不压得住
    by_layer["user_role"] = ROLE_CEILING[request.role] >= needed

    # 第三层：密级是否允许该动作。L4 机密只许读，任何 L2 及以上的动作都拒。
    by_layer["data_sensitivity"] = not (
        request.sensitivity == DataSensitivity.L4_SECRET
        and needed >= CapabilityLevel.L2_SIDE_EFFECT
    )

    if not all(by_layer.values()):
        return PermissionVerdict(
            allowed=False,
            reason="三层权限未全部通过（deny overrides，U51）",
            tier=ApprovalTier.TIER2_DUAL,
            by_layer=by_layer,
        )

    tier_value = DEFAULT_TIER_BY_LEVEL[needed]
    # 密级会**抬高**审批档，但不会降低 —— 同样是取最严者
    if request.sensitivity >= DataSensitivity.L3_SENSITIVE:
        tier_value = max(tier_value, int(ApprovalTier.TIER1_SINGLE))
    if request.sensitivity == DataSensitivity.L4_SECRET:
        tier_value = max(tier_value, int(ApprovalTier.TIER2_DUAL))

    return PermissionVerdict(
        allowed=True,
        reason="三层权限均通过",
        tier=ApprovalTier(tier_value),
        by_layer=by_layer,
    )


def recheck_as(caller: AgentIdentity, callee: AgentIdentity,
               request: PermissionRequest) -> PermissionVerdict:
    """**权限不可传递**（U51）：被调方必须按自身身份重新判定。

    A 调 B 时，B 拿到的不是「A 的权限」，而是**B 自己的权限**。
    这条是借道提权的唯一防线：如果 B 能继承 A 的 L3，
    那么任何一个 L3 Agent 被攻破，整条调用链上所有 Agent 都变成 L3。

    用法：调度层在把任务下发给 B 之前，先用 B 的身份调一次本函数。
    """
    if request.agent.key != caller.key:
        raise PermissionError(
            f"请求主体是 {request.agent.key}，却声明调用方是 {caller.key} —— 身份不一致，拒绝"
        )
    # 关键：换成被调方自己的身份与能力级，其余条件沿用
    as_callee = PermissionRequest(
        agent=callee,
        role=request.role,
        sensitivity=request.sensitivity,
        required=request.required,
    )
    verdict = evaluate(as_callee)
    if caller.level > callee.level and verdict.allowed:
        # 理论上不会发生（evaluate 已按 callee 判定），留作断言式防线
        raise PermissionError(
            f"{callee.key} 的能力级低于 {caller.key}，不得继承其权限（U51 权限不可传递）"
        )
    return verdict


def assert_offline_safe(level: DataSensitivity, *, what: str) -> None:
    """离线进化链路的单向阀（P10 红线）。

    凡是要把数据送进**脱敏前**的离线链路，必须先过这一条。
    L3/L4 一律拒绝 —— 不是「记一笔警告」，是直接抛。
    """
    if not may_enter_offline_link(level):
        raise PermissionError(
            f"密级 {level.name} 的数据不得进入脱敏前的离线进化链路：{what}"
            f"（P10 红线；脱敏后走另一条通道）"
        )
