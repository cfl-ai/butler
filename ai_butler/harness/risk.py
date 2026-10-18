"""P10 风险分级 —— 把表 10 的三条红线落成可判定、可测试的规则。

计划书依据
----------
- 三、P10「所有生产变更风险分级，**高危操作强制人工审批**」
- 表 10「Hook 安全校验：**禁止移除 user_id 行级过滤与鉴权**；
  **禁止读取未脱敏私有 Vault**；**修改 Jev 模板、Wiki 公共知识库大规模变更强制人工审批**」
- 三、P10 离线链路流程「Hook + 质检风险校验 → 沙箱执行代码修改与自测」

裁定 U33
--------
表注给了三条红线，**未给出分级刻度**。本模块把红线落成三档，
并且 **红线一律判 high，不设「紧急通道」**——这是刻意的：
一旦允许绕过，红线就不再是红线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: 风险档位（裁定 U33）
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_LEVELS: tuple[str, ...] = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

#: 红线（表 10 逐条；**命中即 high**）
REDLINE_ROW_LEVEL_FILTER = "移除 user_id 行级过滤或鉴权"
REDLINE_UNREDACTED_VAULT = "读取未脱敏私有 Vault"
REDLINE_JEV_TEMPLATE = "修改 Jev 决策模板"
REDLINE_WIKI_BULK = "Wiki 公共知识库大规模变更"
REDLINES: tuple[str, ...] = (REDLINE_ROW_LEVEL_FILTER, REDLINE_UNREDACTED_VAULT,
                             REDLINE_JEV_TEMPLATE, REDLINE_WIKI_BULK)

# 表 10 的措辞分成两类，**处置方式不同，不能混为一谈**：
#   「**禁止**移除行级过滤与鉴权；**禁止**读取未脱敏私有 Vault」→ 直接拒绝
#   「修改 Jev 模板、Wiki 大型变更**强制人工审批**」→ 走审批，不是拒绝
# 一开始我把四条都按「禁止」处理，结果**把 Jev 模板迭代这条规定流程整个堵死**了 ——
# 比计划书更严并不等于更对。

#: 「禁止」类：命中即拒绝，**不进审批**
FORBIDDEN_REDLINES: tuple[str, ...] = (REDLINE_ROW_LEVEL_FILTER,
                                       REDLINE_UNREDACTED_VAULT)
#: 「强制人工审批」类：命中即需审批，**不自动放行**
APPROVAL_REDLINES: tuple[str, ...] = (REDLINE_JEV_TEMPLATE, REDLINE_WIKI_BULK)

#: 「大规模变更」的页数阈值（实现参数；表注只说「大规模」，未给数值）
WIKI_BULK_THRESHOLD = 20

#: 触及核心模块的多文件变更升为 medium 的阈值（实现参数）
MANY_FILES_THRESHOLD = 8

#: 行级过滤 / 鉴权的代码标记。用于扫描 diff 是否**删掉了**这些保护。
ROW_LEVEL_MARKERS: tuple[str, ...] = (
    "user_id", "user_scope", "row_level", "行级隔离", "tenant",
)
AUTH_MARKERS: tuple[str, ...] = (
    "require_user", "login_status", "authenticate", "鉴权", "session_token",
    "verify_token", "permission",
)


class RiskError(ValueError):
    pass


@dataclass(frozen=True)
class Change:
    """一次待评估的生产变更。"""

    change_id: str
    description: str = ""
    #: 变更涉及的文件
    files: tuple[str, ...] = ()
    #: 统一 diff 文本（用于扫描「是否删掉了保护」）
    diff: str = ""
    #: 该变更要读取的 Wiki 作用域
    wiki_scopes: tuple[str, ...] = ()
    #: 是否已脱敏（读私有 Vault 时**必须**为真）
    redacted: bool = True
    #: 是否修改 Jev 决策模板
    modifies_jev_template: bool = False
    #: 本次写入公共知识库的页数
    wiki_pages_written: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.change_id:
            raise RiskError("变更必须有 change_id")


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    redlines: tuple[str, ...]
    reasons: tuple[str, ...]
    requires_approval: bool
    forbidden: bool = False        # 命中「禁止」类红线 → 直接拒绝

    @property
    def is_high(self) -> bool:
        return self.level == RISK_HIGH

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "redlines": list(self.redlines),
                "reasons": list(self.reasons), "forbidden": self.forbidden,
                "requires_approval": self.requires_approval}


def _removed_protections(diff: str) -> list[str]:
    """扫描 diff 中**被删除**的保护性代码行。

    只看删除行（以 ~{-}~ 开头）——**新增保护不算风险，删除才算**。
    """
    if not diff:
        return []
    hits: list[str] = []
    for raw in diff.splitlines():
        line = raw.strip()
        if not line.startswith("-") or line.startswith("---"):
            continue
        body = line[1:]
        lowered = body.lower()
        for marker in ROW_LEVEL_MARKERS:
            if marker.lower() in lowered:
                hits.append(REDLINE_ROW_LEVEL_FILTER)
                break
        for marker in AUTH_MARKERS:
            if marker.lower() in lowered:
                hits.append(REDLINE_ROW_LEVEL_FILTER)
                break
    return hits


def detect_redlines(change: Change) -> tuple[str, ...]:
    """判定命中了哪些红线。"""
    found: list[str] = []

    if _removed_protections(change.diff):
        found.append(REDLINE_ROW_LEVEL_FILTER)

    # 读私有 Vault：只有**已脱敏**才不触线
    for scope in change.wiki_scopes:
        if scope in ("vault", "private_vault") and not change.redacted:
            found.append(REDLINE_UNREDACTED_VAULT)
            break

    if change.modifies_jev_template:
        found.append(REDLINE_JEV_TEMPLATE)

    if change.wiki_pages_written >= WIKI_BULK_THRESHOLD:
        found.append(REDLINE_WIKI_BULK)

    # 去重但保持 REDLINES 的稳定顺序
    return tuple(r for r in REDLINES if r in found)


def assess(change: Change, *, wiki_bulk_threshold: int = WIKI_BULK_THRESHOLD,
           many_files_threshold: int = MANY_FILES_THRESHOLD) -> RiskAssessment:
    """给一次变更定档。**命中任一红线即 high，没有例外通道。**"""
    redlines = detect_redlines(change)
    reasons: list[str] = []
    if redlines:
        forbidden = any(r in FORBIDDEN_REDLINES for r in redlines)
        for r in redlines:
            reasons.append(
                f"命中红线（{'禁止' if r in FORBIDDEN_REDLINES else '强制人工审批'}）：{r}")
        return RiskAssessment(RISK_HIGH, redlines, tuple(reasons),
                              requires_approval=True, forbidden=forbidden)

    if change.wiki_pages_written >= max(1, wiki_bulk_threshold // 2):
        reasons.append(f"写入公共知识库 {change.wiki_pages_written} 页，接近大规模阈值")
    if len(change.files) >= many_files_threshold:
        reasons.append(f"涉及 {len(change.files)} 个文件，影响面较大")
    if not change.redacted:
        reasons.append("变更基于未脱敏内容（但未触私有 Vault 红线）")

    if reasons:
        return RiskAssessment(RISK_MEDIUM, (), tuple(reasons),
                              requires_approval=False)
    return RiskAssessment(RISK_LOW, (), ("常规缺陷修复或测试补充",),
                          requires_approval=False)
