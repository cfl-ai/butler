"""P3 会话上下文管理：多轮状态保持 · 窗口控制 · 摘要压缩。

计划书依据
----------
- 三、P3「会话上下文管理：**多轮对话状态保持、上下文窗口控制、摘要压缩**」
- 三、P4「禁止读取其他 user_id 数据」
- 三、P0-1「会话有效期 / 空闲超时 / 续期提前量」（会话失效即不再持有上下文）

隔离
----
上下文按 user_id 分片落盘，加载时必须同时给出 user_id 与 session_id；
**不存在**只按 session_id 读取的入口，避免拿到他人会话。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from ai_butler.core.storage import SCOPE_CONVERSATIONS, FileStore

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
ROLES: tuple[str, ...] = (ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM)


class ContextError(ValueError):
    pass


@dataclass(frozen=True)
class ContextPolicy:
    """窗口与压缩参数（计划书未给数值 → 实现参数）。"""

    max_turns: int = 20             # 窗口内最多保留的对话轮数
    max_chars: int = 4000           # 窗口总字符上限
    keep_recent_turns: int = 6      # 压缩时无条件保留的最近轮数
    summary_max_chars: int = 600    # 摘要本身的上限

    def __post_init__(self) -> None:
        if self.max_turns <= 0 or self.max_chars <= 0:
            raise ContextError("max_turns / max_chars 必须为正数")
        if not 0 <= self.keep_recent_turns <= self.max_turns:
            raise ContextError("keep_recent_turns 必须落在 [0, max_turns]")


@dataclass
class Turn:
    role: str
    content: str
    ts: str = ""
    intent: str | None = None
    trace_id: str = ""
    compacted: bool = False

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ContextError(f"未知角色 {self.role!r}，可选 {list(ROLES)}")
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()

    @property
    def length(self) -> int:
        return len(self.content)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Summarizer(Protocol):
    """摘要器。默认实现是抽取式的，不依赖模型；可替换为大模型摘要。"""

    def summarize(self, turns: Sequence[Turn], *, max_chars: int) -> str:
        ...


class ExtractiveSummarizer:
    """抽取式摘要：保留每轮开头，绝不编造内容。"""

    name = "extractive"

    def summarize(self, turns: Sequence[Turn], *, max_chars: int) -> str:
        if not turns:
            return ""
        budget = max(60, max_chars // max(1, len(turns)))
        parts = []
        for turn in turns:
            snippet = turn.content.strip().replace("\n", " ")
            if len(snippet) > budget:
                snippet = snippet[:budget].rstrip() + "…"
            parts.append(f"{turn.role}: {snippet}")
        text = "；".join(parts)
        return text if len(text) <= max_chars else text[:max_chars].rstrip() + "…"


@dataclass
class ConversationContext:
    user_id: str
    session_id: str
    summary: str = ""
    turns: list[Turn] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.user_id or not str(self.user_id).strip():
            raise ContextError("会话上下文必须绑定 user_id")
        if not self.session_id or not str(self.session_id).strip():
            raise ContextError("会话上下文必须绑定 session_id")

    @property
    def total_chars(self) -> int:
        return sum(t.length for t in self.turns) + len(self.summary)

    def window(self, policy: ContextPolicy) -> list[Turn]:
        """上下文窗口控制：先按轮数截断，再按字符上限从旧到新丢弃。

        被丢弃的轮次会先进入摘要（见 ContextManager.compact），不会凭空消失。
        """
        kept = [t for t in self.turns if not t.compacted]
        if len(kept) > policy.max_turns:
            kept = kept[-policy.max_turns:]
        while kept and sum(t.length for t in kept) + len(self.summary) > policy.max_chars:
            kept.pop(0)
        return kept

    def render(self, policy: ContextPolicy) -> str:
        """把窗口渲染成给下游 Agent / 大模型看的上下文文本。"""
        lines = []
        if self.summary:
            lines.append(f"[历史摘要] {self.summary}")
        lines.extend(f"{t.role}: {t.content}" for t in self.window(policy))
        return "\n".join(lines)


class ContextManager:
    def __init__(self, store: FileStore | None = None, *,
                 policy: ContextPolicy | None = None,
                 summarizer: Summarizer | None = None) -> None:
        self.store = store
        self.policy = policy or ContextPolicy()
        self.summarizer = summarizer or ExtractiveSummarizer()
        self._memory: dict[tuple[str, str], ConversationContext] = {}

    # ---------------- 存取（按 user_id 分片） ---------------- #
    def _key(self, user_id: str, session_id: str) -> tuple[str, str]:
        if not user_id or not str(user_id).strip():
            raise ContextError("读取会话上下文必须携带 user_id（计划书三、P4）")
        if not session_id or not str(session_id).strip():
            raise ContextError("读取会话上下文必须携带 session_id")
        return str(user_id), str(session_id)

    def _path(self, user_id: str, session_id: str) -> Path | None:
        if self.store is None:
            return None
        return self.store.path(SCOPE_CONVERSATIONS, user_id, f"{session_id}.json")

    def load(self, user_id: str, session_id: str) -> ConversationContext:
        key = self._key(user_id, session_id)
        path = self._path(*key)
        if path is not None and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return ConversationContext(
                user_id=data["user_id"], session_id=data["session_id"],
                summary=data.get("summary", ""),
                turns=[Turn(**t) for t in data.get("turns", [])])
        if key in self._memory:
            return self._memory[key]
        return ConversationContext(user_id=key[0], session_id=key[1])

    def save(self, ctx: ConversationContext) -> ConversationContext:
        path = self._path(ctx.user_id, ctx.session_id)
        if path is None:
            self._memory[(ctx.user_id, ctx.session_id)] = ctx
            return ctx
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"user_id": ctx.user_id, "session_id": ctx.session_id, "summary": ctx.summary,
             "turns": [t.as_dict() for t in ctx.turns]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return ctx

    # ---------------- 多轮状态保持 ---------------- #
    def append(self, user_id: str, session_id: str, *, role: str, content: str,
               intent: str | None = None, trace_id: str = "") -> ConversationContext:
        ctx = self.load(user_id, session_id)
        ctx.turns.append(Turn(role=role, content=content, intent=intent, trace_id=trace_id))
        return self.save(self.compact_if_needed(ctx))

    # ---------------- 摘要压缩 ---------------- #
    def needs_compaction(self, ctx: ConversationContext) -> bool:
        active = [t for t in ctx.turns if not t.compacted]
        if len(active) > self.policy.max_turns:
            return True
        return ctx.total_chars > self.policy.max_chars

    def compact_if_needed(self, ctx: ConversationContext) -> ConversationContext:
        if not self.needs_compaction(ctx):
            return ctx
        return self.compact(ctx)

    def compact(self, ctx: ConversationContext) -> ConversationContext:
        """把窗口外的旧轮次压进摘要，最近若干轮原样保留。

        被压缩的轮次**不再留在 turns 里**——它们的内容已由摘要代表，
        否则上下文文件会随会话无限增长。因此 mark_compacted 只是审计痕迹。
        """
        active = [t for t in ctx.turns if not t.compacted]
        if not active:
            return ctx

        keep = min(self.policy.keep_recent_turns, len(active))
        head = active[:len(active) - keep] if keep else active
        tail = active[len(active) - keep:] if keep else []

        if head:
            self._merge_summary(ctx, self.summarizer.summarize(
                head, max_chars=self.policy.summary_max_chars))
            for turn in head:
                turn.compacted = True

        ctx.turns = list(tail)
        return self._trim_by_chars(ctx)

    def _trim_by_chars(self, ctx: ConversationContext) -> ConversationContext:
        """按字符预算从最旧一轮开始丢弃，丢弃前先并入摘要。"""
        budget = self.policy.max_chars - len(ctx.summary)
        while ctx.turns and sum(t.length for t in ctx.turns) > budget:
            dropped = ctx.turns.pop(0)
            dropped.compacted = True
            self._merge_summary(ctx, self.summarizer.summarize(
                [dropped], max_chars=self.policy.summary_max_chars))
            budget = self.policy.max_chars - len(ctx.summary)
        return ctx

    def _merge_summary(self, ctx: ConversationContext, text: str) -> None:
        if not text:
            return
        ctx.summary = f"{ctx.summary}；{text}".strip("；") if ctx.summary else text
        if len(ctx.summary) > self.policy.summary_max_chars:
            # 保留最近的摘要内容：旧信息被更新的对话挤掉
            ctx.summary = ctx.summary[-self.policy.summary_max_chars:]
