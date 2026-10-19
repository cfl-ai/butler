"""Agent 模块的命令行入口 —— 让 `python ai_butler/agents/<模块>.py` 直接可跑。

计划书依据
----------
- 三、P4「逐个开发、逐个单测、逐个上线」

裁定
----
命令行壳的形态为裁定 **U52**（来源与复用边界见 `core/decisions.py`），
业务执行链路沿用 **U48**（`standalone.run_standalone`），不另写一套。

为什么要有这个文件
----------------
此前 `python ai_butler/agents/p1_data_platform.py` 会**静默退出**：
模块里只有类定义，没有任何可执行语句，退出代码 0、什么都不打印。
开发者右键运行看到「进程已结束，退出代码为 0」，第一反应是文件坏了
或者项目根本没实现 —— **能跑但不说话，比不能跑更糟**，因为它伪装成正常。

三条边界（U48 的延续，不是新规矩）
--------------------------------
1. **不带参数只打印档案**：只读类属性（ClassVar），不实例化、不连数据库、
   不碰任何外部依赖。档案必须带中文名与计划书依据，让「这个 Agent 是谁」
   一眼可查，而不是甩一个类名让人自己猜。
2. **执行业务必须显式身份**：`--user-id` 与 `--session-id` 缺一个就报错，
   绝不默认填个假值 —— 没有用户上下文的写入无法做行级隔离（计划书三、P0-1）。
3. **命令行不旁路任何校验**：三层权限、State 契约、写作用域、治理判定照旧，
   全部复用 `standalone.run_standalone`。省掉的只有 LangGraph 编排。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from typing import Any

from ai_butler.agents.phase import PhaseAgent
from ai_butler.core import naming, phases


class CliError(RuntimeError):
    """命令行用法错误。**报错并退出非零**，不静默降级成打印档案。"""


#: 集群模块：成员由 ROSTER 名册驱动，打印名册而非单个 Agent 档案
CLUSTER_MODULES = frozenset({"mops"})


def find_agent_classes(module: Any) -> list[type]:
    """按展示优先级返回**本模块定义**的 Agent 类。

    只认 `__module__` 等于本模块的类：直接 `python xxx.py` 运行时模块名为
    `__main__`，本文件的类都算；import 进来的基类（SubAgent / PhaseAgent）
    `__module__` 是别的路径，自然被排除 —— 不需要维护硬编码名单。
    """
    phase_agents: list[type] = []
    plain_agents: list[type] = []
    for obj in vars(module).values():
        if not isinstance(obj, type):
            continue
        if getattr(obj, "__module__", "") != module.__name__:
            continue
        if obj.__name__.startswith("_"):
            continue          # 抽象中间类（如 _MopsAgent），打印它没有信息量
        if issubclass(obj, PhaseAgent):
            phase_agents.append(obj)
        elif _is_subagent(obj):
            plain_agents.append(obj)
        elif obj.__name__.endswith("Agent"):
            # 管家 Agent 不是 SubAgent 子类（它是管辖者，不是被管辖者），
            # 但同样要有档案 —— 否则 `python butler.py` 只会甩一个类名。
            plain_agents.append(obj)
    return phase_agents + plain_agents


def _is_subagent(obj: type) -> bool:
    from ai_butler.agents.base import SubAgent
    return issubclass(obj, SubAgent) and obj.__name__ != "SubAgent"


def _agent_rows(cls: type) -> list[tuple[str, str]]:
    """一个 Agent 的档案行。字段全部来自 ClassVar 与 phases 真值源。"""
    name = getattr(cls, "name", "") or ""
    code = getattr(cls, "phase_code", "") or ""
    if code:
        spec = phases.phase(code)
        return [
            ("中文名", spec.display),
            ("阶段", f"{spec.code} {spec.title}"),
            ("机读 key", spec.key),
            ("核心能力", spec.core_capability),
            ("落地状态", "已实现" if spec.implemented else f"未落地：{spec.note}"),
            ("写入字段", "、".join(getattr(cls, "writes", ()) or ()) or "（无）"),
            ("需要能力", "、".join(getattr(cls, "capabilities", ()) or ()) or "（无）"),
            ("权责", getattr(cls, "responsibilities", "") or "（未声明）"),
        ]
    try:
        cn = naming.agent_title(name) if name else ""
    except KeyError:
        cn = ""
    return [
        ("中文名", cn or cls.__name__),
        ("机读 key", name or cls.__name__),
        ("权责", getattr(cls, "responsibilities", "") or "（未声明）"),
        ("写入字段", "、".join(getattr(cls, "writes", ()) or ()) or "（无）"),
        ("需要能力", "、".join(getattr(cls, "capabilities", ()) or ()) or "（无）"),
    ]


def _display_width(text: str) -> int:
    """终端显示宽度：中文与全角符号占 2 列，ASCII 占 1 列。

    直接用 `len()` + `ljust` 对齐，含中文的行会歪掉 —— 字符数相等但列宽不等。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _module_label(module: Any) -> str:
    """模块的人读标识。

    直接 `python xxx.py` 运行时 `__name__` 是 `__main__`，打印它等于什么都没说；
    此时改用文件名（`p1_data_platform`），与 registry 的 key 对得上。
    """
    name = getattr(module, "__name__", "")
    if name and name != "__main__":
        return name.rsplit(".", 1)[-1]
    file = getattr(module, "__file__", "") or ""
    return file.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".py") or "(unknown)"


def _print_agent(cls: type) -> None:
    rows = _agent_rows(cls)
    width = max(_display_width(k) for k, _ in rows)
    title = f"── {cls.__name__} "
    print(title + "─" * max(4, 60 - _display_width(title)))
    for key, value in rows:
        print(f"  {_pad(key, width)} : {value}")


def _print_cluster(module: Any) -> None:
    """集群模块：打印 ROSTER 名册（成员、中文名、各自写入字段）。"""
    roster = getattr(module, "ROSTER", ())
    print(f"── 集群名册（{len(roster)} 个成员）" + "─" * 30)
    for key, title, field in roster:
        print(f"  {_pad(title, 10)} key={key:<28} 写入={field}")


def print_profile(module: Any) -> None:
    """打印档案。**只读元数据，不实例化、不连库** —— 保证右键运行必有输出。"""
    label = _module_label(module)
    print("=" * 62)
    print(f" {label} —— 直接运行仅打印档案，不碰数据库")
    print("=" * 62)
    if label in CLUSTER_MODULES:
        _print_cluster(module)
        agents = find_agent_classes(module)
    else:
        agents = find_agent_classes(module)
        if not agents:
            print("（本模块没有定义 Agent 类）")
    for cls in agents:
        _print_agent(cls)
    print("─" * 62)
    print(" 独立执行一次业务：加 --user-id <用户> --session-id <会话>")
    print(" 集群/基类模块不建议直接跑业务，请跑对应的阶段模块")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python <agent 模块>.py", add_help=True,
        description="不带参数打印本 Agent 档案；带 --user-id 则独立执行一次业务")
    parser.add_argument("--user-id", default="",
                        help="调用方用户标识（独立执行必填，缺省则只打印档案）")
    parser.add_argument("--session-id", default="",
                        help="会话标识（与 --user-id 同时给出才会执行业务）")
    parser.add_argument("--role", default="member",
                        choices=["auditor", "member", "admin", "owner"],
                        help="调用方用户角色，默认 member")
    parser.add_argument("--sensitivity", default="l2_internal",
                        choices=["l1_public", "l2_internal", "l3_sensitive", "l4_secret"],
                        help="本次调用涉及的数据密级，默认 l2_internal")
    parser.add_argument("--required", default="l1_write_own",
                        choices=["l0_read", "l1_write_own", "l2_side_effect", "l3_critical"],
                        help="本次动作所需能力级，默认 l1_write_own")
    return parser


def main(module: Any, argv: list[str] | None = None) -> int:
    """命令行主函数。返回进程退出码。"""
    args = list(sys.argv[1:] if argv is None else argv)
    opts = _build_parser().parse_args(args)

    label = _module_label(module)
    agents = find_agent_classes(module)

    if not opts.user_id and not opts.session_id:
        print_profile(module)
        return 0

    # ---- 给了身份信息就要跑业务：先校验用法，再谈执行 ----
    if not opts.user_id:
        raise CliError("执行业务必须同时给出 --user-id 与 --session-id（缺 user_id）")
    if not opts.session_id:
        raise CliError("执行业务必须同时给出 --user-id 与 --session-id（缺 session_id）")
    if not agents:
        raise CliError(f"{label} 没有可执行的 Agent 类")
    if label in CLUSTER_MODULES:
        raise CliError(
            f"{label} 是集群模块，成员由 P4 集群统一调度；"
            f"请改为运行 ai_butler/agents/p4_mops.py")

    from ai_butler.agents.standalone import StandaloneContext, run_standalone

    ctx = StandaloneContext(
        user_id=opts.user_id,
        session_id=opts.session_id,
        role=_role(opts.role),
        sensitivity=_sensitivity(opts.sensitivity),
        required=_capability(opts.required),
    )
    agent = agents[0]()
    result = run_standalone(agent, ctx=ctx)
    print(f"已独立执行 {result.agent_key}（{result.elapsed_ms} ms）")
    print(f"  审批档 Tier{result.approval_tier}"
          f"{'，待人工确认' if result.needs_approval else ''}")
    for key, value in sorted(result.updates.items()):
        print(f"  {key} = {value!r}")
    return 0


def _role(value: str):
    from ai_butler.core.permissions import UserRole
    return UserRole(value.upper())


def _sensitivity(value: str):
    from ai_butler.core.permissions import DataSensitivity
    return DataSensitivity(value.upper())


def _capability(value: str):
    from ai_butler.core.permissions import CapabilityLevel
    return CapabilityLevel(value.upper())
