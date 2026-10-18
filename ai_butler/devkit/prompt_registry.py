"""P0-2 Prompt 版本库。

计划书依据
----------
- 三、P0-2「搭建 Prompt 版本库：保存、对比、回滚、A/B 测试；
  同步管理 Jev 决策 Question 模板版本」

设计要点
--------
- 每个版本一个 JSON 文件，落盘在统一文件存储的 prompts 作用域（全局资产，非用户私有）；
- 版本号单调递增，**回滚不删历史**，而是生成一个内容等于旧版本的新版本；
- A/B 分流按 user_id 稳定哈希，同一用户在实验期内始终落在同一组；
- 分流的流量比例计划书未定义 → 作为实现参数默认均分，可由调用方覆盖。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ai_butler.core.storage import SCOPE_PROMPTS, FileStore

#: Jev 决策 Question 模板的命名前缀（计划书三、P0-2 要求同步管理其版本）
JEV_TEMPLATE_PREFIX = "jev.question."

#: A/B 默认分流比例（计划书未定义 → 实现参数）
DEFAULT_AB_SPLIT: dict[str, int] = {"A": 1, "B": 1}


class PromptError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptVersion:
    name: str
    version: int
    template: str
    note: str
    created_at: str
    sha256: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    rolled_back_from: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def of(name: str, version: int, template: str, note: str, created_at: str,
           tags: Sequence[str] = (), rolled_back_from: int | None = None) -> "PromptVersion":
        return PromptVersion(
            name=name, version=version, template=template, note=note,
            created_at=created_at, tags=tuple(tags),
            sha256=hashlib.sha256(template.encode("utf-8")).hexdigest(),
            rolled_back_from=rolled_back_from,
        )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PromptRegistry:
    def __init__(self, store: FileStore) -> None:
        self.store = store

    # ---------------- 路径 ----------------
    def _dir(self, name: str) -> Path:
        return self.store.path(SCOPE_PROMPTS, name)

    def _version_path(self, name: str, version: int) -> Path:
        return self._dir(name) / f"v{version:04d}.json"

    def _index(self, name: str) -> list[int]:
        base = self._dir(name)
        if not base.is_dir():
            return []
        return sorted(int(p.stem[1:]) for p in base.glob("v*.json"))

    # ---------------- 保存 ----------------
    def save(self, name: str, template: str, *, note: str = "",
             tags: Sequence[str] = (), rolled_back_from: int | None = None) -> PromptVersion:
        if not name or not name.strip():
            raise PromptError("Prompt 名不得为空")
        if not template:
            raise PromptError("Prompt 内容不得为空")
        versions = self._index(name)
        version = (versions[-1] + 1) if versions else 1
        record = PromptVersion.of(name, version, template, note, _now(), tags, rolled_back_from)
        path = self._version_path(name, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    def save_jev_question(self, question_name: str, template: str, *, note: str = "",
                          tags: Sequence[str] = ()) -> PromptVersion:
        """Jev 决策 Question 模板：强制走统一前缀，便于版本集中管理。"""
        return self.save(f"{JEV_TEMPLATE_PREFIX}{question_name}", template, note=note, tags=tags)

    # ---------------- 读取 ----------------
    def get(self, name: str, version: int | None = None) -> PromptVersion:
        versions = self._index(name)
        if not versions:
            raise PromptError(f"Prompt 不存在：{name!r}")
        target = versions[-1] if version is None else version
        if target not in versions:
            raise PromptError(f"版本不存在：{name!r} v{target}")
        raw = json.loads(self._version_path(name, target).read_text(encoding="utf-8"))
        return PromptVersion(
            name=raw["name"], version=raw["version"], template=raw["template"],
            note=raw["note"], created_at=raw["created_at"], sha256=raw["sha256"],
            tags=tuple(raw.get("tags", ())), rolled_back_from=raw.get("rolled_back_from"))

    def history(self, name: str) -> list[PromptVersion]:
        return [self.get(name, v) for v in self._index(name)]

    def names(self) -> list[str]:
        base = self.store.scope_dir(SCOPE_PROMPTS)
        if not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())

    def jev_question_names(self) -> list[str]:
        return [n for n in self.names() if n.startswith(JEV_TEMPLATE_PREFIX)]

    # ---------------- 对比 ----------------
    def diff(self, name: str, from_version: int, to_version: int) -> str:
        a = self.get(name, from_version)
        b = self.get(name, to_version)
        return "".join(difflib.unified_diff(
            a.template.splitlines(keepends=True),
            b.template.splitlines(keepends=True),
            fromfile=f"{name}/v{a.version:04d}",
            tofile=f"{name}/v{b.version:04d}",
        ))

    # ---------------- 回滚 ----------------
    def rollback(self, name: str, version: int, *, note: str = "") -> PromptVersion:
        """回滚到指定版本——**不删除历史**，而是生成内容等于该版本的新版本。"""
        target = self.get(name, version)
        return self.save(name, target.template,
                         note=note or f"回滚到 v{version:04d}",
                         tags=target.tags, rolled_back_from=version)

    # ---------------- A/B ----------------
    def ab_assign(self, name: str, *, user_id: str,
                  split: dict[str, int] | None = None) -> str:
        """按 user_id 稳定分流。同一用户在实验期内始终落在同一组。"""
        if not user_id or not str(user_id).strip():
            raise PromptError("A/B 分流必须携带 user_id（计划书：禁止不带用户身份的裸访问）")
        weights = dict(split or DEFAULT_AB_SPLIT)
        if not weights or any(w <= 0 for w in weights.values()):
            raise PromptError(f"分流权重必须为正数：{weights}")
        total = sum(weights.values())
        digest = hashlib.sha256(f"{name}:{user_id}".encode("utf-8")).digest()
        point = int.from_bytes(digest[:8], "big") % total
        cursor = 0
        for variant in sorted(weights):            # 排序保证结果稳定
            cursor += weights[variant]
            if point < cursor:
                return variant
        return sorted(weights)[-1]

    def ab_versions(self, name: str, *, variants: dict[str, int] | None = None) -> dict[str, PromptVersion]:
        """把 A/B 组映射到具体版本。默认取最近两个版本。"""
        versions = self._index(name)
        if len(versions) < 2:
            raise PromptError(f"{name!r} 至少需要两个版本才能做 A/B 对比")
        mapping = variants or {"A": versions[-2], "B": versions[-1]}
        return {k: self.get(name, v) for k, v in mapping.items()}
