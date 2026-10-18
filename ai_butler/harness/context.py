"""P10 上下文压缩（裁定 U36）。

计划书依据
----------
- 三、P10 步骤7「开发上下文压缩机制，对接数据中台记忆摘要；
  **摘要脱敏，不带原始用户隐私**」
- 三、P10 触发时机「离线链路**绝对不能访问原始未脱敏用户私有数据**」

裁定 U36
--------
压缩分两级：结构化裁剪（默认生效）+ 模型摘要（端口注入）。
**压缩不替代脱敏**：顺序固定为「先脱敏，再裁剪，再摘要，最后再验一次」——
压缩会把分散的片段聚到一起，原本单看不敏感的字段拼起来可能就构成身份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ai_butler.evaluation.redaction import Redactor


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    text: str
    source: str = ""
    #: 相关性 0~1，用于裁剪排序
    relevance: float = 0.0
    occurred_at: str = ""
    scope: str = "public"


@dataclass(frozen=True)
class CompressedContext:
    items: tuple[ContextItem, ...]
    dropped: int
    summary: str
    model_layer_active: bool

    @property
    def text(self) -> str:
        parts = [i.text for i in self.items]
        if self.summary:
            parts.append(self.summary)
        return "\n".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {"item_count": len(self.items), "dropped": self.dropped,
                "summary": self.summary, "model_layer_active": self.model_layer_active,
                "items": [{"item_id": i.item_id, "source": i.source,
                           "relevance": i.relevance, "scope": i.scope}
                          for i in self.items]}


#: 模型摘要器：接收已脱敏文本，返回摘要
Summarizer = Callable[[str], str]


def compress(items: Sequence[ContextItem], *, redactor: Redactor,
             max_items: int = 20, summarizer: Summarizer | None = None,
             where: str = "Harness 上下文") -> CompressedContext:
    """先脱敏 → 再裁剪 → 再摘要 → **最后再验一次**。"""
    if max_items <= 0:
        raise ContextError("max_items 必须为正")

    # ---- 1) 先脱敏：任何原文都不许越过这一步 ----
    safe_items: list[ContextItem] = []
    for item in items:
        safe_items.append(ContextItem(
            item_id=item.item_id, text=redactor.redact(item.text),
            source=item.source, relevance=item.relevance,
            occurred_at=item.occurred_at, scope=item.scope))

    # ---- 2) 结构化裁剪：相关度优先，其次新近 ----
    ordered = sorted(safe_items, key=lambda i: (i.relevance, i.occurred_at),
                     reverse=True)
    kept = ordered[:max_items]
    dropped = len(ordered) - len(kept)

    # ---- 3) 模型摘要（可选）----
    summary = ""
    if summarizer is not None and kept:
        summary = redactor.redact(summarizer("\n".join(i.text for i in kept)))

    # ---- 4) 压缩完再验一次：合并后可能拼出身份 ----
    redactor.assert_clean("\n".join(i.text for i in kept), where=where)
    if summary:
        redactor.assert_clean(summary, where=where + "（摘要）")

    return CompressedContext(items=tuple(kept), dropped=dropped, summary=summary,
                             model_layer_active=summarizer is not None)
