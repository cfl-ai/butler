"""P4 子 Agent 能力声明与工具面裁剪。

计划书依据
----------
- 三、P4「所有子 Agent 只读全局状态、仅写入自有字段、**无跨模块越权调用**」
- 三、P2「对外统一检索 API，供**规划 Agent、质检 Agent**、Jev 决策节点调用」
- 三、P4「Wiki 约束：子 Agent 产出事实优先写入对应用户私有 Vault」
- 统一架构规范 10.11「垂类 Agent 不得直接调用支付执行能力」

做法
----
「无跨模块越权调用」不靠自觉：装配层按 Agent 的 capabilities **裁剪**出一个工具面，
未声明的能力在该工具面上取不到，取即抛 CapabilityError。
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping, Sequence

# 能力清单属**实现层命名**：计划书三、P4 只要求「无跨模块越权调用」，
# 未枚举具体能力项，故此处按 P1/P2/P4 已点名过的访问路径逐条对应。
CAP_RETRIEVAL = "retrieval"        # 统一检索 API（P2：供规划 Agent、质检 Agent 调用）
CAP_WIKI_READ = "wiki_read"        # 读取 Agent-Wiki 页面（公共 Workspace + 本人 Vault）
CAP_WIKI_WRITE = "wiki_write"      # 写入 Vault / Workspace（P4 Wiki 约束）
CAP_LLM = "llm"                    # LLM 网关（慢思考）
CAP_JEV = "jev"                    # Jev 高速结构化决策（快思考）
CAP_DATA_READ = "data_read"        # 数据 SDK 只读（P1「所有 Agent 只读」）
CAP_PAYMENT = "payment"            # 支付执行能力（垂类 Agent 不得直接持有）

# Jev 单列一项能力：计划书三、P3 要求「Jev 调用独立配额统计」，
# 它与普通大模型调用是两条独立受管的路径（各自的限流桶与配额）。
CAPABILITIES: tuple[str, ...] = (CAP_RETRIEVAL, CAP_WIKI_READ, CAP_WIKI_WRITE, CAP_LLM,
                                 CAP_JEV, CAP_DATA_READ, CAP_PAYMENT)

#: 任何垂类子 Agent 都不得声明的能力（统一架构规范 10.11）
FORBIDDEN_FOR_SUBAGENTS: tuple[str, ...] = (CAP_PAYMENT,)


class CapabilityError(RuntimeError):
    """子 Agent 触达了未声明（或已声明但未装配）的能力。"""


@dataclass(frozen=True)
class AgentTools:
    """按能力裁剪后的工具面。属性名与能力常量一一对应。"""

    granted: tuple[str, ...] = ()
    retrieval: Any = None
    wiki_read: Any = None
    wiki_write: Any = None
    llm: Any = None
    jev: Any = None
    data_read: Any = None
    payment: Any = None

    def has(self, capability: str) -> bool:
        return capability in self.granted

    def require(self, capability: str):
        if capability not in CAPABILITIES:
            raise CapabilityError(
                f"未知能力 {capability!r}，可选 {list(CAPABILITIES)}")
        if capability not in self.granted:
            raise CapabilityError(
                f"子 Agent 未声明能力 {capability!r}，禁止调用。"
                f"计划书三、P4：所有子 Agent 只读全局状态、无跨模块越权调用。"
                f"已声明：{list(self.granted)}")
        value = getattr(self, capability)
        if value is None:
            raise CapabilityError(f"能力 {capability!r} 已声明但装配层未提供实现")
        return value

    def granted_names(self) -> tuple[str, ...]:
        return tuple(self.granted)


def grant(agent_cls: type, available: Mapping[str, Any], *,
          allow_forbidden: bool = False) -> AgentTools:
    """按 agent 声明的 capabilities 生成工具面。

    未声明的能力不会出现在工具面上；声明了但装配层没提供的，取用时才会报错。
    """
    declared = tuple(getattr(agent_cls, "capabilities", ()) or ())
    unknown = [c for c in declared if c not in CAPABILITIES]
    if unknown:
        raise CapabilityError(
            f"{getattr(agent_cls, 'name', agent_cls)!r} 声明了未知能力 {unknown}，"
            f"可选 {list(CAPABILITIES)}")
    # 支付能力的禁令只针对**垂类 Agent**；支付 Agent 自身须声明 may_hold_payment=True。
    if not allow_forbidden and not getattr(agent_cls, "may_hold_payment", False):
        banned = [c for c in declared if c in FORBIDDEN_FOR_SUBAGENTS]
        if banned:
            raise CapabilityError(
                f"{getattr(agent_cls, 'name', agent_cls)!r} 不得声明 {banned}："
                f"统一架构规范 10.11 禁止垂类 Agent 直接调用支付执行能力，必须经支付 Agent 中转。"
                f"若确为支付 Agent 自身，须显式声明 may_hold_payment = True")

    valid = {f.name for f in fields(AgentTools)} - {"granted"}
    # 声明了能力却没有对应字段 = 有人加了常量忘了加字段。
    # 这里**必须报错**：静默丢弃会让 Agent 在运行期才炸，且报错点离病因很远。
    orphan = [c for c in declared if c not in valid]
    if orphan:
        raise CapabilityError(
            f"能力 {orphan} 在 CAPABILITIES 里存在，但 AgentTools 没有对应字段；"
            f"请同步补上字段，否则该能力会被静默丢弃")
    payload = {name: available.get(name) for name in declared}
    return AgentTools(granted=declared, **payload)


def declared_capabilities(agent_cls: type) -> tuple[str, ...]:
    return tuple(getattr(agent_cls, "capabilities", ()) or ())


def assert_capabilities_within(agent_cls: type, allowed: Sequence[str]) -> None:
    """装配期校验：Agent 声明不得超出装配层允许的范围。"""
    over = [c for c in declared_capabilities(agent_cls) if c not in set(allowed)]
    if over:
        raise CapabilityError(
            f"{getattr(agent_cls, 'name', agent_cls)!r} 声明了超出允许范围的能力 {over}")
