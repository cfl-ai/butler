"""P0-2 AI 研发效能提升平台（开发 & 测试 & 迭代底座）。

计划书依据
----------
- 二、P0「2. AI 研发效能提升平台（开发 & 测试 & 迭代底座）」
- 二、P0「搭建 Agent 标准化脚手架」「建立全场景测试用例库」
  「搭建 Prompt 版本库」「开发自动化评测脚本」「对接 Harness 自进化体系」

职责边界
--------
对外代表 P0-2 的效能工具面，并汇报用例库覆盖度、脚手架与评测能力清单。
"""

from __future__ import annotations

# 直接运行本文件（python xxx.py / PyCharm 右键）时，sys.path[0] 是本文件所在目录
# 而非项目根，`import ai_butler` 会直接 ModuleNotFoundError。此处把项目根补进去；
# 作为包被 import 时 __package__ 非空，这段不执行。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    del _sys, _Path

from dataclasses import dataclass
from typing import Any, ClassVar

from ai_butler.agents.phase import PhaseAgent

#: P0-2 对外代表的能力面（计划书二、P0「2. AI 研发效能提升平台」）。
#:
#: **这份清单不是声明，是实测对象** —— 见 _probe_capabilities()。
_DEVKIT_MODULES: tuple[str, ...] = (
    "scaffold",
    "testkit",
    "prompt_registry",
    "scenario_library",
    "evaluation",
    "log_query",
)


def _probe_capabilities() -> tuple[list[str], list[str]]:
    """逐个真的 import 一遍，返回（可用, 缺失）。

    这里原先写的是**硬编码的字符串清单**：digest 声称拥有 6 项能力，
    但其中三项从没被 import 过 —— 模块被改名或删掉，它依然报「有」。
    那正是「声明 ≠ 能力」，只不过这次出在本项目自己的代码里。
    """
    import importlib

    loaded: list[str] = []
    missing: list[str] = []
    for name in _DEVKIT_MODULES:
        try:
            importlib.import_module("ai_butler.devkit." + name)
        except Exception:       # noqa: BLE001 - 探测就是要吞掉任何导入失败
            missing.append(name)
        else:
            loaded.append(name)
    return loaded, missing


@dataclass
class DevkitAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P0-2"
    writes: ClassVar[tuple[str, ...]] = ("p0_devkit_digest",)
    capabilities: ClassVar[tuple[str, ...]] = ()

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        from ai_butler.devkit.evaluation import EvalReport
        from ai_butler.devkit.scenario_library import default_library
        from ai_butler.devkit.testkit import SCENARIOS

        library = default_library()
        pending = [c.case_id for c in library.pending_cases()]
        loaded, missing = _probe_capabilities()
        return {"p0_devkit_digest": {
            "user_id": uid, "phase": self.phase_code,
            "scenario_categories": list(SCENARIOS),
            "case_total": len(library.cases),
            "case_coverage": library.coverage(),
            "case_pending": pending,
            "eval_metrics": list(getattr(EvalReport, "__dataclass_fields__", {})),
            # 实测结果，不是写死的字符串。缺哪个会出现在 capabilities_missing 里，
            # 由 tests/test_agents_p0_devkit.py 盯着 —— 模块不见了就该变红。
            "capabilities": loaded,
            "capabilities_missing": missing,
        }}

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
