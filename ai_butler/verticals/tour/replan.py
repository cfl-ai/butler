"""行程重规划条件判定（裁定 U40）。

计划书依据
----------
- 五、第二阶段表 11「**行程动态更新**：天气变化、交通延误、景区临时闭园；
  **Jev 做条件判定是否触发重规划，条件成立才调用大模型重算路线**」
- 表 11 步骤4「持续监听天气、交通、景区状态变化；**Jev 高速决策判断是否达到重规划条件**；
  判定为 true 才触发重新调整行程，推送更新方案给用户」

裁定 U40
--------
原文点名的三类事件 → 推导出**确定性前置条件**，再由 Jev 最终判定。

**前置条件不成立时根本不调用大模型** —— 「条件成立才调用」这条设计的意义就在于省调用，
如果每次都召大模型问一遍「要不要重规划」，那这句话就白写了。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 触发原因
TRIGGER_CLOSURE = "scenic_closed"
TRIGGER_WEATHER = "weather_severe"
TRIGGER_DELAY = "traffic_delay"
TRIGGERS: tuple[str, ...] = (TRIGGER_CLOSURE, TRIGGER_WEATHER, TRIGGER_DELAY)

#: 天气严重度达到该档即触发（实现参数；原文只说「天气变化」）
DEFAULT_WEATHER_THRESHOLD = 3


@dataclass(frozen=True)
class ReplanSignals:
    """一次监听收集到的信号。"""

    scenic_closed: bool = False
    #: 天气严重度 0~4（0 正常，4 极端）
    weather_level: int = 0
    #: 交通延误分钟
    transport_delay_minutes: int = 0
    #: 当前行程还剩多少分钟余量
    schedule_slack_minutes: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.weather_level <= 4:
            raise ValueError("天气严重度必须落在 0~4")
        if self.transport_delay_minutes < 0 or self.schedule_slack_minutes < 0:
            raise ValueError("延误与余量不得为负")


@dataclass(frozen=True)
class ReplanDecision:
    """前置判定结论。

    ~{needs_llm}~ 为 True 才允许调用大模型重算路线；否则**保持原方案**。
    """

    needs_llm: bool
    triggers: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"needs_llm": self.needs_llm, "triggers": list(self.triggers),
                "reason": self.reason}


def evaluate_preconditions(signals: ReplanSignals, *,
                           weather_threshold: int = DEFAULT_WEATHER_THRESHOLD
                           ) -> ReplanDecision:
    """确定性前置判定（**不调用任何模型**）。"""
    triggers: list[str] = []

    if signals.scenic_closed:
        triggers.append(TRIGGER_CLOSURE)
    if signals.weather_level >= weather_threshold:
        triggers.append(TRIGGER_WEATHER)
    # 延误**超过余量**才算触发：延误了但还来得及，就不该重规划
    if signals.transport_delay_minutes > signals.schedule_slack_minutes:
        triggers.append(TRIGGER_DELAY)

    if not triggers:
        return ReplanDecision(
            False, (),
            f"无触发条件（天气 {signals.weather_level} < {weather_threshold}，"
            f"延误 {signals.transport_delay_minutes} ≤ 余量 {signals.schedule_slack_minutes}），"
            f"保持原方案，不调用大模型")

    ordered = tuple(t for t in TRIGGERS if t in triggers)
    return ReplanDecision(True, ordered,
                          "前置条件成立，交由 Jev 判定后重算路线：" + "、".join(ordered))
