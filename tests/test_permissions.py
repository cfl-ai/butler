"""三层权限分级的守护测试（U43 / U44 / U45 / U51）。

为什么要有它
------------
「声明了权限」和「权限真的生效」是两回事（MEMORY E-21 / E-29）。
本文件的每条断言都对应一种**具体的越权手法**，而不是「函数能跑通」：
把自己降到低权限、借道调用链提权、把敏感数据塞进离线链路、用角色冒充能力。

裁定依据见 `core/decisions.py` 的 U43（Agent 能力级）/ U44（数据密级）/
U45（角色与审批分档）/ U51（合成规则 = deny overrides + 权限不可传递）。
"""

from __future__ import annotations

import pytest

from ai_butler.core.permissions import (
    ROLE_CEILING,
    AgentIdentity,
    ApprovalTier,
    CapabilityLevel,
    DataSensitivity,
    PermissionError,
    PermissionRequest,
    UserRole,
    assert_offline_safe,
    evaluate,
    may_enter_offline_link,
    recheck_as,
    requires_encryption,
)


def _req(*, level: CapabilityLevel = CapabilityLevel.L1_WRITE_OWN,
         role: UserRole = UserRole.OWNER,
         sensitivity: DataSensitivity = DataSensitivity.L1_PUBLIC,
         required: CapabilityLevel = CapabilityLevel.L1_WRITE_OWN,
         key: str = "p1_data_platform") -> PermissionRequest:
    return PermissionRequest(
        agent=AgentIdentity(key=key, level=level),
        role=role, sensitivity=sensitivity, required=required)


# ---------------------------- 第一层：Agent 能力级 ---------------------------- #

def test_read_only_agent_cannot_write():
    """L0 只读 Agent 想写库 —— 必须被拒，而不是「写了但记一笔」。"""
    verdict = evaluate(_req(level=CapabilityLevel.L0_READ))
    assert not verdict.allowed
    assert not verdict.by_layer["agent_capability"], "应明确标出是能力级这一层没过"


def test_action_level_is_declared_explicitly_not_inferred():
    """动作所需能力级必须**显式声明**。

    曾因用「是否带副作用」布尔量推导，L3 高危动作（删数据、改权限）
    根本落不进判定，角色天花板形同虚设 —— 这里把它钉死。
    """
    # 调外部工具 → L2
    assert not evaluate(_req(level=CapabilityLevel.L1_WRITE_OWN,
                             required=CapabilityLevel.L2_SIDE_EFFECT)).allowed
    assert evaluate(_req(level=CapabilityLevel.L2_SIDE_EFFECT,
                         required=CapabilityLevel.L2_SIDE_EFFECT)).allowed

    # 删数据 → L3。只有 L3 且角色够高才行，L2 Agent 不行
    assert not evaluate(_req(level=CapabilityLevel.L2_SIDE_EFFECT,
                             required=CapabilityLevel.L3_CRITICAL)).allowed
    assert evaluate(_req(level=CapabilityLevel.L3_CRITICAL,
                         required=CapabilityLevel.L3_CRITICAL)).allowed


def test_approval_tier_follows_the_action_not_the_agent():
    """一个 L3 的支付 Agent 做只读检索，只需 L0 的审批档 —— 不因身份高就一律双人。"""
    readonly = evaluate(_req(level=CapabilityLevel.L3_CRITICAL,
                             required=CapabilityLevel.L0_READ))
    assert readonly.tier == ApprovalTier.TIER0_NONE

    critical = evaluate(_req(level=CapabilityLevel.L3_CRITICAL,
                             required=CapabilityLevel.L3_CRITICAL))
    assert critical.tier == ApprovalTier.TIER2_DUAL


# ---------------------------- 第三层：密级 ---------------------------- #

def test_secret_data_never_allows_side_effect():
    """L4 机密数据一律只许读。L2 及以上的动作碰 L4 就拒。"""
    verdict = evaluate(_req(level=CapabilityLevel.L3_CRITICAL,
                            sensitivity=DataSensitivity.L4_SECRET,
                            required=CapabilityLevel.L2_SIDE_EFFECT))
    assert not verdict.allowed
    assert not verdict.by_layer["data_sensitivity"]

    # 只读 L4 是允许的（且必须双人审批，见另一条测试）
    assert evaluate(_req(level=CapabilityLevel.L3_CRITICAL,
                         sensitivity=DataSensitivity.L4_SECRET,
                         required=CapabilityLevel.L0_READ)).allowed


def test_sensitivity_raises_approval_tier_but_never_lowers_it():
    """密级只能**抬高**审批档，不能降低（取最严者原则）。"""
    low = evaluate(_req(level=CapabilityLevel.L1_WRITE_OWN,
                        sensitivity=DataSensitivity.L1_PUBLIC))
    high = evaluate(_req(level=CapabilityLevel.L1_WRITE_OWN,
                         sensitivity=DataSensitivity.L3_SENSITIVE))
    assert high.tier >= low.tier

    critical = evaluate(_req(level=CapabilityLevel.L3_CRITICAL,
                             sensitivity=DataSensitivity.L4_SECRET))
    assert critical.tier == ApprovalTier.TIER2_DUAL, "L4 必须走到双人审批"


def test_encryption_requirements_are_monotonic():
    """密级越高，加密要求只增不减。"""
    for level in DataSensitivity:
        for stricter in DataSensitivity:
            if stricter <= level:
                continue
            assert (requires_encryption(stricter, at_rest=True)
                    >= requires_encryption(level, at_rest=True))


# ---------------------------- P10 红线：离线链路 ---------------------------- #

def test_sensitive_data_cannot_enter_offline_link_before_redaction():
    """L3/L4 不得进入**脱敏前**的离线进化链路 —— 单向阀，直接抛不是警告。"""
    assert may_enter_offline_link(DataSensitivity.L1_PUBLIC)
    assert may_enter_offline_link(DataSensitivity.L2_INTERNAL)
    assert not may_enter_offline_link(DataSensitivity.L3_SENSITIVE)
    assert not may_enter_offline_link(DataSensitivity.L4_SECRET)

    with pytest.raises(PermissionError):
        assert_offline_safe(DataSensitivity.L3_SENSITIVE, what="用户手机号")


# ---------------------------- 权限不可传递（U51） ---------------------------- #

def test_callee_cannot_inherit_callers_higher_level():
    """借道提权：高权限 A 调低权限 B，B 不得继承 A 的能力。

    这是调用链上唯一一道防线 —— 若 B 能继承，攻破任一 L3 Agent
    就等于把整条链上所有 Agent 都升成 L3。
    """
    caller = AgentIdentity(key="p8_payment", level=CapabilityLevel.L3_CRITICAL)
    callee = AgentIdentity(key="p2_material_center", level=CapabilityLevel.L0_READ)

    # 请求主体必须是调用方，否则直接拒（防身份冒用）
    verdict = recheck_as(caller, callee,
                         _req(key="p8_payment", level=CapabilityLevel.L3_CRITICAL))
    assert not verdict.allowed, "被调方 L0 不得因调用方是 L3 而获得写权限"


def test_callee_cannot_inherit_callers_level_even_for_same_action():
    """同一动作，被调方能力不足即拒 —— 不因「是谁在调」而放行。"""
    caller = AgentIdentity(key="p8_payment", level=CapabilityLevel.L3_CRITICAL)
    callee = AgentIdentity(key="p4_mops", level=CapabilityLevel.L2_SIDE_EFFECT)
    # 删数据 L3：调用方能做，被调方只能到 L2 —— 必须拒
    verdict = recheck_as(caller, callee,
                         _req(key="p8_payment", level=CapabilityLevel.L3_CRITICAL,
                              required=CapabilityLevel.L3_CRITICAL))
    assert not verdict.allowed


def test_recheck_rejects_identity_mismatch():
    """声明的调用方与请求主体不一致 —— 拒绝，不接受「大概是他」。"""
    caller = AgentIdentity(key="p8_payment", level=CapabilityLevel.L3_CRITICAL)
    callee = AgentIdentity(key="p1_data_platform", level=CapabilityLevel.L1_WRITE_OWN)
    with pytest.raises(PermissionError):
        recheck_as(caller, callee, _req(key="p1_data_platform"))


# ---------------------------- 合成规则：deny overrides ---------------------------- #

def test_deny_overrides_when_any_layer_refuses():
    """三层任一拒绝即拒绝（U51）。不允许「两层同意所以放行」的加权逻辑。"""
    # 角色是天花板：OWNER 也压不住一个 L0 Agent 想写库
    verdict = evaluate(_req(level=CapabilityLevel.L0_READ, role=UserRole.OWNER))
    assert not verdict.allowed

    # 反过来：ADMIN 天花板是 L2，即使 Agent 自己是 L3，L3 动作（删数据）也要拒
    verdict = evaluate(_req(level=CapabilityLevel.L3_CRITICAL, role=UserRole.ADMIN,
                            required=CapabilityLevel.L3_CRITICAL))
    assert not verdict.allowed
    assert not verdict.by_layer["user_role"], "应明确标出是角色天花板这一层没过"

    # ADMIN 做 L2 动作（调外部工具）是允许的
    assert evaluate(_req(level=CapabilityLevel.L3_CRITICAL, role=UserRole.ADMIN,
                         required=CapabilityLevel.L2_SIDE_EFFECT)).allowed


def test_role_ceiling_is_monotonic_and_auditor_is_read_only():
    """角色天花板单调递增，且 auditor 恒为只读。"""
    assert ROLE_CEILING[UserRole.AUDITOR] == CapabilityLevel.L0_READ
    order = [UserRole.AUDITOR, UserRole.MEMBER, UserRole.ADMIN, UserRole.OWNER]
    for lower, higher in zip(order, order[1:]):
        assert ROLE_CEILING[higher] >= ROLE_CEILING[lower]


def test_verdict_names_the_refusing_layer():
    """被拒时要说清**哪一层拒的**，不能只说「没权限」（E-53）。"""
    verdict = evaluate(_req(level=CapabilityLevel.L0_READ))
    failed = [k for k, ok in verdict.by_layer.items() if not ok]
    assert failed, "拒绝时必须给出未通过的层"

    with pytest.raises(PermissionError) as exc:
        verdict.raise_if_denied(action="写入画像")
    assert "agent_capability" in str(exc.value)


def test_empty_agent_key_is_refused():
    """无主体的权限申请 —— 无从判定，直接拒。"""
    with pytest.raises(PermissionError):
        AgentIdentity(key="   ")
