"""P2 统一切片规则。

计划书依据
----------
- 三、P2「实现文档清洗、去重、分类、时效打标，转换为 Wiki Source 源页面」
- 三、P2「**统一切片规则、Embedding 入库规则**；Wiki 编译产出实体、声明同步写入向量库」

口径
----
计划书未给切片参数，故均为**实现参数**，集中登记在 ChunkingRule：
优先在自然边界（段落 / 句末标点）切开，超长句硬切，切块之间保留重叠以免语义断裂。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

#: 优先切片边界，按「越靠前越优先」排列
DEFAULT_BOUNDARIES: tuple[str, ...] = ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")

_WS = re.compile(r"[ \t\u3000]+")


class ChunkingError(ValueError):
    pass


@dataclass(frozen=True)
class ChunkingRule:
    max_chars: int = 480        # 单块上限（实现参数）
    overlap_chars: int = 80     # 相邻块重叠（实现参数）
    min_chars: int = 40         # 低于此长度的尾块并入前块（实现参数）
    boundaries: tuple[str, ...] = DEFAULT_BOUNDARIES

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ChunkingError("max_chars 必须为正数")
        if not 0 <= self.overlap_chars < self.max_chars:
            raise ChunkingError("overlap_chars 必须落在 [0, max_chars)")
        if self.min_chars < 0:
            raise ChunkingError("min_chars 不得为负")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    index: int
    text: str
    char_start: int
    char_end: int

    @property
    def length(self) -> int:
        return len(self.text)


def normalize_text(text: str) -> str:
    """统一空白，避免同一素材因排版差异被切成不同块。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_sentences(text: str, boundaries: Sequence[str]) -> list[str]:
    """按边界切句，保留分隔符本身。"""
    pieces: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        buf += text[i]
        for boundary in boundaries:
            if text.startswith(boundary, i):
                buf += boundary[1:]
                i += len(boundary)
                pieces.append(buf)
                buf = ""
                break
        else:
            i += 1
    if buf.strip():
        pieces.append(buf)
    return [p for p in (s.strip() for s in pieces) if p]


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def chunk_document(doc_id: str, text: str, *, rule: ChunkingRule | None = None) -> list[Chunk]:
    """把一个文档切成带重叠的块。doc_id 参与 chunk_id 生成，保证幂等。"""
    if not doc_id or not str(doc_id).strip():
        raise ChunkingError("doc_id 不得为空")
    rule = rule or ChunkingRule()
    text = normalize_text(text)
    if not text:
        return []

    sentences: list[str] = []
    for piece in _split_sentences(text, rule.boundaries):
        sentences.extend(_hard_split(piece, rule.max_chars) if len(piece) > rule.max_chars
                         else [piece])

    chunks: list[Chunk] = []
    buf = ""
    buf_start = 0
    cursor = 0
    for sentence in sentences:
        start = text.find(sentence, cursor)
        start = cursor if start < 0 else start
        cursor = start + len(sentence)

        if buf and len(buf) + 1 + len(sentence) > rule.max_chars:
            chunks.append(_make(doc_id, len(chunks), buf, buf_start))
            tail = buf[-rule.overlap_chars:] if rule.overlap_chars else ""
            buf = f"{tail} {sentence}".strip()
            buf_start = max(0, cursor - len(sentence) - len(tail))
        else:
            if not buf:
                buf_start = start
            buf = f"{buf} {sentence}".strip() if buf else sentence

    if buf:
        if chunks and len(buf) < rule.min_chars:
            chunks[-1] = _make(doc_id, len(chunks) - 1,
                               f"{chunks[-1].text} {buf}".strip(), chunks[-1].char_start)
        else:
            chunks.append(_make(doc_id, len(chunks), buf, buf_start))
    return chunks


def _make(doc_id: str, index: int, text: str, start: int) -> Chunk:
    return Chunk(chunk_id=f"{doc_id}#{index:04d}", doc_id=doc_id, index=index, text=text,
                 char_start=max(0, start), char_end=max(0, start) + len(text))


def chunk_digest(chunks: Sequence[Chunk]) -> str:
    """切片结果的稳定摘要，用于「同一素材是否已入库」的判断。"""
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.text.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
