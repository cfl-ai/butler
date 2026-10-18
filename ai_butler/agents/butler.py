"""管家 Agent —— 统一管辖全部阶段 Agent。

计划书依据
----------
- 计划书封面「**管家 Agent 生态体系项目计划书**」
- 一、项目概述「业务层：场景调度 Agent 全局唯一总指挥」
- 三、P3「绑定所有 Skill 工具、子 Agent、业务流程**统一入口**；未登录拒绝执行任何业务 Skill」
- 三、P3「开发多子 Agent 自动分发路由；下发任务携带 user_id、wiki_digest」
- 三、P8「由**顶层管家**统一调度所有支付流程」

管辖范围
--------
| | 管辖者 | 被管辖 |
| 阶段 Agent（P0-1…P10） | **管家 Agent** | 每个阶段一项 Agent |
| 业务子 Agent（订酒店…） | P3 场景调度 Agent | 具体业务动作 |

管家只做**管辖**：登记、能力治理、会话闸门、配额、台账与覆盖度。
业务意图怎么路由是 P3 的事，管家不重复实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ai_butler.agents.base import SubAgent, resolve
from ai_butler.agents.capabilities import CAPABILITIES, AgentTools
from ai_butler.agents.phase import PhaseAgent, PhaseNotImplemented
from ai_butler.agents.registry import AgentRegistry, AgentSpec
from ai_butler.core import phases
from ai_butler.core.phases import BUTLER_DISPLAY, BUTLER_KEY, PhaseSpec


class ButlerError(RuntimeError):
    pass


@dataclass
class ButlerResult:
    user_id: str
    session_id: str
    granted: bool                       # 是否通过会话闸门
    code: str = ""                      # 拒绝码 / 阶段编号
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "session_id": self.session_id,
                "granted": self.granted, "code": self.code, "detail": self.detail,
                "payload": dict(self.payload)}


class ButlerAgent:
    """管家 Agent。"""

    #: 会话闸门拒绝码
    LOGIN_REQUIRED = "LOGIN_REQUIRED"

    def __init__(self, *, settings: Any = None,
                 available: Mapping[str, Any] | None = None) -> None:
        self.settings = settings
        self.registry = AgentRegistry()
        #: 能力实现池：阶段 Agent 声明什么，就只发什么（计划书三、P4「无跨模块越权调用」）
        self._available: dict[str, Any] = dict(available or {})
        self._scheduler: Any = None

    # ---------------- 装配 ---------------- #
    def provide(self, capability: str, implementation: Any) -> None:
        if capability not in CAPABILITIES:
            raise ButlerError(f"未知能力 {capability!r}，可选 {list(CAPABILITIES)}")
        self._available[capability] = implementation

    def attach_scheduler(self, scheduler: Any) -> None:
        """挂上 P3 的调度器，管家即可作为业务**统一入口**。"""
        self._scheduler = scheduler

    # ---------------- 登记 ---------------- #
    def enroll(self, agent: SubAgent) -> AgentSpec:
        """登记一项阶段 Agent。命名必须与计划书真值源一致，否则拒绝。"""
        code = getattr(agent, "phase_code", "")
        if not code:
            raise ButlerError(
                f"{type(agent).__name__} 未声明 phase_code，不是阶段 Agent；"
                f"业务子 Agent 请交给 P3 场景调度 Agent 管辖")
        spec = phases.phase(code)                       # 未知编号在此报错
        expected = spec.key
        if getattr(agent, "name", "") != expected:
            raise ButlerError(
                f"阶段 Agent 命名与计划书真值源不一致：{getattr(agent, 'name', '')!r} "
                f"≠ {expected!r}（{spec.display}）")
        registered = self.registry.register(agent)
        agent.tools = self.registry.tools_for(agent.name, self._available)
        return registered

    def enroll_all(self, agents: Iterable[SubAgent]) -> list[AgentSpec]:
        return [self.enroll(a) for a in agents]

    # ---------------- 管辖视图 ---------------- #
    def phase_agents(self) -> tuple[PhaseAgent, ...]:
        """按**阶段真值源的声明顺序**返回（P0-1 → P10）。

        早先这里用 registry.names()（字母序），P10 落地后
        'p10_harness' < 'p1_data_platform'，台账顺序就与计划书对不上了。
        阶段顺序属于真值源，不能让字符串排序决定。
        """
        order = {spec.key: i for i, spec in enumerate(phases.PHASES)}
        agents = [a for a in self.registry.all() if isinstance(a, PhaseAgent)]
        return tuple(sorted(agents, key=lambda a: order.get(a.name, len(order))))

    def ledger(self) -> list[dict[str, Any]]:
        """全部阶段的管辖台账。

        **未落地的阶段照样列出**，implemented=False —— 不允许出现
        「只登记做过的，看起来像全做完了」这种假象。
        """
        enrolled = {a.phase_code: a for a in self.phase_agents()}
        rows: list[dict[str, Any]] = []
        for spec in phases.PHASES:
            agent = enrolled.get(spec.code)
            rows.append({
                "code": spec.code, "display": spec.display, "title": spec.title,
                "layer": spec.layer, "implemented": spec.implemented,
                "enrolled": agent is not None,
                "agent_key": spec.key,
                "capabilities": list(getattr(agent, "capabilities", ()) or ()),
                "writes": list(getattr(agent, "writes", ()) or ()),
                "responsibilities": getattr(agent, "responsibilities", "")
                                  or f"{spec.display}：{spec.core_capability}",
                "note": spec.note,
            })
        return rows

    def coverage(self) -> dict[str, Any]:
        total = len(phases.PHASES)
        done = len(phases.implemented_phases())
        enrolled = len(self.phase_agents())
        return {"butler": BUTLER_KEY, "phases_total": total,
                "phases_implemented": done, "phases_enrolled": enrolled,
                "implemented": [p.code for p in phases.implemented_phases()],
                "planned": [p.code for p in phases.planned_phases()],
                "enrolled_codes": sorted(a.phase_code for a in self.phase_agents())}

    def unimplemented(self) -> tuple[PhaseSpec, ...]:
        return phases.planned_phases()

    def spec_of(self, code: str) -> PhaseSpec:
        return phases.phase(code)

    # ---------------- 受管辖地调用 ---------------- #
    def govern(self, state: Mapping[str, Any]) -> ButlerResult:
        """会话闸门。管家的第一道管控，先于任何阶段与业务执行。"""
        uid = str(state.get("user_id") or "")
        sid = str(state.get("session_id") or "")
        if not uid.strip():
            return ButlerResult(uid, sid, granted=False, code="NO_USER_ID",
                                detail="管辖要求携带 user_id（计划书三、P4 隐私约束）")
        if not state.get("login_status"):
            return ButlerResult(uid, sid, granted=False, code=self.LOGIN_REQUIRED,
                                detail="会话失效：管家拒绝放行任何业务 Skill（计划书三、P3）")
        return ButlerResult(uid, sid, granted=True)

    async def run_phase(self, code: str, state: Mapping[str, Any]) -> ButlerResult:
        """在管辖下调用某个阶段 Agent。阶段 Agent 的 run 可为同步或异步。"""
        verdict = self.govern(state)
        if not verdict.granted:
            return verdict

        spec = phases.phase(code)
        agent = next((a for a in self.phase_agents() if a.phase_code == code), None)
        if agent is None:
            # 未登记 = 未落地。**明确拒绝，绝不返回假数据。**
            raise PhaseNotImplemented(
                f"{spec.display}（{spec.title}）尚未落地，管家不予放行。"
                f"计划书该阶段核心能力：{spec.core_capability}")

        payload = await resolve(agent(dict(state)))
        return ButlerResult(verdict.user_id, verdict.session_id, granted=True, code=code,
                            payload=payload)

    async def handle(self, *, user_id: str, session_id: str, utterance: str,
                     labels: Iterable[str], login_status: bool = True,
                     wiki_digest: Any = None) -> ButlerResult:
        """业务统一入口：管家过闸 → 交给 P3 调度器分发业务子 Agent。"""
        state = {"user_id": user_id, "session_id": session_id,
                 "login_status": login_status, "utterance": utterance}
        verdict = self.govern(state)
        if not verdict.granted:
            return verdict
        if self._scheduler is None:
            raise ButlerError("管家尚未挂接调度器（attach_scheduler），无法处理业务请求")

        result = await self._scheduler.run(
            user_id=user_id, session_id=session_id, utterance=utterance,
            labels=tuple(labels), login_status=login_status, wiki_digest=wiki_digest)
        return ButlerResult(user_id=verdict.user_id, session_id=verdict.session_id,
                            granted=True, code=result.intent or "",
                            detail=result.clarifying_question,
                            payload=result.as_dict())

    # ---------------- 观测 ---------------- #
    def snapshot(self) -> dict[str, Any]:
        return {"butler": BUTLER_KEY, "display": BUTLER_DISPLAY,
                "coverage": self.coverage(),
                "enrolled_agents": list(self.registry.names()),
                "capabilities_available": sorted(self._available),
                "scheduler_attached": self._scheduler is not None}

    def tools_of(self, code: str) -> AgentTools:
        agent = next((a for a in self.phase_agents() if a.phase_code == code), None)
        if agent is None:
            raise PhaseNotImplemented(f"{phases.phase(code).display} 尚未落地，无工具面")
        return self.registry.tools_for(agent.name, self._available)
