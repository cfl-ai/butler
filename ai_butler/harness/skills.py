"""P10 MCP + Skill 工具注册（裁定 U35）。

计划书依据
----------
- 三、P10 步骤5「基于 **MCP+Skill 协议**集成检索、Git、文件、终端工具；
   **新增 MCP Skill：Agent-Wiki 维护 Skill，仅用于维护公共 Workspace**」
- 三、P10 离线链路「通过 MCP Skill 维护公共 Agent-Wiki Workspace 知识库
  （清理冲突、归档过期来源、生成报告）」

裁定 U35
--------
用官方 ~{mcp}~ Python SDK；工具以 Skill 注册表组织，每个 Skill **显式声明作用域**。
「Agent-Wiki 维护 Skill」**只允许公共 Workspace** —— 在注册层就拒绝任何带 user_id 的目标，
而不是指望调用方自觉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: 作用域
SCOPE_PUBLIC = "public"
SCOPE_VAULT = "vault"
SCOPES: tuple[str, ...] = (SCOPE_PUBLIC, SCOPE_VAULT)

#: 工具类别（计划书点名：检索、Git、文件、终端）
TOOL_RETRIEVAL = "retrieval"
TOOL_GIT = "git"
TOOL_FILE = "file"
TOOL_TERMINAL = "terminal"
TOOL_CATEGORIES: tuple[str, ...] = (TOOL_RETRIEVAL, TOOL_GIT, TOOL_FILE, TOOL_TERMINAL)

#: 计划书点名新增的 Skill
SKILL_AGENT_WIKI = "agent_wiki_maintenance"


class SkillError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    category: str
    #: 允许作用域。**默认只允许公共**——私有 Vault 必须显式开，且默认不开。
    allowed_scopes: tuple[str, ...] = (SCOPE_PUBLIC,)
    handler: Callable[..., Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.category not in TOOL_CATEGORIES:
            raise SkillError(f"未知工具类别 {self.category!r}，可选 {list(TOOL_CATEGORIES)}")
        for scope in self.allowed_scopes:
            if scope not in SCOPES:
                raise SkillError(f"未知作用域 {scope!r}")


def assert_skill_scope(skill: SkillSpec, *, scope: str,
                       user_id: str | None = None) -> None:
    """调用前的作用域闸门。

    两条独立检查：
    1. 作用域必须在 Skill 允许列表内；
    2. **公共作用域不得携带 user_id**（计划书：公共 Workspace 无用户 ID）。
    """
    if scope not in skill.allowed_scopes:
        raise SkillError(
            f"Skill {skill.name!r} 不允许作用于 {scope!r}（允许 {list(skill.allowed_scopes)}）；"
            f"私有 Vault 须显式开启，默认不开")
    if scope == SCOPE_PUBLIC and user_id:
        raise SkillError(
            f"Skill {skill.name!r} 以公共作用域调用却携带 user_id={user_id!r}；"
            f"公共 Workspace 不含用户 ID")


def agent_wiki_maintenance_skill(*, handler: Callable[..., Any] | None = None
                                 ) -> SkillSpec:
    """计划书点名新增的 MCP Skill：**仅用于维护公共 Workspace**。"""
    return SkillSpec(
        name=SKILL_AGENT_WIKI,
        description="维护公共 Agent-Wiki Workspace：清理冲突、归档过期来源、生成报告",
        category=TOOL_FILE,
        allowed_scopes=(SCOPE_PUBLIC,),          # 只允许公共
        handler=handler)


@dataclass
class SkillRegistry:
    skills: dict[str, SkillSpec] = field(default_factory=dict)

    def register(self, skill: SkillSpec) -> None:
        if skill.name in self.skills:
            raise SkillError(f"Skill {skill.name!r} 已注册")
        self.skills[skill.name] = skill

    def get(self, name: str) -> SkillSpec:
        try:
            return self.skills[name]
        except KeyError:
            raise SkillError(f"未注册的 Skill：{name!r}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.skills))

    def by_category(self, category: str) -> tuple[SkillSpec, ...]:
        return tuple(s for s in self.skills.values() if s.category == category)

    def call(self, name: str, *, scope: str, user_id: str | None = None,
             **kwargs: Any) -> Any:
        skill = self.get(name)
        assert_skill_scope(skill, scope=scope, user_id=user_id)
        if skill.handler is None:
            raise SkillError(f"Skill {name!r} 未绑定实现")
        return skill.handler(**kwargs)


def default_registry() -> SkillRegistry:
    """预置注册表：覆盖计划书点名的四类工具 + Agent-Wiki 维护 Skill。"""
    registry = SkillRegistry()
    registry.register(SkillSpec("retrieval_search", "混合检索（P2 服务）",
                                TOOL_RETRIEVAL))
    registry.register(SkillSpec("git_status", "查看工作区状态", TOOL_GIT))
    registry.register(SkillSpec("git_diff", "查看变更", TOOL_GIT))
    registry.register(SkillSpec("file_read", "读取文件", TOOL_FILE))
    registry.register(SkillSpec("file_write", "写入文件", TOOL_FILE))
    registry.register(SkillSpec("terminal_run", "在沙箱内执行命令", TOOL_TERMINAL))
    registry.register(agent_wiki_maintenance_skill())
    return registry
