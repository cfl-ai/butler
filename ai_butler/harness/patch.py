"""P10 补丁机械层 —— 让「自动改代码」成为可回滚、可审计的动作。

计划书依据
----------
- 三、P10 步骤1「搭建基础代码修改沙箱，支持代码修改、命令执行、单元测试」
- 三、P10 离线链路「Hook + 质检风险校验 → **沙箱执行代码修改与自测** →
  评测迭代效果，未达标持续循环修复」
- 三、P10 触发时机「7×24 小时后台离线自动运行」——**无人值守意味着
  每一次改动都必须自带回滚路径**，不能指望有人盯着

为什么这一层刻意不含任何智能
----------------------------
上一轮把补丁应用器如实登记为「未实现」，理由是不能假装能改源码。
这一层补上那个缺口，但它**不调用模型、不猜意图、不修正模型写错的定位**。
它只做四件事：解析、校验范围、应用、回滚。

分层的原因是最坏的失败模式决定的：
**模型写了一个看起来对、但位置错三行的 diff，被静默应用上去。**
这比直接报错停下坏得多——报错会被人看见，改坏的地方不会。
所以这里的容错方向是反的：宁可 apply 失败让模型重来，
也不做「聪明的模糊匹配」。

三条结构性防线（都不依赖模型自觉）
----------------------------------
1. **范围白名单按缺陷类别限定到子系统**。修检索召回的补丁只能落在
   `ai_butler/retrieval/`，改不到别处。这不是信任问题，是权限问题。
2. **`tests/` 与 `ai_butler/evaluation/` 全局禁改**。
   前者能让「删掉断言」变成「测试全绿」，后者是**让被评的缺陷去改判据**——
   改完测试是绿的、缺陷还在，正是表 10 第一条红线要防的那件事。
3. **改动规模硬上限**。超过阈值说明这不是修复而是重写，重写该走人工。
   数值是实现参数（计划书未给刻度），取值理由写在 `PatchGuard` 里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from ai_butler.harness.risk import Change

__all__ = [
    "PatchError",
    "Hunk",
    "FilePatch",
    "ParsedPatch",
    "PatchGuard",
    "PatchApplier",
    "PatchResult",
    "ALLOWED_SCOPE_BY_KIND",
    "FORBIDDEN_PATHS",
    "parse_unified_diff",
]


class PatchError(ValueError):
    """补丁不合法 / 无法安全应用。**一律不应用**，不降级、不猜。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Hunk:
    """一个 `@@` 块。`lines` 保留原始前缀（' ' / '+' / '-'）。"""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]

    @property
    def old_lines(self) -> tuple[str, ...]:
        """上下文行 + 删除行（去掉前缀）——应用时要精确匹配的原文。"""
        return tuple(ln[1:] for ln in self.lines if ln[:1] in (" ", "-"))

    @property
    def new_lines(self) -> tuple[str, ...]:
        """上下文行 + 新增行（去掉前缀）——替换后的内容。"""
        return tuple(ln[1:] for ln in self.lines if ln[:1] in (" ", "+"))

    @property
    def touched(self) -> int:
        """本块改动的行数（新增 + 删除），用于规模校验。"""
        return sum(1 for ln in self.lines if ln[:1] in ("+", "-"))


@dataclass(frozen=True)
class FilePatch:
    """单个文件的补丁。`path` 已规范化为仓库相对路径（正斜杠、无 a/ b/ 前缀）。"""

    path: str
    hunks: tuple[Hunk, ...]

    @property
    def touched(self) -> int:
        return sum(h.touched for h in self.hunks)


@dataclass(frozen=True)
class ParsedPatch:
    """一份完整补丁。`raw` 保留原文，供审计与落账（审计要看原始文本）。"""

    files: tuple[FilePatch, ...]
    raw: str = ""

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    @property
    def touched(self) -> int:
        return sum(f.touched for f in self.files)

    def is_empty(self) -> bool:
        return not self.files or self.touched == 0


# --------------------------------------------------------------------------- #
# 范围白名单
# --------------------------------------------------------------------------- #
#: **全局禁改**，与缺陷类别无关。
#:
#: `tests/` —— 允许模型改测试，它最省力的「修复」就是删掉失败断言，
#:            于是测试全绿而缺陷原样存在。这正是「胡乱修改」的典型形态。
#: `ai_butler/evaluation/` —— 评测判据。让被评的一方改判据，
#:            等于让被告改判决书（表 10 第一条红线的同一逻辑）。
#: 门禁自身（risk / hooks / patch / permissions / decisions）—— 同理。
FORBIDDEN_PATHS: tuple[str, ...] = (
    "tests/",
    "ai_butler/evaluation/",
    "ai_butler/harness/hooks.py",
    "ai_butler/harness/risk.py",
    "ai_butler/harness/patch.py",
    "ai_butler/harness/intake.py",
    "ai_butler/core/permissions.py",
    "ai_butler/core/decisions.py",
    "ai_butler/core/config.py",
    ".github/",
    "deploy/",
    "mobile/",
    "docs/",
)

#: 缺陷类别 → 允许改动的路径前缀。**未登记的类别一律不受理**
#: （与 `intake._VERIFY_TARGETS` 同一立场：不登记就不给机会）。
#:
#: 收窄到子系统的理由：这张表同时是「改动范围」的判据，
#: 越界即是「胡乱修改」——不需要模型自证，路径本身就说明了问题。
ALLOWED_SCOPE_BY_KIND: dict[str, tuple[str, ...]] = {
    "recall_gap": ("ai_butler/retrieval/",),
    "missing_evidence": ("ai_butler/retrieval/",),
    "quality_gap": ("ai_butler/retrieval/",),
    "false_tool_call": ("ai_butler/retrieval/planner.py", "ai_butler/scheduler/"),
    "unstable": ("ai_butler/scheduler/",),
    "cost_regression": ("ai_butler/retrieval/", "ai_butler/harness/context.py"),
}

#: 单个补丁最多涉及的文件数（实现参数）。
#: 理由：一次缺陷修复动 4 个以上文件，说明缺陷定位本身就不成立，
#: 该退回人工而不是让模型继续猜。
MAX_FILES = 3

#: 单个补丁最多改动的行数（新增+删除，实现参数）。
#: 理由：离线链路无人值守，200 行以上的改动无法在审批时被人有效读懂；
#: 「读不懂的自动变更」和「没审批」是一回事。
MAX_TOUCHED_LINES = 200


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")

#: 模型常见的「礼貌性包装」：```diff 围栏。剥掉而不是报错——
#: 报错会让它再犯一次同样的错，白烧一轮 token。
_FENCE_RE = re.compile(r"^\s*```(?:diff|patch)?\s*$")


def normalize_path(raw: str) -> str:
    """把 diff 头里的路径规范成仓库相对路径。

    模型会写 `a/x.py`、`b/x.py`、`./x.py`、`x\\y.py`、`/abs/x.py`——
    这些都必须归一到同一个键，否则白名单会漏判（漏判即防线失效）。
    """
    path = raw.strip().replace("\\", "/")
    # 去掉 `--- a/x.py\t2026-01-01` 这类时间戳后缀
    path = path.split("\t")[0].strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    # `/dev/null` 必须**先判**：下面的 lstrip("/") 会把它变成 "dev/null"，
    # 于是「删除文件」被当成改一个叫 dev/null 的普通文件放行。
    if path in ("/dev/null", "dev/null"):
        return ""
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    path = path.lstrip("/")
    if not path:
        return ""
    # 路径穿越：`../` 一律拒绝（`_resolve` 还会再验一次落点）
    if ".." in Path(path).parts:
        raise PatchError(f"补丁路径含上跳（..），拒绝：{raw!r}")
    return path


def parse_unified_diff(text: str) -> ParsedPatch:
    """解析统一 diff。**任何歧义都报错**，不做「尽力解析」。

    失败比误解析好：误解析的后果是把补丁应用到错误的位置，
    而错误位置往往仍是合法 Python——测试可能过了，逻辑已经坏了。
    """
    if not text or not text.strip():
        raise PatchError("空补丁")

    raw_lines = [ln for ln in text.splitlines() if not _FENCE_RE.match(ln)]
    files: list[FilePatch] = []
    cur_path = ""
    cur_hunks: list[Hunk] = []
    pending_old = ""          # 已读到 `---`，等 `+++`
    i = 0
    n = len(raw_lines)

    def _flush() -> None:
        nonlocal cur_path, cur_hunks
        if cur_path:
            if not cur_hunks:
                raise PatchError(f"{cur_path} 没有任何 @@ 块")
            files.append(FilePatch(cur_path, tuple(cur_hunks)))
        cur_path, cur_hunks = "", []

    while i < n:
        line = raw_lines[i]
        if line.startswith("--- "):
            _flush()
            pending_old = normalize_path(line[4:])
            i += 1
            continue
        if line.startswith("+++ "):
            new_path = normalize_path(line[4:])
            if not new_path:
                raise PatchError(f"不支持删除文件（+++ /dev/null）：{pending_old!r}")
            cur_path = new_path
            i += 1
            continue
        m = _HUNK_RE.match(line)
        if m:
            if not cur_path:
                raise PatchError("@@ 块出现在 +++ 之前")
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            body: list[str] = []
            i += 1
            while i < n:
                cur = raw_lines[i]
                if cur.startswith("@@") or cur.startswith("--- "):
                    break
                if cur.startswith("+++ "):
                    break
                if cur[:1] in (" ", "+", "-"):
                    body.append(cur)
                elif cur.startswith("\\"):      # \ No newline at end of file
                    body.append(" ")            # 归一为空上下文行
                elif cur.strip() == "":
                    body.append(" ")            # 空行按上下文行处理
                else:
                    raise PatchError(f"无法识别的 diff 行：{cur!r}")
                i += 1
            if not body:
                raise PatchError(f"{cur_path} 的 @@ 块为空")
            cur_hunks.append(Hunk(old_start, old_count, new_start, new_count,
                                  tuple(body)))
            continue
        if not line.strip():
            i += 1
            continue
        raise PatchError(f"diff 中存在游离内容：{line!r}")

    _flush()
    if not files:
        raise PatchError("未解析到任何文件补丁")
    return ParsedPatch(tuple(files), text)


# --------------------------------------------------------------------------- #
# 范围校验
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PatchGuard:
    """改动范围校验。**违规即整份补丁作废**，不挑「能改的改」。

    `frozen_paths`（U81）是**跨轮**的边界：前几轮已经提交过补丁的文件，
    本轮不许再改。要改必须先撤销（撤销需人工确认，见 `harness/revision.py`）。

    为什么「同一文件再改一次」也要拦：第二轮补丁的上下文行是第一轮改过之后
    的内容，两份补丁叠在一起后，出问题时无法说清是哪一轮造成的。
    责任不可分割的自动化改动，比改错更难处理。
    """

    kind: str
    max_files: int = MAX_FILES
    max_touched: int = MAX_TOUCHED_LINES
    #: 已被前几轮提交的补丁改动过的文件（本轮不可再改，除非先撤销）
    frozen_paths: tuple[str, ...] = ()

    @property
    def allowed_prefixes(self) -> tuple[str, ...]:
        return ALLOWED_SCOPE_BY_KIND.get(self.kind, ())

    @classmethod
    def for_kind(cls, kind: str, **kw) -> "PatchGuard":
        return cls(kind=str(kind), **kw)

    def violations(self, patch: ParsedPatch) -> tuple[str, ...]:
        out: list[str] = []
        allowed = self.allowed_prefixes
        if not allowed:
            return (f"缺陷类别 {self.kind!r} 未登记可改动范围，不受理自动补丁",)

        if len(patch.files) > self.max_files:
            out.append(f"改动 {len(patch.files)} 个文件，超过上限 {self.max_files}"
                       f"（一次缺陷修复不该铺这么开）")
        if patch.touched > self.max_touched:
            out.append(f"改动 {patch.touched} 行，超过上限 {self.max_touched}"
                       f"（这是重写不是修复）")

        for f in patch.files:
            if any(f.path == p.rstrip("/") or f.path.startswith(p)
                   for p in FORBIDDEN_PATHS):
                out.append(f"{f.path} 属全局禁改路径"
                           f"（改测试或改评测判据会让缺陷「自己消失」）")
                continue
            if not any(f.path == p or f.path.startswith(p) for p in allowed):
                out.append(f"{f.path} 不在缺陷类别 {self.kind} 允许的范围 "
                           f"{list(allowed)} 内")
                continue
            # 跨轮边界（U81）。放在范围校验**之后**：先判「这块该不该你管」，
            # 再判「这块虽然归你管，但这轮归你改吗」——两个问题的处置不同，
            # 理由也必须不同（前者是越权，后者是等待撤销审批）。
            if any(f.path == p for p in self.frozen_paths):
                out.append(
                    f"{f.path} 已被前几轮提交的补丁改动过，本轮不可再改："
                    f"同一文件叠两份自动补丁会让责任无法分割。"
                    f"确实需要再改，必须先撤销上一轮（撤销需人工审批）")
        return tuple(out)

    def frozen_hits(self, patch: ParsedPatch) -> tuple[str, ...]:
        """补丁触碰到的跨轮锁定文件。空 = 没越界。

        单独一个方法而不是从 `violations()` 里过滤：调用方要区分
        「越权改别的板块」与「改了自己的板块但这轮归别人」，两者的
        处置完全不同（前者作废，后者等撤销审批）。从中文句子里筛
        ——那是 E-44 的老路。
        """
        return tuple(f.path for f in patch.files
                     if any(f.path == p for p in self.frozen_paths))

    def check(self, patch: ParsedPatch) -> None:
        bad = self.violations(patch)
        if bad:
            raise PatchError("补丁超出允许范围：" + "；".join(bad))


# --------------------------------------------------------------------------- #
# 应用与回滚
# --------------------------------------------------------------------------- #
#: 允许 hunk 位置与声明行号的最大偏差（实现参数）。
#:
#: 模型写 diff 时行号常差几行（它数的是自己眼里的片段，不是文件真实行号）。
#: 但**只接受 context 逐行完全相同的匹配**——位置可放宽，内容不放过。
CONTEXT_DRIFT_LIMIT = 50


@dataclass(frozen=True)
class PatchResult:
    """一次补丁应用的结果。**`ok=False` 时仓库必须已回到原状。**"""

    ok: bool
    paths: tuple[str, ...] = ()
    touched: int = 0
    reason: str = ""
    rolled_back: bool = False
    #: 应用**前**的各文件原文（None = 原本不存在）。`ok=True` 时才有内容。
    #:
    #: 它是「撤销一份已提交补丁」的唯一依据（U81）：`commit()` 之后
    #: `PatchApplier` 的备份就丢了，而撤销发生在更晚的时候。
    #: 刻意**不进 `as_dict()`**：那是源码正文，会顺着 State 一路传下去；
    #: 需要它的只有记账环节，由调用方直接读属性。
    before: Mapping[str, str | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "paths": list(self.paths), "touched": self.touched,
                "reason": self.reason, "rolled_back": self.rolled_back,
                # 只给计数：让人知道「有快照可撤销」，但不把源码塞进日志
                "before_paths": sorted(self.before)}


@dataclass
class PatchApplier:
    """在指定根目录上应用补丁，全程保留回滚能力。

    `root` 是**可配置的工作根**：生产指向项目根，验证时指向可丢弃的副本。
    复制一份最小工作区来验证「自动改代码」这件事本身是否成立，
    比在真仓库上试要安全——虽然两者都会备份。
    """

    root: Path
    #: 备份：路径 → 原内容（None 表示原本不存在）
    _backup: dict[str, str | None] = field(default_factory=dict, init=False)
    #: 最近一次应用的结果
    last: PatchResult | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    # ------------------------------------------------------------------ #
    def _resolve(self, rel: str) -> Path:
        target = (self.root / rel).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise PatchError(f"{rel} 解析到工作根之外，拒绝") from exc
        return target

    @staticmethod
    def _apply_hunks(original: str, fp: FilePatch) -> str:
        lines = original.splitlines(keepends=True)
        out: list[str] = []
        cursor = 0

        for idx, hunk in enumerate(fp.hunks):
            old_block = hunk.old_lines
            expected = max(0, hunk.old_start - 1)
            pos = _locate(lines, old_block, expected, cursor)
            if pos < 0:
                raise PatchError(
                    f"{fp.path} 第 {idx + 1} 个 @@ 块（声明行 {hunk.old_start}）"
                    f"在文件中找不到匹配的原文。期望首行："
                    f"{(old_block[0] if old_block else '<空>')!r}")
            out.extend(lines[cursor:pos])
            out.extend(_with_newlines(hunk.new_lines, lines, pos, old_block))
            cursor = pos + len(old_block)

        out.extend(lines[cursor:])
        return "".join(out)

    # ------------------------------------------------------------------ #
    def apply(self, patch: ParsedPatch | Change) -> PatchResult:
        """校验并落盘。**先全部算好再写**，避免半个补丁落地。

        接受 `Change` 或 `ParsedPatch`：调用方（loop）手上只有 `Change`，
        让它自己先解析会把「解析失败」这一支的处理逻辑散到两处。
        """
        if isinstance(patch, Change):
            try:
                patch = patch_from_change(patch)
            except PatchError as exc:
                self.last = PatchResult(False, reason=f"补丁无法解析：{exc}")
                return self.last

        if patch.is_empty():
            return PatchResult(False, reason="补丁为空（没有实际改动）")

        staged: list[tuple[str, Path, str, str]] = []
        try:
            for fp in patch.files:
                target = self._resolve(fp.path)
                existed = target.exists()
                original = target.read_text(encoding="utf-8") if existed else ""
                updated = self._apply_hunks(original, fp)
                if updated == original:
                    raise PatchError(f"{fp.path} 应用后内容未变化（空改动）")
                staged.append((fp.path, target, original if existed else None,
                               updated))
        except PatchError as exc:
            self.last = PatchResult(False, paths=patch.paths,
                                    touched=patch.touched, reason=str(exc))
            return self.last
        except OSError as exc:
            self.last = PatchResult(False, paths=patch.paths,
                                    touched=patch.touched,
                                    reason=f"读取失败：{exc}")
            return self.last

        written: list[str] = []
        try:
            for rel, target, original, updated in staged:
                self._backup[rel] = original
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8", newline="") as fh:
                    fh.write(updated)
                written.append(rel)
        except OSError as exc:
            # 写了一半：把已写的还原，保证「失败=原状」这个不变量
            for rel in written:
                self._restore(rel)
            self.last = PatchResult(False, paths=patch.paths, touched=patch.touched,
                                    reason=f"写入失败：{exc}", rolled_back=True)
            return self.last

        # 快照在这里打包，而不是让调用方从 `_backup` 里翻：`_backup` 是
        # 「还没提交」的状态，commit 后即清空；而撤销需要它在更晚的时候仍在。
        snapshot: dict[str, str | None] = {
            rel: original for rel, _target, original, _updated in staged}
        self.last = PatchResult(True, paths=patch.paths, touched=patch.touched,
                                reason="已应用", before=snapshot)
        return self.last

    # ------------------------------------------------------------------ #
    def _restore(self, rel: str) -> None:
        original = self._backup.pop(rel, None)
        target = self._resolve(rel)
        if original is None:
            if target.exists():
                target.unlink()
            return
        with target.open("w", encoding="utf-8", newline="") as fh:
            fh.write(original)

    def rollback(self) -> tuple[str, ...]:
        """还原所有改动过的文件。幂等——重复调用不会把文件改坏。"""
        restored = tuple(sorted(self._backup))
        for rel in restored:
            self._restore(rel)
        if self.last is not None and self.last.ok:
            self.last = PatchResult(False, paths=self.last.paths,
                                    touched=self.last.touched,
                                    reason=self.last.reason, rolled_back=True)
        return restored

    def commit(self) -> None:
        """确认保留改动，丢弃备份。此后 `rollback()` 不再能还原。"""
        self._backup.clear()

    @property
    def pending(self) -> tuple[str, ...]:
        """尚未 commit 的改动文件（测试用来断言「真的回滚了」）。"""
        return tuple(sorted(self._backup))


def _with_newlines(new_block: Sequence[str],
                   existing: Sequence[str], pos: int,
                   old_block: Sequence[str]) -> list[str]:
    """给新块的行补回换行符。

    `splitlines(keepends=True)` 得到的行自带换行；而 diff 里的 `+` 行不带。
    末行没有换行的情形（`\\ No newline at end of file`）这里统一按「跟随原文件」
    处理：原文件末行有换行就补，没有就不补。不做这个归一，
    应用出来的文件会少一个换行，下一次 diff 就永远对不上了。
    """
    out: list[str] = []
    for i, text in enumerate(new_block):
        src_index = pos + i if i < len(old_block) else pos + len(old_block) - 1
        has_nl = (src_index < len(existing)
                  and existing[src_index].endswith("\n"))
        out.append(text if text.endswith("\n") or not has_nl else text + "\n")
    return out


def _locate(lines: Sequence[str], old_block: Sequence[str],
            expected: int, cursor: int) -> int:
    """在 `expected` 附近找 old_block 的**逐行精确**匹配位置。

    只在 `[cursor, expected + CONTEXT_DRIFT_LIMIT]` 里找：
    - 不往前找：前面的行已被上一个 hunk 消费，回跳会造成重复插入；
    - 不无限往后找：模糊命中比找不到更危险。
    """
    if not old_block:
        return min(max(expected, cursor), len(lines))
    limit = min(len(lines) - len(old_block), max(expected, cursor) + CONTEXT_DRIFT_LIMIT)
    for pos in range(max(cursor, 0), limit + 1):
        if _block_matches(lines, old_block, pos):
            return pos
    # 最后再试一次「声明的精确位置」，覆盖 cursor 已被推后的情形
    if _block_matches(lines, old_block, expected) and expected >= cursor:
        return expected
    return -1


def _block_matches(lines: Sequence[str], block: Sequence[str], pos: int) -> bool:
    if pos < 0 or pos + len(block) > len(lines):
        return False
    for offset, want in enumerate(block):
        have = lines[pos + offset]
        if have.rstrip("\n") != want.rstrip("\n"):
            return False
    return True


def patch_from_change(change: Change) -> ParsedPatch:
    """从 `Change` 取补丁。空 diff 明确报错，而不是当成「无改动可应用」。"""
    if not change.diff.strip():
        raise PatchError(f"变更 {change.change_id} 没有 diff，无法应用")
    return parse_unified_diff(change.diff)
