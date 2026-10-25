"""缺陷指纹 —— 回答「跨轮重试时，错误信息到底变了没有」（U81）。

计划书依据
----------
- 三、P10 离线链路「评测迭代效果，**未达标持续循环修复**」——
  「持续循环」必须有个刹车：循环的条件是「情况在变」，不是「次数没到」
- 用户指令（2026-10-25）：「跨轮重试要查验**错误信息已更改**」

为什么必须单独判这一件事
------------------------
多轮重试有两种失败形态，看起来完全不同，代价也不一样：

1. **原地打转**：补丁改了一个地方，缺陷证据一个字都没变，下一轮又改一遍。
   跑满 3 轮、烧掉 3 次模型调用与 3 次全量评测，最后交出一份
   「试过了，没辙」——但它从来没真的推进过，只是重复了三次同一个动作。
2. **真推进但没修完**：证据变了（少了一条、或从「召回 2/5」变成「召回 4/5」），
   该继续。

这两种情况的处置完全相反。**只看「还有没有缺陷」是分不出来的**——
两种情况都有缺陷。所以需要一个能比「错误信息形态」的判据。

归一化：为什么不能直接比原文
----------------------------
缺陷证据里混着会自然抖动的量：

    「仍不通过 3 个用例」   vs  「仍不通过 2 个用例」
    「耗时 1284ms 超阈值」  vs  「耗时 1197ms 超阈值」

这些抖动**不代表缺陷形态变了**。直接比原文会把它们当成「有进展」，
于是闭环每轮都判定「变了，继续」，等于没有刹车。

所以比较用**归一化文本**：数字、十六进制、ISO 时间一律替换成占位符。
`exact`（原文摘要）同时保留，用于追溯「当时到底是哪句话」。

这个取舍的方向是明确的：**把抖动当进展**会让闭环无限空转（严重），
**把微小进展当没进展**只是提前转人工（可恢复）。所以归一化朝保守走。

不归一化什么
------------
路径、标识符、中文措辞**一律保留**。它们才是「问题在哪」的信息：

    `ai_butler/retrieval/planner.py`  → 改成别的文件 = 缺陷换了地方，这是真变化
    `SMALL_TALK`                      → 改成 `UNKNOWN` = 分类结论变了，真变化

归一化只吃掉「同一件事的不同量级」，不吃「不同的事」。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "DefectFingerprint",
    "FingerprintDiff",
    "FingerprintEntry",
    "fingerprint_of",
    "normalize_evidence",
]

#: 归一化占位符。取不会出现在正常中文/代码里的形态，避免与真实内容撞车。
_NUM = "#n"
_HEX = "#h"
_TIME = "#t"

_ISO_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
                       r"(?:Z|[+-]\d{2}:?\d{2})?")
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: 归一化后参与比较的文本长度上限。证据偶尔会带整段 diff，
#: 全量比较没必要，前这么多字符已经覆盖了「哪一句在说问题」。
_COMPARE_CHARS = 400


def normalize_evidence(text: str) -> str:
    """把证据文本归一成「可比形态」。

    顺序不能换：先吃掉时间（它包含数字），再吃十六进制，最后吃裸数字。
    反过来会把 `2026-10-25T10:00` 切成 `#n-#n-#nT#n:#n`——
    虽然可比，但丢了「这是一个时间戳」的信息，人读溯源记录时会困惑。
    """
    out = _ISO_TIME.sub(_TIME, str(text or ""))
    out = _HEX_TOKEN.sub(_HEX, out)
    out = _NUMBER.sub(_NUM, out)
    return " ".join(out.split())[:_COMPARE_CHARS]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 一条缺陷的指纹
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FingerprintEntry:
    """一条缺陷的「错误信息形态」。

    `defect_id` 本身是稳定的（`用例 + 维度`，见 `defects.Defect`），
    所以同一个 id 在不同轮次出现时，比较的是它的**证据有没有变**。

    `normalized` 里同时含证据与定位提示：定位提示从 `planner.py` 变成
    `chat.py` 说明缺陷指向换了地方，那是**真变化**，不是措辞差异。
    两者用 `@` 分隔，避免「证据尾部」与「提示头部」拼接后产生假相同。
    """

    defect_id: str
    kind: str
    dimension: str
    severity: str
    normalized: str
    exact_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {"defect_id": self.defect_id, "kind": self.kind,
                "dimension": self.dimension, "severity": self.severity,
                "normalized": self.normalized,
                "exact_digest": self.exact_digest}

    @classmethod
    def build(cls, *, defect_id: str, kind: str, dimension: str, severity: str,
              evidence: str, target_hint: str = "") -> "FingerprintEntry":
        return cls(defect_id=defect_id, kind=kind, dimension=dimension,
                   severity=severity,
                   normalized=(normalize_evidence(evidence) + " @ "
                               + normalize_evidence(target_hint)),
                   exact_digest=_digest(f"{evidence}\x00{target_hint}")[:16])

    @classmethod
    def of(cls, defect: Mapping[str, Any]) -> "FingerprintEntry":
        return cls.build(
            defect_id=str(defect.get("defect_id") or ""),
            kind=str(defect.get("kind") or ""),
            dimension=str(defect.get("dimension") or ""),
            severity=str(defect.get("severity") or ""),
            evidence=str(defect.get("evidence") or ""),
            target_hint=str(defect.get("target_hint") or ""))


@dataclass(frozen=True)
class FingerprintDiff:
    """两轮指纹的逐条比对。**不给一个布尔值**——那没法解释给谁听。"""

    resolved: tuple[str, ...] = ()          # 前有今无：修好了
    appeared: tuple[str, ...] = ()          # 前无今有：改出了新问题
    #: 同一缺陷 id、但证据变了：(id, 前归一化, 后归一化)
    mutated: tuple[tuple[str, str, str], ...] = ()
    carried: tuple[str, ...] = ()           # 一字未变：原地打转
    before_count: int = 0
    after_count: int = 0

    @property
    def moved(self) -> bool:
        """错误信息是否**发生了任何变化**。"""
        return bool(self.resolved or self.appeared or self.mutated)

    @property
    def identical(self) -> bool:
        """原地打转：前后都有缺陷，且一条都没变。

        前后都为空时不叫 identical（那是「全好了」，复评早已 accepted，
        不该走到这里）。所以这里显式要求两边都非空。
        """
        return (not self.moved and self.before_count > 0
                and self.after_count > 0)

    def render(self) -> str:
        """人读说明，直接进溯源账。"""
        if self.identical:
            return (f"错误信息一字未变（{self.after_count} 条缺陷全部原样保留）："
                    f"本轮改动没有推进任何一条，继续重试只会重复同一个动作")
        parts: list[str] = []
        if self.resolved:
            parts.append(f"已消失 {len(self.resolved)} 条（{', '.join(self.resolved[:4])}"
                         f"{'…' if len(self.resolved) > 4 else ''}）")
        if self.appeared:
            parts.append(f"新出现 {len(self.appeared)} 条（{', '.join(self.appeared[:4])}"
                         f"{'…' if len(self.appeared) > 4 else ''}）")
        if self.mutated:
            parts.append(f"证据变化 {len(self.mutated)} 条（{', '.join(m[0] for m in self.mutated[:4])}"
                         f"{'…' if len(self.mutated) > 4 else ''}）")
        if self.carried:
            parts.append(f"原样保留 {len(self.carried)} 条")
        return "；".join(parts) if parts else "无可比内容"

    def as_dict(self) -> dict[str, Any]:
        return {"resolved": list(self.resolved),
                "appeared": list(self.appeared),
                "mutated": [{"defect_id": m[0], "before": m[1], "after": m[2]}
                            for m in self.mutated],
                "carried": list(self.carried),
                "moved": self.moved, "identical": self.identical,
                "summary": self.render()}


# --------------------------------------------------------------------------- #
# 一批缺陷的指纹
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DefectFingerprint:
    """一轮缺陷的聚合指纹。`stable` 用于跨轮比对。"""

    entries: tuple[FingerprintEntry, ...] = ()
    #: 归一化后的集合摘要（**排序后**算，与缺陷产出顺序无关）
    stable: str = ""
    #: 原文摘要（追溯用，不参与「有没有变」的判定）
    exact: str = ""

    def __bool__(self) -> bool:
        return bool(self.entries)

    @property
    def count(self) -> int:
        return len(self.entries)

    def ids(self) -> tuple[str, ...]:
        return tuple(e.defect_id for e in self.entries)

    def diff_against(self, before: "DefectFingerprint") -> FingerprintDiff:
        """`self`（后）与 `before`（前）的逐条比对。

        方向写死为「后.diff_against(前)」，因为写反了不会报错，
        只会把「修好了」显示成「新出现」——一个永远看不出来的错。
        """
        now = {e.defect_id: e for e in self.entries}
        then = {e.defect_id: e for e in before.entries}
        resolved = tuple(sorted(set(then) - set(now)))
        appeared = tuple(sorted(set(now) - set(then)))
        mutated: list[tuple[str, str, str]] = []
        carried: list[str] = []
        for defect_id in sorted(set(now) & set(then)):
            old, new = then[defect_id], now[defect_id]
            if old.normalized == new.normalized and old.kind == new.kind:
                carried.append(defect_id)
            else:
                mutated.append((defect_id, old.normalized, new.normalized))
        return FingerprintDiff(
            resolved=resolved, appeared=appeared, mutated=tuple(mutated),
            carried=tuple(carried), before_count=len(then), after_count=len(now))

    def as_dict(self) -> dict[str, Any]:
        return {"stable": self.stable, "exact": self.exact, "count": self.count,
                "entries": [e.as_dict() for e in self.entries]}


def fingerprint_of(defects: Sequence[Mapping[str, Any] | Any]) -> DefectFingerprint:
    """从缺陷序列算指纹。接受 dict（State 形态）或 `Defect` 对象。

    两种形态都收的理由：这个判据要在「驱动拿 State 里的 dict」与
    「测试拿 `DefectBatch`」两处都用上。让调用方自己转一遍，
    迟早出现一处转错字段、另一处是对的。
    """
    entries: dict[str, FingerprintEntry] = {}
    for raw in defects:
        if isinstance(raw, Mapping):
            entry = FingerprintEntry.of(raw)
        else:
            entry = FingerprintEntry.build(
                defect_id=str(getattr(raw, "defect_id", "") or ""),
                kind=str(getattr(getattr(raw, "kind", None), "value", "") or ""),
                dimension=str(getattr(raw, "dimension", "") or ""),
                severity=str(getattr(getattr(raw, "severity", None), "value", "") or ""),
                evidence=str(getattr(raw, "evidence", "") or ""),
                target_hint=str(getattr(raw, "target_hint", "") or ""))
        if not entry.defect_id:
            continue
        entries[entry.defect_id] = entry

    ordered = tuple(entries[k] for k in sorted(entries))
    stable = _digest("\n".join(f"{e.defect_id}|{e.kind}|{e.normalized}"
                               for e in ordered))[:32]
    exact = _digest("\n".join(f"{e.defect_id}|{e.exact_digest}" for e in ordered))[:32]
    return DefectFingerprint(entries=ordered, stable=stable, exact=exact)
