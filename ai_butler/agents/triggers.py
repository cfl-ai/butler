"""P4 全局状态变更 → 子任务自动触发。

计划书依据
----------
- 三、P4「触发时机：**全局状态变更自动触发对应子任务执行**；会话失效则跳过执行」

边界
----
本模块只**计算**该触发谁，不执行任何子 Agent。
真正的分发仍由 P3 的调度器完成——计划书三、P4 明确要求
「Agent 之间不得直接互相传递任务，必须走场景调度 Agent 路由层」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_butler.agents.registry import AgentRegistry, RegistryError


class TriggerError(RuntimeError):
    pass


@dataclass(frozen=True)
class TriggerRule:
    """某个状态字段发生变化时，应当触发的子 Agent。"""

    watch: tuple[str, ...]
    agents: tuple[str, ...]
    note: str = ""

    def __post_init__(self) -> None:
        if not self.watch:
            raise TriggerError("触发规则必须声明关注的 State 字段")
        if not self.agents:
            raise TriggerError("触发规则必须声明要触发的子 Agent")


@dataclass(frozen=True)
class TriggerPlan:
    fired: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.fired

    def as_dict(self) -> dict[str, Any]:
        return {"fired": list(self.fired), "skipped": list(self.skipped),
                "changed": list(self.changed), "reasons": list(self.reasons)}


def changed_fields(before: Mapping[str, Any], after: Mapping[str, Any],
                   *, ignore: Sequence[str] = ()) -> tuple[str, ...]:
    """比较前后状态，返回发生变化的字段名。"""
    ignored = set(ignore)
    keys = (set(before) | set(after)) - ignored
    return tuple(sorted(k for k in keys if before.get(k) != after.get(k)))


class TriggerBus:
    def __init__(self, *, registry: AgentRegistry, rules: Sequence[TriggerRule],
                 ignore_fields: Sequence[str] = ()) -> None:
        self.registry = registry
        self.rules = list(rules)
        self.ignore_fields = tuple(ignore_fields)
        known = set(registry.names())
        for rule in self.rules:
            missing = [a for a in rule.agents if a not in known]
            if missing:
                raise RegistryError(
                    f"触发规则引用了未注册的子 Agent：{missing}；"
                    f"已注册：{sorted(known)}")

    def plan(self, *, before: Mapping[str, Any], after: Mapping[str, Any],
             login_status: bool = True) -> TriggerPlan:
        changed = changed_fields(before, after, ignore=self.ignore_fields)
        wanted: list[str] = []
        reasons: list[str] = []
        for rule in self.rules:
            hit = [f for f in rule.watch if f in changed]
            if hit:
                wanted.extend(rule.agents)
                reasons.append(f"字段 {hit} 变化 → 触发 {list(rule.agents)}"
                               + (f"（{rule.note}）" if rule.note else ""))

        # 去重且保持注册表顺序，便于断言
        ordered = [n for n in self.registry.names() if n in set(wanted)]

        if not login_status:
            # 计划书三、P4：会话失效则跳过执行
            return TriggerPlan(fired=(), skipped=tuple(ordered), changed=changed,
                               reasons=tuple(reasons + ["会话失效，全部子任务跳过"]))
        return TriggerPlan(fired=tuple(ordered), skipped=(), changed=changed,
                           reasons=tuple(reasons))
