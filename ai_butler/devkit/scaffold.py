"""P0-2 Agent 标准化脚手架。

计划书依据
----------
- 三、P0-2「搭建 Agent 标准化脚手架：一键生成目录、Class、状态注册、单元测试；
  脚手架自动注入 user_id 上下文；提供 Agent-Wiki 页面脚手架自动生成实体 / 声明元数据」
- 三、P4「逐个开发、逐个单测、逐个上线；所有子 Agent 只读全局状态、仅写入自有字段、
  无跨模块越权调用」
- 三、P4「隐私约束：读写私有数据必须从 LangGraph State 获取 user_id，
  不允许硬编码、不能为空」
- 五、5「模块低耦合，代码结构标准化，点餐、售后等新业务模块可复用整套架构」

生成的代码**自带约束**而不是靠开发者自觉：
- user_id 只能经 SubAgent.require_user_id(state) 取得；
- 写入字段被 writes 白名单限定，越界即抛 WriteScopeError；
- 生成的单测直接断言上述两条。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from ai_butler.agent_wiki.ontology import (
    CLAIM,
    ENTITY,
    DEFAULT_STATUS,
    new_page,
)
from ai_butler.agent_wiki.workspace import WikiStore

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class ScaffoldError(ValueError):
    pass


@dataclass
class ScaffoldResult:
    agent_name: str
    class_name: str
    files: list[Path] = field(default_factory=list)


def _class_name(agent_name: str) -> str:
    return "".join(part.capitalize() for part in agent_name.split("_")) + "Agent"


def _tuple_literal(items: tuple[str, ...]) -> str:
    if not items:
        return ""
    return ", ".join(f'"{i}"' for i in items) + ","


AGENT_TEMPLATE = Template('''"""$agent_name Agent —— 由 P0-2 脚手架生成。

计划书依据：
- 三、P4「所有子 Agent 只读全局状态、仅写入自有字段、无跨模块越权调用」
- 三、P4「读写私有数据必须从 LangGraph State 获取 user_id，不允许硬编码、不能为空」
- 三、P4「Wiki 约束：输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID」
- 三、P4「无跨模块越权调用」

约束（由 ai_butler/agents/base.py 与 agents/capabilities.py 强制）：
- user_id 只能从 State 取，不得硬编码、不得由构造参数注入；
- run() 只能返回 writes 白名单内的字段；
- 会话失效时本 Agent 不执行；
- 只能使用 capabilities 里声明的外部能力，未声明的装配层根本不会发到手上。
"""

from __future__ import annotations

from typing import Any, ClassVar

from ai_butler.agents.base import SubAgent


class $class_name(SubAgent):
    """$description"""

    name: ClassVar[str] = "$agent_name"
    #: 仅允许写入这些 State 字段（计划书三、P4「仅写入自有字段」）
    writes: ClassVar[tuple[str, ...]] = ($writes)
    #: 允许读取的 State 字段（五个必填字段始终可读）
    reads: ClassVar[tuple[str, ...]] = ($reads)
    #: 需要的外部能力（计划书三、P4「无跨模块越权调用」）。
    #: 取值见 ai_butler/agents/capabilities.py::CAPABILITIES；垂类 Agent 不得声明 payment。
    capabilities: ClassVar[tuple[str, ...]] = ($capabilities)
    #: 权责说明，供 AgentRegistry.ledger() 与人工审阅
    responsibilities: ClassVar[str] = "$responsibilities"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        # user_id 只能从这里取：硬编码或构造参数注入一律不合规（计划书三、P4）
        user_id = self.require_user_id(state)
        wiki_digest = self.wiki_digest(state)

        # TODO: 实现 $agent_name 的业务逻辑（计划书对应阶段）
        _ = (user_id, wiki_digest)
        return {"$default_write": None}
''');

TEST_TEMPLATE = Template('''"""$agent_name Agent 单元测试 —— 由 P0-2 脚手架生成。

计划书依据：三、P0-2「脚手架…一键生成目录、Class、状态注册、单元测试」
"""

from __future__ import annotations

import pytest

from ai_butler.agents.$agent_name.agent import $class_name
from ai_butler.devkit import testkit


def test_declares_write_scope():
    """计划书三、P4：仅写入自有字段。"""
    assert $class_name.writes, "$class_name 必须声明 writes 白名单"
    assert "$default_write" in $class_name.writes


def test_declares_capabilities_within_the_known_set():
    """计划书三、P4：无跨模块越权调用——能力声明必须落在已知集合内。"""
    from ai_butler.agents.capabilities import CAPABILITIES, FORBIDDEN_FOR_SUBAGENTS
    assert set($class_name.capabilities) <= set(CAPABILITIES)
    assert not (set($class_name.capabilities) & set(FORBIDDEN_FOR_SUBAGENTS))
    assert $class_name.responsibilities, "必须写明权责，供注册表台账与人工审阅"


def test_reads_user_id_from_state_and_writes_only_own_fields():
    agent = $class_name()
    state = testkit.make_state(user_id="u-1", session_id="s-1")
    updates = agent(state)
    testkit.assert_writes_within_scope(agent, updates)
    assert set(updates) <= set($class_name.writes)


def test_refuses_state_without_user_id():
    """计划书三、P4：user_id 不允许硬编码、不能为空。"""
    agent = $class_name()
    state = testkit.make_state(user_id="u-1", session_id="s-1")
    state["user_id"] = ""
    with pytest.raises(Exception):
        agent(state)


def test_skips_execution_when_session_is_invalid():
    """计划书三、P4：全局状态变更自动触发对应子任务执行；会话失效则跳过执行。"""
    agent = $class_name()
    state = testkit.make_state(user_id="u-1", session_id="s-1", login_status=False)
    assert agent(state) == {}
''');


def scaffold_agent(agent_name: str, *, root: str | Path, description: str = "",
                   writes: tuple[str, ...] = (), reads: tuple[str, ...] = (),
                   default_write: str | None = None,
                   capabilities: tuple[str, ...] = (),
                   responsibilities: str = "") -> ScaffoldResult:
    """一键生成 Agent 目录、Class 与单元测试。

    default_write 未指定时取 writes 的第一个；两者都为空则报错——
    计划书要求「仅写入自有字段」，没有写入字段的 Agent 无意义。
    """
    if not _NAME_RE.match(agent_name):
        raise ScaffoldError(
            f"Agent 名 {agent_name!r} 不合规：只允许小写字母、数字、下划线，且以字母开头"
        )
    writes = tuple(writes)
    if not writes:
        raise ScaffoldError("必须至少声明一个自有写入字段（计划书三、P4「仅写入自有字段」）")
    if default_write is None:
        default_write = writes[0]
    if default_write not in writes:
        raise ScaffoldError(f"default_write={default_write!r} 不在 writes 白名单内")

    # 能力声明在生成期就要合法：未知能力、或垂类 Agent 声明支付能力，一律拒绝生成
    from ai_butler.agents.capabilities import CAPABILITIES, FORBIDDEN_FOR_SUBAGENTS
    unknown = [c for c in capabilities if c not in CAPABILITIES]
    if unknown:
        raise ScaffoldError(f"未知能力 {unknown}，可选 {list(CAPABILITIES)}")
    banned = [c for c in capabilities if c in FORBIDDEN_FOR_SUBAGENTS]
    if banned:
        raise ScaffoldError(
            f"垂类 Agent 不得声明 {banned}：统一架构规范 10.11 要求经支付 Agent 中转")

    root = Path(root)
    pkg_dir = root / "ai_butler" / "agents" / agent_name
    tests_dir = root / "tests"
    if pkg_dir.exists():
        raise ScaffoldError(f"Agent 目录已存在：{pkg_dir}")
    pkg_dir.mkdir(parents=True)
    tests_dir.mkdir(parents=True, exist_ok=True)

    class_name = _class_name(agent_name)
    subs = {
        "agent_name": agent_name,
        "class_name": class_name,
        "description": description or f"{agent_name} 子 Agent（计划书阶段待补）",
        "writes": _tuple_literal(writes),
        "reads": _tuple_literal(reads),
        "default_write": default_write,
        "capabilities": _tuple_literal(tuple(capabilities)),
        "responsibilities": responsibilities or f"{agent_name} 的权责待补",
    }

    files: list[Path] = []
    init = pkg_dir / "__init__.py"
    init.write_text('"""由 P0-2 脚手架生成的 Agent 包。"""\n', encoding="utf-8")
    files.append(init)

    agent_py = pkg_dir / "agent.py"
    agent_py.write_text(AGENT_TEMPLATE.substitute(subs), encoding="utf-8")
    files.append(agent_py)

    test_py = tests_dir / f"test_agent_{agent_name}.py"
    test_py.write_text(TEST_TEMPLATE.substitute(subs), encoding="utf-8")
    files.append(test_py)

    return ScaffoldResult(agent_name=agent_name, class_name=class_name, files=files)


# --------------------------------------------------------------------------- #
# Agent-Wiki 页面脚手架
# --------------------------------------------------------------------------- #
def scaffold_wiki_entity(store: WikiStore, *, entity_id: str, name: str, entity_type: str,
                         user_id: str | None = None, now: str = "") -> Path:
    """生成实体页面（自动带上本体元数据）。"""
    page = new_page(ENTITY, entity_id, title=name, user_id=user_id, now=now,
                    name=name, entity_type=entity_type)
    return store.write_page(page, user_id=user_id)


def scaffold_wiki_claim(store: WikiStore, *, claim_id: str, subject_id: str, object_id: str,
                        description: str, confidence: float, evidence_ids: tuple[str, ...],
                        claim_type: str = "fact", status: str | None = None,
                        user_id: str | None = None, now: str = "",
                        contradicts: tuple[str, ...] = ()) -> Path:
    """生成声明页面。

    confidence 与 evidence_ids 由调用方提供：
    计划书三、P1 要求「每一条增量事实生成 Claim 声明并绑定 Evidence 证据；
    自动标记置信度」，因此本脚手架**不提供默认置信度**，避免凭空造数。
    """
    page = new_page(CLAIM, claim_id, title=description, confidence=confidence,
                    user_id=user_id, now=now,
                    status=status or DEFAULT_STATUS[CLAIM],
                    claim_type=claim_type, description=description,
                    subject_id=subject_id, object_id=object_id,
                    evidence_ids=list(evidence_ids),
                    contradicts=list(contradicts))
    return store.write_page(page, user_id=user_id)
