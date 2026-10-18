"""垂类 Agent 通用脚手架（裁定 U39）。

计划书依据
----------
- 五、第二阶段表 11「模块定位：旅游 Agent 属于**下垂类业务执行单元**，
  **不负责意图判断、任务分发**，仅接收五层意图路由中枢下发标准化任务；
  完整兼容 Jev 决策、Agent-Wiki 公私知识库、全局回退防甩锅规则；
  **点餐、售后等其他垂类直接复制本模块结构**，替换业务逻辑、工具集即可快速接入」
- 五、第二阶段表 12「回退规则：**仅任务完全超出业务域才允许回退**；
  本职范围内**参数缺失、数据采集失败、方案生成失败禁止回退**，
  自行提示用户、重试或引导补充信息。单任务最大回退次数全局 2 次上限」
- 五、第二阶段 5「模块边界界定：旅游 Agent 仅负责行程业务逻辑，
  **无权修改路由规则、无权分发任务给其他 Agent**；
  所有跨模块请求（支付、售后）**必须通过管家路由中枢中转，Agent 之间禁止直接互相调用**；
  所有回退操作必须**原路返回路由中枢**，Agent 之间不允许直接转交任务，防止甩锅循环」

裁定 U39
--------
依原文「可复制、可替换」的要求，把各垂类**共有且不可省略**的四件事固化到基类：
① 职责边界（✅必做 / ❌必回退）② 回退规则 ③ 参数解析与缺失询问 ④ 结果归档。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agents.base import SubAgent

#: 全局最大回退次数（表 12，与 P3 的全局约束同一数值）
MAX_ROLLBACKS = 2

# ---- 失败类别（表 12 把「可否回退」直接绑定在类别上）---- #
FAILURE_OUT_OF_SCOPE = "out_of_scope"          # 超出业务域 → **允许**回退
FAILURE_MISSING_PARAM = "missing_parameter"    # 参数缺失 → 禁止回退，问用户
FAILURE_COLLECT_FAILED = "collect_failed"      # 采集失败 → 禁止回退，重试
FAILURE_PLAN_FAILED = "plan_failed"            # 方案生成失败 → 禁止回退，降级提示
FAILURE_KINDS: tuple[str, ...] = (FAILURE_OUT_OF_SCOPE, FAILURE_MISSING_PARAM,
                                  FAILURE_COLLECT_FAILED, FAILURE_PLAN_FAILED)

#: **唯一**允许回退的类别集合 —— 写成集合是为了让「多一类」变得显眼
ROLLBACK_ALLOWED: frozenset[str] = frozenset({FAILURE_OUT_OF_SCOPE})

#: 处置动作
ACTION_ROLLBACK = "rollback"
ACTION_ASK_USER = "ask_user"
ACTION_RETRY = "retry"
ACTION_DEGRADE = "degrade"


class VerticalError(RuntimeError):
    pass


@dataclass(frozen=True)
class DutyBoundary:
    """一个垂类的职责边界（表 11 的 ✅ / ❌ 两张清单）。"""

    vertical: str
    in_scope: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    #: 超域任务应该交给谁（仅作**路由提示**，垂类不得自行转交）
    handoff_hint: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.vertical:
            raise VerticalError("职责边界必须声明垂类名")
        if not self.in_scope:
            raise VerticalError(f"{self.vertical} 必须声明至少一项本职能力")

    def classify(self, task_kind: str) -> str:
        if task_kind in self.in_scope:
            return FAILURE_OUT_OF_SCOPE if False else "in_scope"
        if task_kind in self.out_of_scope:
            return FAILURE_OUT_OF_SCOPE
        # **未列入的既不算本职也不算明确超域** —— 按超域处理更安全吗？
        # 不：表 12 说「仅任务**完全**超出业务域才允许回退」，
        # 未列入说明边界没写全，属于配置问题，必须显式失败而不是猜。
        raise VerticalError(
            f"任务类别 {task_kind!r} 未在 {self.vertical} 的职责边界内声明；"
            f"✅={list(self.in_scope)} ❌={list(self.out_of_scope)}")


@dataclass(frozen=True)
class FallbackDecision:
    """对一次失败的处置。"""

    allowed: bool
    action: str
    reason: str
    failure: str

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "action": self.action,
                "reason": self.reason, "failure": self.failure}


def decide_fallback(*, failure: str, rollbacks_used: int = 0,
                    max_rollbacks: int = MAX_ROLLBACKS) -> FallbackDecision:
    """表 12 的回退判定。

    **只有超域才允许回退**；域内失败一律自行处置。达到全局上限后即使超域也不回退，
    避免与路由中枢形成甩锅循环。
    """
    if failure not in FAILURE_KINDS:
        raise VerticalError(f"未知失败类别 {failure!r}，可选 {list(FAILURE_KINDS)}")

    if failure not in ROLLBACK_ALLOWED:
        action = {FAILURE_MISSING_PARAM: ACTION_ASK_USER,
                  FAILURE_COLLECT_FAILED: ACTION_RETRY,
                  FAILURE_PLAN_FAILED: ACTION_DEGRADE}[failure]
        return FallbackDecision(
            False, action,
            f"{failure} 属于本职范围内的失败，表 12 明令禁止回退，"
            f"应由垂类自行处置（{action}）", failure)

    if rollbacks_used >= max_rollbacks:
        return FallbackDecision(
            False, ACTION_DEGRADE,
            f"已达全局回退上限 {max_rollbacks} 次，不再回退，避免甩锅循环", failure)

    return FallbackDecision(True, ACTION_ROLLBACK,
                            "任务完全超出业务域，原路返回路由中枢", failure)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    required: bool = True
    description: str = ""


@dataclass
class ParamResult:
    values: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    def ask_text(self) -> str:
        if not self.missing:
            return ""
        return "还需要你补充：" + "、".join(self.missing)


def parse_params(raw: Mapping[str, Any], specs: Sequence[ParamSpec]) -> ParamResult:
    """抽取并校验必填参数（表 11 步骤1）。

    **缺失参数不抛异常、不回退** —— 表 12 要求「主动询问用户补齐，禁止回退路由」，
    所以这里返回缺失清单，由调用方去问。
    """
    values: dict[str, Any] = {}
    missing: list[str] = []
    for spec in specs:
        value = raw.get(spec.name)
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            if spec.required:
                missing.append(spec.description or spec.name)
            continue
        values[spec.name] = value
    return ParamResult(values=values, missing=missing)


@dataclass
class VerticalAgent(SubAgent):
    """垂类 Agent 基类（裁定 U39）。

    **不负责意图判断、不负责任务分发**（表 11 模块定位）——
    因此本基类不提供任何路由能力；跨模块请求只能产出「请求描述」交管家中转。
    """

    #: 职责边界，子类必须声明
    boundary: ClassVar[DutyBoundary]

    #: 按能力裁剪后的工具面（与 PhaseAgent 同构）。
    #: 垂类同样受「无跨模块越权调用」约束，因此装配层也要裁剪它的工具面，
    #: 未声明的能力**根本不会发到手上**。
    tools: Any = None

    #: 垂类**无权**修改路由规则或向其他 Agent 分发任务（表 11 模块边界）
    may_route: ClassVar[bool] = False
    may_dispatch_to_peers: ClassVar[bool] = False

    @classmethod
    def out_of_scope_request(cls, *, target: str, payload: Mapping[str, Any]
                             ) -> dict[str, Any]:
        """产出跨模块请求描述，**交管家中转**（Agent 之间禁止直接调用）。

        这里**只产描述、不执行** —— 垂类没有把自己变成调度者的口子。
        """
        if not target:
            raise VerticalError("跨模块请求必须指明目标模块")
        return {"relay_via": "butler_agent", "target": target,
                "payload": dict(payload),
                "note": "表中 11 要求跨模块请求必须通过管家路由中枢中转"}

    def declare_scope(self, task_kind: str) -> str:
        return self.boundary.classify(task_kind)
