"""P4 子 Agent 注册表与权责台账。

计划书依据
----------
- 三、P4「逐个开发、逐个单测、逐个上线」
- 三、P4「Agent 之间**不得直接互相传递任务**，必须走场景调度 Agent 路由层」
- 三、P4「无跨模块越权调用」
- 五、5「旅游 Agent 仅负责行程业务逻辑，**无权修改路由规则、无权分发任务给其他 Agent**」

结构性保证
----------
本注册表**刻意不提供**任何「让 A Agent 调用 B Agent」的入口，
也不提供任何修改路由规则的方法。Agent 之间要协作，只能经由 P3 的调度器。
这不是靠文档约定，而是靠这里根本没有那个方法。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import AgentTools, declared_capabilities, grant


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    """注册台账条目：权责、写入范围、能力声明。"""

    name: str
    writes: tuple[str, ...]
    reads: tuple[str, ...]
    capabilities: tuple[str, ...]
    responsibilities: str = ""


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, SubAgent] = {}

    # ---------------- 注册 ---------------- #
    def register(self, agent: SubAgent) -> AgentSpec:
        name = getattr(agent, "name", "")
        if not name or not str(name).strip():
            raise RegistryError("子 Agent 必须声明非空的 name")
        if name in self._agents:
            raise RegistryError(f"子 Agent 名称重复：{name!r}（计划书三、P4「逐个上线」）")
        if not getattr(agent, "writes", ()):
            raise RegistryError(
                f"子 Agent {name!r} 未声明自有写入字段。"
                f"计划书三、P4：所有子 Agent 仅写入自有字段。")
        declared_capabilities(type(agent))          # 未定义能力在此报错
        self._agents[name] = agent
        return self.spec(name)

    def unregister(self, name: str) -> None:
        if name not in self._agents:
            raise RegistryError(f"未注册的子 Agent：{name!r}")
        del self._agents[name]

    # ---------------- 查询 ---------------- #
    def get(self, name: str) -> SubAgent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise RegistryError(f"未注册的子 Agent：{name!r}") from exc

    def names(self) -> tuple[str, ...]:
        """名字（**字母序**）。注意不要用它来排阶段顺序——见 butler.phase_agents。"""
        return tuple(sorted(self._agents))

    def all(self) -> tuple[SubAgent, ...]:
        """按**注册顺序**返回全部 Agent。"""
        return tuple(self._agents.values())

    def spec(self, name: str) -> AgentSpec:
        agent = self.get(name)
        return AgentSpec(name=name, writes=tuple(getattr(agent, "writes", ())),
                         reads=tuple(getattr(agent, "reads", ())),
                         capabilities=declared_capabilities(type(agent)),
                         responsibilities=getattr(agent, "responsibilities", ""))

    def specs(self) -> tuple[AgentSpec, ...]:
        return tuple(self.spec(n) for n in self.names())

    def ledger(self) -> list[dict[str, Any]]:
        """权责台账，供人工审阅与文档核对。"""
        return [{"name": s.name, "responsibilities": s.responsibilities,
                 "writes": list(s.writes), "reads": list(s.reads),
                 "capabilities": list(s.capabilities)} for s in self.specs()]

    # ---------------- 能力装配 ---------------- #
    def tools_for(self, name: str, available: Mapping[str, Any], *,
                  allow_forbidden: bool = False) -> AgentTools:
        """只发放该 Agent 已声明的能力。"""
        return grant(type(self.get(name)), available, allow_forbidden=allow_forbidden)
