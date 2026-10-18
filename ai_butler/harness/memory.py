"""P10 MEMORY.md 持久记忆沉淀（计划书点名要求）。

计划书依据
----------
- 三、P10 步骤9「搭建 **MEMORY.md 持久记忆沉淀机制**，存储缺陷与解决方案；
  **不存储真实用户隐私**，记录 **Jev 失败 case、Wiki 典型矛盾案例**」
- 三、P10 核心架构「集成…**持久记忆体系**」

注意
----
MEMORY.md 是**计划书点名要求**的产物，因此它在「md 产出白名单」之内。
本模块只负责**结构化条目的渲染与追加**；真实用户隐私一律不得进入——
记录的是「缺陷与解决方案」，不是「谁遇到了什么」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ai_butler.evaluation.redaction import Redactor

#: 记忆类别（计划书点名后两类）
MEMORY_DEFECT = "defect"
MEMORY_JEV_FAILURE = "jev_failure"
MEMORY_WIKI_CONFLICT = "wiki_conflict"
MEMORY_KINDS: tuple[str, ...] = (MEMORY_DEFECT, MEMORY_JEV_FAILURE,
                                 MEMORY_WIKI_CONFLICT)

#: 条目分隔标记，便于解析回读
ENTRY_PREFIX = "### "


class MemoryError(ValueError):
    pass


@dataclass(frozen=True)
class MemoryEntry:
    kind: str
    key: str
    title: str
    problem: str
    solution: str
    tags: tuple[str, ...] = ()
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if self.kind not in MEMORY_KINDS:
            raise MemoryError(f"未知记忆类别 {self.kind!r}，可选 {list(MEMORY_KINDS)}")
        if not self.key.strip():
            raise MemoryError("记忆条目必须有稳定 key")
        if not self.problem.strip() or not self.solution.strip():
            raise MemoryError("记忆条目必须同时记录「问题」与「解决方式」")

    def render(self, redactor: Redactor) -> str:
        """渲染成 Markdown 片段。**出口脱敏**，防止手滑把案例里的身份写进去。"""
        lines = [f"{ENTRY_PREFIX}{self.key}｜{self.title}", "",
                 f"- **类别**：{self.kind}"]
        if self.recorded_at:
            lines.append(f"- **记录时间**：{self.recorded_at}")
        if self.tags:
            lines.append("- **标签**：" + "、".join(self.tags))
        lines += ["", f"- **问题**：{self.problem}", f"- **处理方式**：{self.solution}", ""]
        block = "\n".join(lines)
        safe = redactor.redact(block)
        redactor.assert_clean(safe, where=f"记忆条目 {self.key}")
        return safe


@dataclass
class MemoryStore:
    path: Path
    redactor: Redactor = field(default_factory=Redactor)

    def render(self, entries: Sequence[MemoryEntry]) -> str:
        return "\n".join(e.render(self.redactor) for e in entries)

    def append(self, entries: Sequence[MemoryEntry], *,
               heading: str = "## 自进化沉淀（Harness 自动追加）") -> int:
        """追加条目到 MEMORY.md。返回追加的条目数。"""
        if not entries:
            return 0
        block = self.render(entries)
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        if heading not in text:
            text = text.rstrip() + "\n\n---\n\n" + heading + "\n\n"
        text = text.rstrip() + "\n\n" + block + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        return len(entries)

    def keys(self) -> tuple[str, ...]:
        """已沉淀条目的 key，用于去重（同一个坑不重复记）。"""
        if not self.path.exists():
            return ()
        found: list[str] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.startswith(ENTRY_PREFIX):
                head = line[len(ENTRY_PREFIX):].split("｜")[0].strip()
                if head:
                    found.append(head)
        return tuple(found)

    def has(self, key: str) -> bool:
        return key in self.keys()


def from_qa_report(*, report: dict[str, Any], recorded_at: str = "") -> list[MemoryEntry]:
    """把 P9 的评估报告转成待沉淀记忆。

    **只沉淀「模式」，不沉淀「个案」**：记的是「Jev 在某类意图上易错」，
    而不是「某个用户被误判成了什么」。
    """
    out: list[MemoryEntry] = []
    jev = report.get("jev") or {}
    errors = int(jev.get("error_cases") or 0)
    if errors:
        out.append(MemoryEntry(
            kind=MEMORY_JEV_FAILURE, key="jev-error-pattern",
            title="Jev 决策错误累积",
            problem=f"本周期检测到 {errors} 条 Jev 决策错误案例",
            solution="用这批案例更新 Jev Question 模板候选集，交由人工审批后生效",
            tags=("jev",), recorded_at=recorded_at))
    wiki = report.get("wiki") or {}
    conflicts = int(wiki.get("conflict_cases") or 0)
    if conflicts:
        out.append(MemoryEntry(
            kind=MEMORY_WIKI_CONFLICT, key="wiki-conflict-pattern",
            title="Wiki 典型矛盾案例",
            problem=f"本周期检测到 {conflicts} 条 Wiki 矛盾案例",
            solution="经 Agent-Wiki 维护 Skill 清理公共 Workspace 冲突并归档过期来源",
            tags=("wiki",), recorded_at=recorded_at))
    return out
