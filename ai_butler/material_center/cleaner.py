"""P2 文档清洗 · 去重 · 分类 · 时效打标。

计划书依据
----------
- 三、P2「实现文档清洗、去重、分类、时效打标，**转换为 Wiki Source 源页面**」
- 三、P2「原始素材自动生成 Agent-Wiki source.* 源页面，保存来源元数据」
- 三、P2「强制所有素材绑定溯源来源，对应 Wiki Evidence 证据条目」
- 三、P2「开发过期素材识别、新旧版本增量融合；过期来源标记为 status:archived 归档」

分类与时效口径
-------------
计划书点名的公共域内容为「景点、天气、公开资讯」；旅游模块另点名
「景区开放状态、天气、交通、门票、排队情况」。据此确定分类集合；
各分类的时效天数计划书未给 → 属**实现参数**，集中登记在 CATEGORY_TTL_DAYS。

**例外（U122，用户裁定）**：带正式文号的**规范性文件**不设到期日。
TTL 是为易变的运行时事实（天气/票价/公告）设的；制度文件的更新靠新版本
替代（supersede），靠时间到期会把 2013 年的办法算成 2014-01-30 过期。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from ai_butler.agent_wiki.ontology import SOURCE, SOURCE_ACTIVE, WikiPage, new_page

#: 计划书公共域与旅游模块点名过的素材类别
CATEGORY_SCENIC = "景点"
CATEGORY_WEATHER = "天气"
CATEGORY_TRAFFIC = "交通"
CATEGORY_TICKET = "门票"
CATEGORY_NEWS = "公开资讯"
CATEGORY_OTHER = "其他"
CATEGORIES: tuple[str, ...] = (CATEGORY_SCENIC, CATEGORY_WEATHER, CATEGORY_TRAFFIC,
                               CATEGORY_TICKET, CATEGORY_NEWS, CATEGORY_OTHER)

#: 分类关键词（实现参数）
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    CATEGORY_WEATHER: ("天气", "气温", "降雨", "降水", "台风", "空气质量", "weather"),
    CATEGORY_TRAFFIC: ("交通", "地铁", "公交", "航班", "高铁", "路况", "拥堵", "导航"),
    CATEGORY_TICKET: ("门票", "票价", "预约", "售票", "免票", "优惠券"),
    CATEGORY_SCENIC: ("景区", "景点", "开放时间", "排队", "游玩", "游览", "公园", "博物馆"),
    CATEGORY_NEWS: ("公告", "资讯", "新闻", "通知", "发布"),
}

#: 各类素材的时效天数（实现参数）
CATEGORY_TTL_DAYS: dict[str, int] = {
    CATEGORY_WEATHER: 1,
    CATEGORY_TRAFFIC: 3,
    CATEGORY_TICKET: 30,
    CATEGORY_NEWS: 30,
    CATEGORY_SCENIC: 365,
    CATEGORY_OTHER: 30,
}

#: 清洗时要剔除的样板行（实现参数）
BOILERPLATE_PATTERNS: tuple[str, ...] = (
    r"^\s*版权(所有|归).*$", r"^\s*转载请注明.*$", r"^\s*免责声明.*$",
    r"^\s*(点击|长按).*(关注|查看).*$", r"^\s*来源[:：].*$",
    r"^\s*编辑[:：].*$", r"^\s*责任编辑[:：].*$", r"^\s*扫描二维码.*$",
    r"^\s*(分享|收藏|点赞|在看)\s*$",
)
_BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS))
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_MULTI_PUNCT_RE = re.compile(r"([。！？；，,.!?;])\1{1,}")
#: 规范性文件的文号（如 `财行〔2013〕531号`、`国办发[2019]12号`）。
#: 用它判「这是不是一份**会长期有效**的文件」，而不是一条会过期的资讯（U122）。
_NORMATIVE_DOC_NO = re.compile(
    r"[\u4e00-\u9fa5]{1,8}\s*[〔\[（(]\s*(?:19|20)\d{2}\s*[〕\]）)]\s*\d{1,4}\s*号")
#: 块级标签必须先变成换行再剥离。
#: 若一律替换成空格，整页会被压成一行，随后**基于行首的样板行剔除全部失效**
#: （「版权所有…」不在行首就删不掉），切片也会丢掉段落边界。
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|h[1-6]|section|article|header|footer|"
    r"blockquote|pre|form|nav|aside)\b[^>]*>", re.I)


class CleanError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RawDocument:
    doc_id: str
    url: str
    title: str
    text: str
    publisher: str = ""
    published_at: str | None = None      # ISO8601
    fetched_at: str = ""


@dataclass(frozen=True)
class CleanedDocument:
    doc_id: str
    url: str
    title: str
    text: str
    publisher: str
    category: str
    content_hash: str
    published_at: str | None
    stale_after: str
    fetched_at: str
    duplicate_of: str | None = None
    removed_lines: int = 0

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None

    def is_stale(self, *, now: datetime | None = None) -> bool:
        if not self.stale_after:
            # 空串 = **没有到期日**（规范性文件，U122）。不是「忘了打标」：
            # 忘打标会表现为「永远不过期」，所以这里只能靠 clean_document 显式置空。
            return False
        moment = now or utcnow()
        deadline = datetime.fromisoformat(self.stale_after)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return moment >= deadline


def clean_text(text: str) -> tuple[str, int]:
    """返回（清洗后文本, 被剔除的样板行数）。"""
    text = _SCRIPT_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)      # 保住块级边界
    text = _TAG_RE.sub(" ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    kept: list[str] = []
    removed = 0
    for line in text.split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if not line:
            kept.append("")
            continue
        if _BOILERPLATE_RE.match(line):
            removed += 1
            continue
        kept.append(line)

    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    out = _MULTI_PUNCT_RE.sub(r"\1", out)
    return out, removed


def classify(text: str, *, title: str = "") -> str:
    """按计划书点名过的类别做关键词分类。命中多个时取命中次数最多者。"""
    blob = f"{title} {text}"
    best, best_hits = CATEGORY_OTHER, 0
    for category, words in CATEGORY_KEYWORDS.items():
        hits = sum(blob.count(w) for w in words)
        if hits > best_hits:
            best, best_hits = category, hits
    return best


def content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", "", text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def ttl_days(category: str) -> int:
    return CATEGORY_TTL_DAYS.get(category, CATEGORY_TTL_DAYS[CATEGORY_OTHER])


def is_normative_document(text: str) -> bool:
    """是不是一份**规范性文件**（带正式文号），而不是一条会过期的资讯。

    判据只认文号，不认「办法/通知/规定」这类字眼：字眼在新闻标题里也常见，
    而文号是发文机关给的正式标识，可核验。计划书 P2 的 TTL 是为「易变的运行时
    事实」（天气、票价、公告）设的；制度文件的更新靠**新版本替换**（supersede），
    不靠时间到期，所以对它不设到期日（U122）。
    """
    return bool(_NORMATIVE_DOC_NO.search(text or ""))


def clean_document(raw: RawDocument, *, now: datetime | None = None) -> CleanedDocument:
    text, removed = clean_text(raw.text)
    if not text:
        raise CleanError(f"素材 {raw.doc_id!r} 清洗后为空，拒绝入库")
    moment = now or utcnow()
    category = classify(text, title=raw.title)
    published = raw.published_at
    base = datetime.fromisoformat(published) if published else moment
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    # 规范性文件不设到期日（U122）：按「发布日 + 资讯 TTL」会给 2013 年的办法
    # 算出 2014-01-30 的到期日，入库即过期十年；一旦 archive_stale 被装配，
    # 这批制度素材会被静默清掉，表现为「明明入过库却答不出来」。
    stale_after = "" if is_normative_document(text) else (
        base + timedelta(days=ttl_days(category))).isoformat()
    return CleanedDocument(
        doc_id=raw.doc_id, url=raw.url, title=raw.title or raw.doc_id, text=text,
        publisher=raw.publisher, category=category, content_hash=content_hash(text),
        published_at=published, stale_after=stale_after,
        fetched_at=raw.fetched_at or moment.isoformat(), removed_lines=removed)


def deduplicate(docs: Sequence[CleanedDocument]) -> list[CleanedDocument]:
    """按内容哈希去重：先出现者保留，后者标记 duplicate_of（**不静默丢弃**）。"""
    seen: dict[str, str] = {}
    out: list[CleanedDocument] = []
    for doc in docs:
        first = seen.get(doc.content_hash)
        if first is None:
            seen[doc.content_hash] = doc.doc_id
            out.append(doc)
        else:
            from dataclasses import replace
            out.append(replace(doc, duplicate_of=first))
    return out


def clean_batch(raw_docs: Iterable[RawDocument], *, now: datetime | None = None
                ) -> list[CleanedDocument]:
    return deduplicate([clean_document(d, now=now) for d in raw_docs])


# --------------------------------------------------------------------------- #
# 转换为 Agent-Wiki Source 源页面
# --------------------------------------------------------------------------- #
def to_wiki_source_page(cleaned: CleanedDocument, *, user_id: str | None = None,
                        now: str = "") -> WikiPage:
    """计划书三、P2「转换为 Wiki Source 源页面，保存来源元数据」。

    素材溯源库属于公共 Workspace ⇒ 默认**不带 user_id**；
    私有 Vault 素材（如用户上传）可显式传入 user_id。
    """
    if cleaned.is_duplicate:
        raise CleanError(
            f"素材 {cleaned.doc_id!r} 是 {cleaned.duplicate_of!r} 的重复内容，不应再生成源页面"
        )
    return new_page(
        SOURCE, cleaned.doc_id, title=cleaned.title, user_id=user_id, now=now,
        status=SOURCE_ACTIVE, url=cleaned.url, publisher=cleaned.publisher,
        category=cleaned.category, content_hash=cleaned.content_hash,
        published_at=cleaned.published_at or "", stale_after=cleaned.stale_after,
        fetched_at=cleaned.fetched_at,
    )
