"""联网素材的**入库准入核验** —— 三轮筛真实性，假的不进索引。

与 `material_center/verification.py` 的区别（两个层次，别混）
-----------------------------------------------------------
| 模块 | 时机 | 判什么 | 判错的代价 |
|---|---|---|---|
| `verification.py` | 回答**发出之前** | 这段话有没有带证据 ID 的支撑 | 一次回答站不住 |
| `admission.py`（本模块） | 素材**入库之前** | 这份材料本身是不是真的 | 污染整个索引、之后每次检索都被骗 |

因此两套阈值与判据各自独立。把入库准入塞进 `verification.py` 会让
「回答复检」与「素材准入」共享一套常量 —— 日后调其中一个必然误伤另一个（E-44 老路）。

三轮的口径（用户要求：每轮把真实的收进来、把虚假的剔掉）
--------------------------------------------------------
| 轮次 | 判据 | 结果 |
|---|---|---|
| 第 1 轮 | 来源属权威域（部委 / 政府网 / 中央媒体）**且**正文完整 | 收 |
| 第 2 轮 | 与已收素材的字符 3-gram 重合度 ≥ `CROSS_ACCEPT` | 独立来源互相印证 → 收 |
| 第 3 轮 | 仍未定的：正文完整且来源为政府机构 → 收；其余剔除 | 收 / 剔除 |

三类剔除必须分开统计（E-92：**判不了 ≠ 判不过**）
------------------------------------------------
- `contradicted`：自称同一文号，正文却与权威版本几乎不重合 → **判定为虚假/篡改**
- `duplicate_repost`：与已收内容近乎逐字相同且来源非权威 → 重复转载，不入库（避免索引冗余）
- `unverifiable_*`：取不到正文 / 与任何已收内容都无重合且无权威背书 → **无法核实**

「无法核实」永不进索引，但**永不写成「虚假」**。报告里两类分别计数，
否则「筛掉 5 条」这种话会被读成「抓到 5 条假消息」。

已知边界（如实写在这里，不留给读者猜）
------------------------------------
- 只做**互证**，不做**外部事实核查**：三轮都依赖「有没有第二个来源说同样的话」。
  全网只有一家独有内容的，无论真假都会被判为「无法核实」而拒收。
- 矛盾检测只覆盖「同文号却内容不符」这一种形态。金额、日期层面的细小篡改
  检测不出来 —— 那种需要按条款做结构化比对，本模块不做。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# ---------------------------------------------------------------- 阈值（实现参数）
#: 正文少于此值视为「没抓到正文」，不足以核实
MIN_BODY_CHARS = 120
#: 正文完整度门槛：低于此值的多是节选/摘要，**不能独立成为一条证据**
MIN_COMPLETE_CHARS = 600
#: 与已收内容的重合度达到此值：视为同一事实获得了独立印证
CROSS_ACCEPT = 0.45
#: 近乎逐字相同：判定为重复转载
DUPLICATE_RATIO = 0.85
#: 完全无重合：没有任何独立依据
NO_OVERLAP_RATIO = 0.15
#: 判定「同文号却内容不符」所需的最短正文（短报道是节选，不能当矛盾）
CONTRADICTION_MIN_CHARS = 800
#: 判定矛盾的相似度上限
CONTRADICTION_RATIO = 0.15

#: 权威域：部委 / 中央媒体一手发布
AUTHORITATIVE_DOMAINS: tuple[str, ...] = (
    "mof.gov.cn", "people.com.cn", "cnr.cn",
    "xinhuanet.com", "news.cn", "cctv.com",
)
#: 中央主站：**只有 gov.cn 本身**算权威。
#: 用 `endswith(".gov.cn")` 一刀切会把山东省审计厅、县纪委网站全升成 A 级 ——
#: 地方转载站的内容并非一手发布，必须走互证。
CENTRAL_GOV_HOSTS: tuple[str, ...] = ("gov.cn", "www.gov.cn")
#: 政府机构域（地方厅局等）：可信但需互证
INSTITUTIONAL_SUFFIXES: tuple[str, ...] = (".gov.cn",)

TIER_AUTHORITATIVE = "authoritative"
TIER_INSTITUTIONAL = "institutional"
TIER_UNKNOWN = "unknown"
TIERS: tuple[str, ...] = (TIER_AUTHORITATIVE, TIER_INSTITUTIONAL, TIER_UNKNOWN)

# ---------------------------------------------------------------- 判定结果代码
ACCEPT_AUTHORITATIVE = "authoritative_source"
ACCEPT_CORROBORATED = "corroborated_by_peer"
ACCEPT_THIRD_ROUND = "institutional_after_three_rounds"
REJECT_CONTRADICTED = "contradicted"
REJECT_DUPLICATE = "duplicate_repost"
REJECT_NO_BODY = "unverifiable_no_body"
REJECT_NO_OVERLAP = "unverifiable_no_overlap"
REJECT_PARTIAL = "unverifiable_partial"
REJECT_UNRESOLVED = "unverifiable_after_three_rounds"

#: 哪些代码算「判定为虚假」—— 与「无法核实」必须分开统计
FALSE_CODES: frozenset[str] = frozenset({REJECT_CONTRADICTED})

_DOC_NO_RE = re.compile(r"财[行办]*\s*[〔\[（(]\s*(\d{4})\s*[〕\]）)]\s*(\d{1,4})\s*号")
_WS_RE = re.compile(r"[\s\u3000]+")
_PUNCT = "，。、；：！？,.;:!?（）()《》〈〉“”\"'‘’—－-…·　 "


@dataclass(frozen=True)
class Candidate:
    """一条待核实素材。`text` 必须是**抓到的正文**，不接受模型转述。

    用模型复述内容再入库，等于把「模型可能编错」的东西当成事实源 ——
    真实链路里永远存原始抓取文本。
    """

    doc_id: str
    url: str
    title: str
    text: str
    publisher: str = ""
    published_at: str | None = None

    @property
    def body_chars(self) -> int:
        return len(_WS_RE.sub("", self.text or ""))


@dataclass(frozen=True)
class Verdict:
    doc_id: str
    round_no: int
    accepted: bool
    code: str
    reason: str
    similarity: float | None = None
    peer: str | None = None

    @property
    def is_false(self) -> bool:
        return self.code in FALSE_CODES

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id, "round": self.round_no, "accepted": self.accepted,
            "code": self.code, "reason": self.reason,
            "similarity": None if self.similarity is None else round(self.similarity, 3),
            "peer": self.peer, "is_false": self.is_false,
        }


@dataclass
class AdmissionReport:
    rounds: list[dict] = field(default_factory=list)
    accepted: list[Candidate] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def rejected(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.accepted]

    def by_code(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.rejected:
            out[v.code] = out.get(v.code, 0) + 1
        return out

    def false_count(self) -> int:
        return sum(1 for v in self.verdicts if v.is_false)

    def unverifiable_count(self) -> int:
        return sum(1 for v in self.rejected if v.code.startswith("unverifiable"))

    def as_dict(self) -> dict:
        return {
            "candidates": len(self.verdicts),
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "false_rejected": self.false_count(),
            "unverifiable_rejected": self.unverifiable_count(),
            "by_code": self.by_code(),
            "rounds": self.rounds,
            "verdicts": [v.as_dict() for v in self.verdicts],
        }


# ---------------------------------------------------------------- 基础度量
def _host_of(url: str) -> str:
    m = re.match(r"^[a-zA-Z][\w+.-]*://([^/?#]+)", url or "")
    host = (m.group(1) if m else "").lower()
    return host.split("@")[-1].split(":")[0]


def source_tier(url: str) -> str:
    """按域名判来源等级。**只看域名，不看内容** —— 内容真假由后面的轮次判。"""
    host = _host_of(url)
    if not host:
        return TIER_UNKNOWN
    if host in CENTRAL_GOV_HOSTS:
        return TIER_AUTHORITATIVE
    if any(host == d or host.endswith("." + d) for d in AUTHORITATIVE_DOMAINS):
        return TIER_AUTHORITATIVE
    if any(host.endswith(s) for s in INSTITUTIONAL_SUFFIXES):
        return TIER_INSTITUTIONAL
    return TIER_UNKNOWN


def shingles(text: str, n: int = 3) -> set[str]:
    """字符 n-gram 集合。中文不用分词：分词器差异会让「同一段话」看起来不同。"""
    clean = "".join(ch for ch in (text or "") if ch not in _PUNCT and ch not in "\r\n\t")
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def similarity(a: str, b: str, *, n: int = 3) -> float:
    """Jaccard 重合度。任一为空返回 0（不把「都没内容」当吻合）。"""
    sa, sb = shingles(a, n), shingles(b, n)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


def document_numbers(text: str) -> set[str]:
    """抽出文号（财行〔2013〕531号 → 2013-531）。用于识别「同文号不同内容」。"""
    return {f"{m.group(1)}-{m.group(2)}" for m in _DOC_NO_RE.finditer(text or "")}


def _best_peer(doc: Candidate, accepted: Sequence[Candidate]) -> tuple[float, str | None]:
    best, peer = 0.0, None
    for other in accepted:
        score = similarity(doc.text, other.text)
        if score > best:
            best, peer = score, other.doc_id
    return best, peer


def _contradiction(doc: Candidate, accepted: Sequence[Candidate]) -> tuple[bool, str | None]:
    """自称同一文号、正文却几乎不重合 → 篡改/编造。

    只对**正文足够长**的候选启用：短报道是节选，与全文重合度天然低，
    判成「矛盾」会误杀真实来源 —— 宁可漏判，不可错杀。
    """
    if doc.body_chars < CONTRADICTION_MIN_CHARS:
        return False, None
    mine = document_numbers(doc.text)
    if not mine:
        return False, None
    for other in accepted:
        if other.body_chars < CONTRADICTION_MIN_CHARS:
            continue
        if not (mine & document_numbers(other.text)):
            continue
        if similarity(doc.text, other.text) < CONTRADICTION_RATIO:
            return True, other.doc_id
    return False, None


# ---------------------------------------------------------------- 三轮核验
def review(candidates: Iterable[Candidate]) -> AdmissionReport:
    """三轮核验。每轮**只处理上一轮没定下来的**：通过即收，判假即剔。"""
    pending = list(candidates)
    report = AdmissionReport()

    # ---- 第 1 轮：权威来源 + 正文完整 ----
    still: list[Candidate] = []
    detail1: list[dict] = []
    for doc in pending:
        if doc.body_chars < MIN_BODY_CHARS:
            still.append(doc)        # 没抓到正文 ≠ 判假，留到下一轮
            continue
        # 重复判定必须**每一轮都先做**：同一份文件的多个发布渠道（部委站、
        # 国务院公报、地方厅局转载）内容近乎逐字相同，全收会让检索被同一段话霸屏。
        score, peer = _best_peer(doc, report.accepted)
        if report.accepted and score >= DUPLICATE_RATIO:
            v = Verdict(doc.doc_id, 1, False, REJECT_DUPLICATE,
                        f"与已收素材 {peer} 近乎逐字相同（{score:.0%}），"
                        "同一文件的另一发布渠道 → 不重复入库",
                        similarity=score, peer=peer)
            report.verdicts.append(v)
            detail1.append(v.as_dict())
            continue
        if source_tier(doc.url) == TIER_AUTHORITATIVE and doc.body_chars >= MIN_COMPLETE_CHARS:
            v = Verdict(doc.doc_id, 1, True, ACCEPT_AUTHORITATIVE,
                        f"权威域一手来源（{_host_of(doc.url)}），正文 {doc.body_chars} 字")
            report.verdicts.append(v)
            report.accepted.append(doc)
            detail1.append(v.as_dict())
        else:
            # 权威域但正文只有摘要/节选：**不能只凭域名就收**，
            # 交给第 2 轮看它是否与已收素材互证。
            still.append(doc)
    report.rounds.append({"round": 1, "checked": len(pending),
                          "accepted": len(detail1), "pending": len(still),
                          "detail": detail1})

    # ---- 第 2 轮：独立来源交叉印证 ----
    detail2: list[dict] = []
    next_round: list[Candidate] = []
    for doc in still:
        if doc.body_chars < MIN_BODY_CHARS:
            if not report.accepted:
                # 连一份可信内容都没有时无法互证 → 直接记「无法核实」。
                # 否则会空转出「第三轮通过」的假流程。
                v = Verdict(doc.doc_id, 2, False, REJECT_NO_BODY,
                            f"正文仅 {doc.body_chars} 字，未取得可核实内容")
                report.verdicts.append(v)
                detail2.append(v.as_dict())
                continue
            next_round.append(doc)
            continue
        score, peer = _best_peer(doc, report.accepted)
        if score >= DUPLICATE_RATIO:
            v = Verdict(doc.doc_id, 2, False, REJECT_DUPLICATE,
                        f"与已收素材 {peer} 近乎逐字相同（{score:.0%}）→ 重复内容不重复入库",
                        similarity=score, peer=peer)
            report.verdicts.append(v)
            detail2.append(v.as_dict())
        elif score >= CROSS_ACCEPT:
            v = Verdict(doc.doc_id, 2, True, ACCEPT_CORROBORATED,
                        f"与已收素材 {peer} 重合 {score:.0%}，独立来源互相印证",
                        similarity=score, peer=peer)
            report.verdicts.append(v)
            report.accepted.append(doc)
            detail2.append(v.as_dict())
        else:
            next_round.append(doc)
    report.rounds.append({"round": 2, "checked": len(still),
                          "accepted": sum(1 for d in detail2 if d["accepted"]),
                          "pending": len(next_round), "detail": detail2})

    # ---- 第 3 轮：矛盾剔除 / 重复消解 / 机构源保守放行 ----
    detail3: list[dict] = []
    left: list[Candidate] = []
    for doc in next_round:
        tier = source_tier(doc.url)

        bad, peer = _contradiction(doc, report.accepted)
        if bad:
            peer_text = next((o.text for o in report.accepted if o.doc_id == peer), "")
            v = Verdict(doc.doc_id, 3, False, REJECT_CONTRADICTED,
                        f"自称与 {peer} 同文号，正文重合仅 "
                        f"{similarity(doc.text, peer_text):.0%} → 判定为篡改或编造",
                        similarity=similarity(doc.text, peer_text), peer=peer)
            report.verdicts.append(v)
            detail3.append(v.as_dict())
            continue

        if doc.body_chars < MIN_BODY_CHARS:
            v = Verdict(doc.doc_id, 3, False, REJECT_NO_BODY,
                        f"三轮均未取得正文（{doc.body_chars} 字），无法核实")
            report.verdicts.append(v)
            detail3.append(v.as_dict())
            continue

        score, peer = _best_peer(doc, report.accepted)
        if score >= DUPLICATE_RATIO and tier != TIER_AUTHORITATIVE:
            v = Verdict(doc.doc_id, 3, False, REJECT_DUPLICATE,
                        f"与 {peer} 近乎逐字相同（{score:.0%}），非权威源转载，"
                        "不入库以免索引重复", similarity=score, peer=peer)
            report.verdicts.append(v)
            detail3.append(v.as_dict())
            continue

        if score < NO_OVERLAP_RATIO and tier == TIER_UNKNOWN:
            v = Verdict(doc.doc_id, 3, False, REJECT_NO_OVERLAP,
                        f"与已收素材最高仅重合 {score:.0%}，且来源不明 → 无独立依据",
                        similarity=score, peer=peer)
            report.verdicts.append(v)
            detail3.append(v.as_dict())
            continue

        if doc.body_chars < MIN_COMPLETE_CHARS:
            # 正文只是摘要/节选：与已收内容有交叉（说明同源）但自身撑不起一条证据。
            # 收进来只会让同一段话在索引里出现两遍，检索时挤掉别的素材。
            v = Verdict(doc.doc_id, 3, False, REJECT_PARTIAL,
                        f"正文仅 {doc.body_chars} 字（节选/摘要），与已收素材重合 "
                        f"{score:.0%} → 不独立成证据，不入库",
                        similarity=score, peer=peer)
            report.verdicts.append(v)
            detail3.append(v.as_dict())
            continue

        if tier in (TIER_AUTHORITATIVE, TIER_INSTITUTIONAL):
            v = Verdict(doc.doc_id, 3, True, ACCEPT_THIRD_ROUND,
                        f"政府机构来源（{_host_of(doc.url)}）、正文完整，"
                        f"与前文重合 {score:.0%}（侧重不同），三轮后收",
                        similarity=score, peer=peer)
            report.verdicts.append(v)
            report.accepted.append(doc)
            detail3.append(v.as_dict())
            continue

        left.append(doc)
    report.rounds.append({"round": 3, "checked": len(next_round),
                          "accepted": sum(1 for d in detail3 if d["accepted"]),
                          "pending": len(left), "detail": detail3})

    # ---- 三轮后仍未定：不入库，也**不写成虚假** ----
    for doc in left:
        v = Verdict(doc.doc_id, 4, False, REJECT_UNRESOLVED,
                    "三轮筛选后仍无法确认真实性 → 不入库")
        report.verdicts.append(v)
    if left:
        report.rounds.append({"round": 4, "checked": len(left), "accepted": 0,
                              "pending": 0,
                              "detail": [v.as_dict() for v in report.verdicts
                                         if v.code == REJECT_UNRESOLVED]})
    return report


__all__ = [
    "ACCEPT_AUTHORITATIVE",
    "ACCEPT_CORROBORATED",
    "ACCEPT_THIRD_ROUND",
    "AdmissionReport",
    "AUTHORITATIVE_DOMAINS",
    "CENTRAL_GOV_HOSTS",
    "CONTRADICTION_MIN_CHARS",
    "CONTRADICTION_RATIO",
    "CROSS_ACCEPT",
    "Candidate",
    "DUPLICATE_RATIO",
    "FALSE_CODES",
    "INSTITUTIONAL_SUFFIXES",
    "MIN_BODY_CHARS",
    "MIN_COMPLETE_CHARS",
    "NO_OVERLAP_RATIO",
    "REJECT_CONTRADICTED",
    "REJECT_DUPLICATE",
    "REJECT_NO_BODY",
    "REJECT_NO_OVERLAP",
    "REJECT_PARTIAL",
    "REJECT_UNRESOLVED",
    "TIER_AUTHORITATIVE",
    "TIER_INSTITUTIONAL",
    "TIER_UNKNOWN",
    "Verdict",
    "document_numbers",
    "review",
    "shingles",
    "similarity",
    "source_tier",
]
