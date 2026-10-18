"""阶段 Agent 基类 —— 每个阶段项目本身就是一项 Agent。

计划书依据
----------
- 计划书封面「管家 Agent 生态体系项目计划书」：生态由管家 Agent 统一管辖
- 三、P4「逐个开发、逐个单测、逐个上线；所有子 Agent 只读全局状态、仅写入自有字段」
- 三、P3「绑定所有 Skill 工具、子 Agent、业务流程统一入口」

与业务子 Agent 的区别
--------------------
| | 阶段 Agent | 业务子 Agent |
| 粒度 | 一个阶段（P1 数据中台…） | 一个业务动作（订酒店、查天气…） |
| 管辖者 | 管家 Agent | P3 场景调度 Agent |
| 职责 | 对外提供本阶段能力，并汇报本阶段运行态 | 完成具体业务任务 |
| 写入字段 | 自有 ~{pN_digest}~ | 自有业务字段 |

**未实现的阶段不得注册实例**：由管家按 phases.planned_phases() 显式列出，
避免「登记了就等于做到了」的假象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ai_butler.agents.base import SubAgent
from ai_butler.core.phases import PhaseSpec, phase


class PhaseError(RuntimeError):
    pass


class PhaseNotImplemented(PhaseError):
    """阶段尚未落地。调用即抛，绝不返回假数据。"""


@dataclass
class PhaseAgent(SubAgent):
    """阶段 Agent。子类必须声明 ~{phase_code}~；其余元数据一律从 phases 真值源取。"""

    #: 计划书阶段编号，如 "P1"
    phase_code: ClassVar[str] = ""
    #: 本阶段 Agent 对外写入的 State 字段（自有字段）
    writes: ClassVar[tuple[str, ...]] = ()
    #: 本阶段需要的外部能力（计划书三、P4「无跨模块越权调用」）
    capabilities: ClassVar[tuple[str, ...]] = ()

    tools: Any = None
    settings: Any = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.__dict__.get("phase_code"):
            return                      # 抽象中间类，跳过
        spec = phase(cls.phase_code)    # 未知阶段编号在此报错
        cls.name = spec.key
        cls.responsibilities = f"{spec.display}：{spec.core_capability}"

    # ------------------------------------------------------------------ #
    @classmethod
    def spec(cls) -> PhaseSpec:
        return phase(cls.phase_code)

    @classmethod
    def manifest(cls) -> dict[str, Any]:
        """本阶段的对外说明，供管家台账使用。"""
        spec = cls.spec()
        return {"code": spec.code, "key": spec.key, "display": spec.display,
                "title": spec.title, "layer": spec.layer,
                "core_capability": spec.core_capability,
                "capabilities": list(cls.capabilities),
                "writes": list(cls.writes),
                "responsibilities": cls.responsibilities,
                "implemented": spec.implemented}

    def digest_key(self) -> str:
        return f"{self.phase_code.lower().replace('-', '')}_digest"
