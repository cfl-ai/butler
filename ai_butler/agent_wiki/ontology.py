"""Agent-Wiki v2 知识本体。

计划书依据
----------
- 一、项目概述「Agent-Wiki v2 知识本体范式：本体包含：实体 Entity、来源 Source、声明 Claim、
  证据 Evidence、关系 Relation、矛盾 Contradiction、开放问题 Question、综合摘要 Synthesis。
  同时支持人类可读 Markdown 页面 + 机器编译缓存（agent-digest.json / claims.jsonl / pages.json）；
  区分公共 Workspace（全用户共享公开知识）与用户私有 Vault（绑定 user_id，个人私有知识库），
  实现事实溯源、矛盾检测，抑制大模型幻觉。」
- 三、P1「每一条增量事实生成 Claim 声明并绑定 Evidence 证据；自动标记置信度，识别 Contradiction 矛盾声明」
- 三、P2「原始素材自动生成 Agent-Wiki source.* 源页面，保存来源元数据」
- 三、P2「过期来源标记为 status:archived 归档」
- 七、18「知识具备完整本体…编译可输出机器可读缓存文件」

开源借鉴依据（详见 ai_butler/core/decisions.py）
----------------------------------------------
- U17：claim 字段以 Microsoft GraphRAG 的 covariates 为起点
       （claim_type / description / subject_id / object_id / status /
        valid_from / valid_to(ISO8601) / evidence.source_text / evidence.text_unit_id / source_url）
- U16：矛盾语义采用 Graphiti 的 fact-invalidation —— **不删除旧声明**，
       给旧关系写 invalid_at、新增 contradicts 关系，保留完整时间线
- U18：页面形态采用 Markdown + YAML frontmatter + 固定小节（Observations / Relations）
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import frontmatter

# --------------------------------------------------------------------------- #
# 本体要素枚举（计划书原文的八个要素，逐字对应）
# --------------------------------------------------------------------------- #
ENTITY = "entity"
SOURCE = "source"
CLAIM = "claim"
EVIDENCE = "evidence"
RELATION = "relation"
CONTRADICTION = "contradiction"
QUESTION = "question"
SYNTHESIS = "synthesis"

PAGE_TYPES: tuple[str, ...] = (ENTITY, SOURCE, CLAIM, EVIDENCE, RELATION,
                               CONTRADICTION, QUESTION, SYNTHESIS)

#: U17（GraphRAG status）+ 三、P2（archived 归档）
CLAIM_STATUSES: tuple[str, ...] = ("TRUE", "FALSE", "SUSPECTED", "ARCHIVED")

#: 三、P2「过期来源标记为 status:archived 归档」
SOURCE_ACTIVE = "active"
SOURCE_ARCHIVED = "archived"
SOURCE_STATUSES: tuple[str, ...] = (SOURCE_ACTIVE, SOURCE_ARCHIVED)

#: status 的语义随要素类型而变，默认值必须按类型给：
#: claim 用 U17（GraphRAG）的 TRUE/FALSE/SUSPECTED/ARCHIVED；
#: source 用三、P2 的 active/archived；其余要素计划书未定义 status，留空。
DEFAULT_STATUS: dict[str, str] = {CLAIM: "TRUE", SOURCE: "active"}

#: 固定小节（U18）
SECTION_OBSERVATIONS = "Observations"
SECTION_RELATIONS = "Relations"
FIXED_SECTIONS: tuple[str, ...] = (SECTION_OBSERVATIONS, SECTION_RELATIONS)


class OntologyError(ValueError):
    """页面不符合本体约定。"""


# --------------------------------------------------------------------------- #
# 页面模型
# --------------------------------------------------------------------------- #
@dataclass
class WikiPage:
    """全部本体页面共有的字段。

    注意 user_id 的归属规则（计划书原文）：
    - 公共 Workspace 页面**不得**带 user_id（「素材溯源库属于 Agent-Wiki 公共 Workspace 无用户 ID」）；
    - 用户私有 Vault 页面**必须**带 user_id（「每个用户独立私有 Vault，绑定 user_id」）。
    """

    id: str
    type: str
    title: str = ""
    confidence: float | None = None
    status: str = ""                      # 语义随类型而定，见 DEFAULT_STATUS
    user_id: str | None = None            # 仅私有 Vault 页面填写
    created_at: str = ""
    updated_at: str = ""
    #: U16：矛盾时不删旧声明，改为写入失效时间
    invalid_at: str | None = None
    #: U16：与之矛盾的声明/关系 id
    contradicts: list[str] = field(default_factory=list)
    #: U17：ISO8601 有效期
    valid_from: str | None = None
    valid_to: str | None = None
    observations: list[str] = field(default_factory=list)
    relations: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in PAGE_TYPES:
            raise OntologyError(f"未知本体要素类型 {self.type!r}，允许：{PAGE_TYPES}")
        if not self.id or not str(self.id).strip():
            raise OntologyError("页面 id 不得为空")
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise OntologyError("置信度必须落在 0~1")

    # ---------------- frontmatter 互转 ----------------
    _SCALARS = ("id", "type", "title", "confidence", "status", "user_id",
                "created_at", "updated_at", "invalid_at", "valid_from", "valid_to",
                "contradicts")

    def to_frontmatter(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key in self._SCALARS:
            value = getattr(self, key)
            if value in (None, "", []):
                continue
            data[key] = value
        for key, value in self.extra.items():
            data.setdefault(key, value)
        return data

    @classmethod
    def from_frontmatter(cls, data: dict[str, Any], body_sections: dict[str, list[str]]) -> "WikiPage":
        known = set(cls._SCALARS) | {"observations", "relations", "extra"}
        kwargs: dict[str, Any] = {
            "id": str(data.get("id", "")),
            "type": str(data.get("type", "")),
            "title": data.get("title", "") or "",
            "confidence": data.get("confidence"),
            "status": data.get("status", "") or "",
            "user_id": data.get("user_id"),
            "created_at": data.get("created_at", "") or "",
            "updated_at": data.get("updated_at", "") or "",
            "invalid_at": data.get("invalid_at"),
            "contradicts": list(data.get("contradicts") or []),
            "valid_from": data.get("valid_from"),
            "valid_to": data.get("valid_to"),
            "observations": list(body_sections.get(SECTION_OBSERVATIONS, [])),
            "relations": list(body_sections.get(SECTION_RELATIONS, [])),
            "extra": {k: v for k, v in data.items() if k not in known},
        }
        return cls(**kwargs)

    def to_markdown(self) -> str:
        """渲染为人类可读 Markdown 页面（U18：frontmatter + 固定小节）。"""
        post = frontmatter.Post("", **self.to_frontmatter())
        lines = [f"# {self.title or self.id}", ""]
        lines.append(f"## {SECTION_OBSERVATIONS}")
        lines += [f"- {item}" for item in self.observations] or ["-"]
        lines.append("")
        lines.append(f"## {SECTION_RELATIONS}")
        lines += [f"- {item}" for item in self.relations] or ["-"]
        lines.append("")
        post.content = "\n".join(lines)
        return frontmatter.dumps(post)

    @classmethod
    def parse(cls, text: str) -> "WikiPage":
        post = frontmatter.loads(text)
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for raw in post.content.splitlines():
            line = raw.rstrip()
            if line.startswith("## "):
                current = line[3:].strip()
                sections.setdefault(current, [])
            elif current and line.startswith("- "):
                item = line[2:].strip()
                if item:
                    sections[current].append(item)
        return cls.from_frontmatter(dict(post.metadata), sections)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 各本体要素的专有字段（按需校验，避免不同要素之间字段串味）
# --------------------------------------------------------------------------- #
REQUIRED_EXTRA: dict[str, tuple[str, ...]] = {
    ENTITY: ("name", "entity_type"),
    SOURCE: ("url",),                       # 三、P2「保存来源元数据」
    CLAIM: ("claim_type", "description", "subject_id", "object_id"),   # U17
    EVIDENCE: ("source_id", "source_text", "text_unit_id"),            # U17
    RELATION: ("subject_id", "predicate", "object_id"),
    CONTRADICTION: ("left_id", "right_id", "reason"),
    QUESTION: ("text",),
    SYNTHESIS: ("summary_of",),
}


def validate(page: WikiPage) -> None:
    """要素专有字段 + 状态取值校验。"""
    missing = [k for k in REQUIRED_EXTRA[page.type] if not page.extra.get(k)]
    if missing:
        raise OntologyError(f"{page.type} 页面缺少必填字段：{missing}")
    if page.type in DEFAULT_STATUS and not page.status:
        raise OntologyError(
            f"{page.type} 页面必须给出 status，取值 {tuple(DEFAULT_STATUS[page.type].split()) or ''}"
            f"（claim 见 U17；source 见三、P2）"
        )
    if page.type == CLAIM and page.status not in CLAIM_STATUSES:
        raise OntologyError(f"claim status 必须是 {CLAIM_STATUSES}，实际 {page.status!r}")
    if page.type == SOURCE and page.status not in SOURCE_STATUSES:
        raise OntologyError(f"source status 必须是 {SOURCE_STATUSES}，实际 {page.status!r}")


def new_page(page_type: str, page_id: str, *, title: str = "", confidence: float | None = None,
             user_id: str | None = None, now: str = "", status: str | None = None,
             **extra: Any) -> WikiPage:
    page = WikiPage(
        id=page_id, type=page_type, title=title, confidence=confidence, user_id=user_id,
        created_at=now, updated_at=now,
        status=status if status is not None else DEFAULT_STATUS.get(page_type, ""),
        extra=dict(extra),
    )
    validate(page)
    return page
