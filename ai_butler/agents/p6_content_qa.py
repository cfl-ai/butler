"""P6 内容质检 Agent —— 全局防错、防幻、防假。

计划书依据
----------
- 三、P6「内容质检 Agent｜全局防错、防幻、防假」
- 三、P6 核心能力「事实校验、冲突检测、幻觉拦截、账单核验、人工审核兜底；
  深度对接 Agent-Wiki 本体做事实溯源校验，**Jev 辅助可信度分类**」
- 步骤 1「开发**反向检索事实核验**机制；解析方案输出命题，查询 Wiki 的 Claim 声明、
  Evidence 证据，检测 Contradiction 矛盾；Jev 做结构化可信度分类」
- 步骤 2「开发**时间、空间、逻辑、费用冲突检测**；复用 Wiki 矛盾记录」
- 步骤 3「开发**虚构、错误、幻觉内容拦截**；无证据支撑命题**降级、提示风险**」
- 步骤 4「校验**所有方案、所有账单、所有入库数据**」+ 表注「**隐私校验项**：检测输出是否
  混入其他用户隐私；一旦发现**直接拦截输出并告警**」
- 步骤 5「实现**二次重生成**机制，超限提示信息不足；信息不足在 Wiki 生成
  **question.* 开放问题页面**标记待核实」
- 步骤 6「挂载**统一人工审核节点**；人工后台只能按 user_id 查看对应账号业务，
  **禁止批量遍历全部用户隐私**」
- 触发时机「方案生成、账单生成、数据入库前**强制校验**」

隐私校验怎么做到「不遍历他人数据」
--------------------------------
表注要求检出「输出是否混入其他用户隐私」，而步骤 6 又禁止批量遍历用户隐私——
两条合起来，唯一自洽的做法是**基于引用来源的作用域校验**，而不是把别人的数据捞出来比对：

1. **引用作用域校验**：输出引用的每个 Claim/Evidence ID，都必须能在
   「本人私有 Vault 或公共 Workspace」内解析到；解析不到即视为越界引用 → **拦截并告警**。
   系统中私有数据只能经引用进入输出，所以这一条即等价于隐私边界。
2. **显式标记扫描**：输出正文若出现**他人账号标识**（当前账号自己的除外）→ 拦截并告警。
   这是防御性的兜底，用于抓「手滑把 id 拼进文案」这类事故。

三层结论：**block（拦截）/ downgrade（降级并提示风险）/ pass（放行）**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, QUESTION, WikiPage, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.agents.capabilities import CAP_JEV, CAP_RETRIEVAL, CAP_WIKI_READ
from ai_butler.agents.phase import PhaseAgent
from ai_butler.data_platform.governance import ReviewQueue

# ============================== 结论与类别 ============================== #
ACTION_PASS = "pass"
ACTION_DOWNGRADE = "downgrade"
ACTION_BLOCK = "block"
ACTIONS: tuple[str, ...] = (ACTION_PASS, ACTION_DOWNGRADE, ACTION_BLOCK)

#: 冲突类别（计划书三、P6 步骤 2 逐字：时间、空间、逻辑、费用）
CONFLICT_TIME = "time"
CONFLICT_SPACE = "space"
CONFLICT_LOGIC = "logic"
CONFLICT_COST = "cost"
CONFLICT_KINDS: tuple[str, ...] = (CONFLICT_TIME, CONFLICT_SPACE, CONFLICT_LOGIC,
                                   CONFLICT_COST)

#: 事实核验发现（步骤 1/3）
FINDING_UNSUPPORTED = "无证据支撑"
FINDING_CONTRADICTION = "矛盾声明"
FINDING_MISSING_EVIDENCE = "证据缺失"
FINDING_LOW_CONFIDENCE = "低置信声明"
FINDING_KINDS: tuple[str, ...] = (FINDING_UNSUPPORTED, FINDING_CONTRADICTION,
                                  FINDING_MISSING_EVIDENCE, FINDING_LOW_CONFIDENCE)

#: 隐私问题
PRIVACY_CROSS_USER_REFERENCE = "越界引用他人数据"
PRIVACY_OTHER_ACCOUNT_MARK = "正文出现他人账号标识"
PRIVACY_DANGLING_REFERENCE = "引用无法在本人作用域内解析"
PRIVACY_KINDS: tuple[str, ...] = (PRIVACY_CROSS_USER_REFERENCE,
                                  PRIVACY_OTHER_ACCOUNT_MARK,
                                  PRIVACY_DANGLING_REFERENCE)

#: Jev 可信度分类（枚举；Jev 不输出自由文本）
CREDIBILITY_HIGH = "high"
CREDIBILITY_MEDIUM = "medium"
CREDIBILITY_LOW = "low"
CREDIBILITY_LABELS: tuple[str, ...] = (CREDIBILITY_HIGH, CREDIBILITY_MEDIUM,
                                       CREDIBILITY_LOW)

#: 校验对象类别（计划书三、P6 步骤 4「所有方案、所有账单、所有入库数据」）
KIND_PLAN = "plan"
KIND_BILL = "bill"
KIND_INGEST = "ingest"
KIND_CLAIM = "claim"
VERIFY_KINDS: tuple[str, ...] = (KIND_PLAN, KIND_BILL, KIND_INGEST, KIND_CLAIM)

#: 计划书写死的数字：**二次**重生成
MAX_REGENERATIONS = 2

#: 中文数字 → 阿拉伯数字。时间表达里「六点半」「七点」极常见，
#: 只认阿拉伯数字会**整类漏检**。
_CN_DIGITS: dict[str, int] = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _to_int(token: str) -> int | None:
    """把「6」「六」「十二」「二十」这类写法转成整数。"""
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if "十" in token:
        left, _, right = token.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(token)


#: 时间 / 金额 的抽取模式（实现参数）
_TIME_RE = re.compile(r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*[:：点]\s*(\d{2}|半)?")
_COST_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块|块钱)")
#: 空间语义标记：出现这些词时，object 不一致才算空间冲突
_SPACE_MARKERS: tuple[str, ...] = ("位于", "在", "出发", "到达", "地点", "入口", "地址")
#: 账号标识形态（用于显式标记扫描）
_ACCOUNT_RE = re.compile(r"user-[A-Za-z0-9_\-]{1,32}")


class QaError(RuntimeError):
    pass


# ============================== 数据结构 ============================== #
@dataclass(frozen=True)
class Proposition:
    """待校验的一条命题（来自方案 / 账单 / 入库数据）。"""

    statement: str
    kind: str = KIND_CLAIM
    subject_id: str = ""
    object_id: str = ""
    claim_page_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.statement or not str(self.statement).strip():
            raise QaError("命题内容不得为空")
        if self.kind not in VERIFY_KINDS:
            raise QaError(f"未知校验对象类别 {self.kind!r}，可选 {list(VERIFY_KINDS)}")

    @property
    def references(self) -> tuple[str, ...]:
        return tuple(x for x in (self.claim_page_id, *self.evidence_ids) if x)


@dataclass
class QaVerdict:
    user_id: str
    action: str
    checked: dict[str, int] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    privacy: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    credibility: str = ""
    regeneration: dict[str, Any] = field(default_factory=dict)
    open_questions: list[str] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    blocked_text: str = ""

    @property
    def passed(self) -> bool:
        return self.action == ACTION_PASS

    @property
    def blocked(self) -> bool:
        return self.action == ACTION_BLOCK

    def as_dict(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "action": self.action, "passed": self.passed,
                "checked": dict(self.checked), "findings": list(self.findings),
                "conflicts": list(self.conflicts), "privacy": list(self.privacy),
                "alerts": list(self.alerts), "credibility": self.credibility,
                "regeneration": dict(self.regeneration),
                "open_questions": list(self.open_questions),
                "review_items": list(self.review_items),
                "blocked_text": self.blocked_text}


# ============================== 冲突检测 ============================== #
def _times_in(text: str) -> list[str]:
    out: list[str] = []
    for raw_hour, raw_minute in _TIME_RE.findall(text):
        hour = _to_int(raw_hour)
        if hour is None or not 0 <= hour <= 24:
            continue
        minute = "30" if raw_minute == "半" else (raw_minute or "00")
        out.append(f"{hour:02d}:{minute}")
    return out


def detect_time_conflicts(items: Sequence[Proposition]) -> list[dict[str, Any]]:
    """同一主体的时间取值不一致 → 时间冲突。"""
    buckets: dict[str, set[str]] = {}
    for item in items:
        times = _times_in(item.statement)
        if times and item.subject_id:
            buckets.setdefault(item.subject_id, set()).update(times)
    return [{"kind": CONFLICT_TIME, "subject_id": subject, "values": sorted(values),
             "detail": f"同一主体出现多个时间：{sorted(values)}"}
            for subject, values in sorted(buckets.items()) if len(values) > 1]


def detect_cost_conflicts(items: Sequence[Proposition]) -> list[dict[str, Any]]:
    """同一主体的金额取值不一致 → 费用冲突。"""
    buckets: dict[str, set[str]] = {}
    for item in items:
        costs = _COST_RE.findall(item.statement)
        key = item.subject_id or item.object_id
        if costs and key:
            buckets.setdefault(key, set()).update(costs)
    return [{"kind": CONFLICT_COST, "subject_id": subject, "values": sorted(values),
             "detail": f"同一主体出现多个金额：{sorted(values)}"}
            for subject, values in sorted(buckets.items()) if len(values) > 1]


def detect_space_conflicts(items: Sequence[Proposition]) -> list[dict[str, Any]]:
    """带空间语义的命题中，同一主体指向不同客体 → 空间冲突。"""
    buckets: dict[str, set[str]] = {}
    for item in items:
        if not item.subject_id or not item.object_id:
            continue
        if any(marker in item.statement for marker in _SPACE_MARKERS):
            buckets.setdefault(item.subject_id, set()).add(item.object_id)
    return [{"kind": CONFLICT_SPACE, "subject_id": subject, "values": sorted(values),
             "detail": f"同一主体被放到多个地点：{sorted(values)}"}
            for subject, values in sorted(buckets.items()) if len(values) > 1]


def detect_logic_conflicts(items: Sequence[Proposition], *,
                           wiki: WikiStore | None = None,
                           user_id: str | None = None) -> list[dict[str, Any]]:
    """逻辑冲突：**复用 Wiki 的 Contradiction 记录**（计划书三、P6 步骤 2）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not item.claim_page_id or item.claim_page_id in seen:
            continue
        seen.add(item.claim_page_id)
        page = None
        if wiki is not None and user_id:
            page = wiki.read_page(item.claim_page_id, user_id=user_id) or \
                wiki.read_page(item.claim_page_id)
        if page is not None and page.contradicts:
            out.append({"kind": CONFLICT_LOGIC, "subject_id": item.claim_page_id,
                        "values": list(page.contradicts),
                        "detail": f"Wiki 记录了与 {list(page.contradicts)} 的矛盾"})
    return out


def detect_conflicts(items: Sequence[Proposition], *, wiki: WikiStore | None = None,
                     user_id: str | None = None) -> list[dict[str, Any]]:
    return (detect_time_conflicts(items) + detect_space_conflicts(items)
            + detect_cost_conflicts(items)
            + detect_logic_conflicts(items, wiki=wiki, user_id=user_id))


# ============================== 质检引擎 ============================== #
PROPOSITION_SPLIT = re.compile(r"[。！？\n]+")


def extract_propositions(text: str, *, kind: str = KIND_PLAN) -> list[Proposition]:
    """把一段输出解析成命题（计划书三、P6 步骤 1「解析方案输出命题」）。"""
    out = []
    for piece in PROPOSITION_SPLIT.split(text or ""):
        piece = piece.strip()
        if piece:
            out.append(Proposition(statement=piece, kind=kind))
    return out


@dataclass
class ContentVerifier:
    """P6 质检引擎。"""

    wiki: WikiStore
    retrieval: Any = None
    jev: Any = None
    review_queue: ReviewQueue | None = None
    settings: Any = None
    min_confidence: float | None = None
    account_pattern: re.Pattern[str] = _ACCOUNT_RE

    # ---------------- 阈值 ---------------- #
    @property
    def threshold(self) -> float:
        if self.min_confidence is not None:
            return float(self.min_confidence)
        return float(getattr(self.settings, "jev_confidence_threshold", 0.82))

    # ---------------- 步骤 4 表注：隐私校验 ---------------- #
    def check_privacy(self, *, user_id: str, items: Sequence[Proposition],
                      output_text: str,
                      reference_owners: Mapping[str, str | None] | None = None
                      ) -> list[dict[str, Any]]:
        """**不遍历他人数据**的隐私校验（见模块头部说明）。

        reference_owners 是生产者提供的来源表（引用 ID → 归属账号，None 表示公共）。
        为什么需要它：WikiStore 的行级隔离使得「读他人页面」与「页面不存在」都返回 None，
        二者**在读取侧不可区分**。因此：
        - 给了来源表 → 能直接判出「越界引用他人数据」；
        - 没给 → 只能观测到「解析不到」，而它同样被拦截，因为隔离之下
          越界引用**正是以解析不到的形式表现出来**。
        """
        problems: list[dict[str, Any]] = []
        owners = dict(reference_owners or {})

        for item in items:
            for ref in item.references:
                owner = owners.get(ref, "__unknown__")
                if owner != "__unknown__" and owner not in (None, user_id):
                    problems.append({"kind": PRIVACY_CROSS_USER_REFERENCE, "ref": ref,
                                     "detail": f"引用 {ref} 归属 {owner}，非本人"})
                    continue
                own = self.wiki.read_page(ref, user_id=user_id)
                if own is not None:
                    if own.user_id not in (None, user_id):
                        problems.append({"kind": PRIVACY_CROSS_USER_REFERENCE, "ref": ref,
                                         "detail": f"引用 {ref} 归属 {own.user_id}，非本人"})
                    continue
                if self.wiki.read_page(ref) is None:
                    problems.append({
                        "kind": PRIVACY_DANGLING_REFERENCE, "ref": ref,
                        "detail": (f"引用 {ref} 在本人 Vault 与公共 Workspace 内均无法解析；"
                                   f"在行级隔离之下，越界引用他人数据也表现为这一形态")})

        for mark in sorted(set(self.account_pattern.findall(output_text or ""))):
            if mark != user_id:
                problems.append({"kind": PRIVACY_OTHER_ACCOUNT_MARK, "ref": mark,
                                 "detail": f"正文出现他人账号标识 {mark}"})
        return problems

    # ---------------- 步骤 1：反向检索事实核验 ---------------- #
    def verify_propositions(self, *, user_id: str,
                            items: Sequence[Proposition]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for item in items:
            if not item.evidence_ids:
                findings.append({"kind": FINDING_MISSING_EVIDENCE,
                                 "statement": item.statement,
                                 "detail": "命题未绑定 Evidence 证据 ID"})
            if item.confidence is not None and item.confidence < self.threshold:
                findings.append({"kind": FINDING_LOW_CONFIDENCE,
                                 "statement": item.statement,
                                 "detail": f"置信 {item.confidence:.3f} < {self.threshold}"})
            if item.claim_page_id:
                page = (self.wiki.read_page(item.claim_page_id, user_id=user_id)
                        or self.wiki.read_page(item.claim_page_id))
                if page is not None and page.contradicts:
                    findings.append({"kind": FINDING_CONTRADICTION,
                                     "statement": item.statement,
                                     "detail": list(page.contradicts)})
        return findings

    def reverse_search_unsupported(self, *, user_id: str, text: str,
                                   limit: int = 5) -> list[dict[str, Any]]:
        """**反向检索**：拿输出命题当查询去反查知识库，查不到支撑即为疑似幻觉。"""
        if self.retrieval is None or not (text or "").strip():
            return []
        findings: list[dict[str, Any]] = []
        for prop in extract_propositions(text)[:limit]:
            response = self.retrieval.search(prop.statement, user_id=user_id, top_k=3)
            supported = any(hit.evidence_ids for hit in response.hits)
            if not supported:
                findings.append({"kind": FINDING_UNSUPPORTED, "statement": prop.statement,
                                 "detail": "反向检索未找到任何带证据的支撑素材"})
        return findings

    # ---------------- 步骤 5：开放问题页 + 二次重生成 ---------------- #
    def raise_open_questions(self, *, user_id: str, statements: Sequence[str],
                             now: str = "") -> list[str]:
        """信息不足时在 Wiki 生成 question.* 页面标记待核实。"""
        created: list[str] = []
        for index, statement in enumerate(statements, start=1):
            page_id = f"question-{abs(hash((user_id, statement))) % (10 ** 12):012d}"
            if self.wiki.read_page(page_id, user_id=user_id) is not None:
                created.append(page_id)
                continue
            self.wiki.write_page(new_page(
                QUESTION, page_id, title=f"待核实：{statement[:40]}", user_id=user_id,
                now=now, text=f"信息不足，待核实：{statement}",
                raised_by="p6_content_qa", sequence=index), user_id=user_id)
            created.append(page_id)
        return created

    @staticmethod
    def regeneration_plan(*, attempt: int) -> dict[str, Any]:
        """二次重生成机制：还有额度就重生成，超限即提示信息不足。"""
        allowed = attempt < MAX_REGENERATIONS
        return {"attempt": attempt, "max": MAX_REGENERATIONS, "allowed": allowed,
                "exhausted": not allowed,
                "message": "" if allowed else "信息不足，已无法通过重新生成补齐，请补充更多信息"}

    # ---------------- 步骤 6：人工审核节点 ---------------- #
    def enqueue_review(self, *, user_id: str, verdict: QaVerdict) -> list[str]:
        if self.review_queue is None:
            return []
        items: list[str] = []
        for problem in verdict.privacy:
            item = self.review_queue.enqueue(
                kind="privacy", ref_id=f"{user_id}:{problem['ref']}",
                reason=problem["detail"], user_id=user_id,
                payload={"action": verdict.action})
            items.append(item.item_id)
        for conflict in verdict.conflicts:
            item = self.review_queue.enqueue(
                kind="conflict", ref_id=f"{user_id}:{conflict['subject_id']}",
                reason=conflict["detail"], user_id=user_id)
            items.append(item.item_id)
        return items

    # ---------------- Jev 可信度分类 ---------------- #
    async def classify_credibility(self, *, user_id: str, digest: Mapping[str, Any]) -> str:
        """步骤 1「Jev 做结构化可信度分类」。Jev 不可用时按规则兜底，不假装有结论。"""
        if self.jev is None:
            return ""
        decision = await self.jev.decide(
            user_id=user_id,
            question=("请对本次质检结果做可信度分类。"
                      f"隐私问题 {digest['privacy_count']} 项；"
                      f"冲突 {digest['conflict_count']} 项；"
                      f"证据缺失 {digest['missing_evidence']} 项；"
                      f"疑似幻觉 {digest['unsupported']} 项"),
            allowed_labels=CREDIBILITY_LABELS)
        return decision.label if decision.accepted and decision.label else ""

    # ---------------- 主流程 ---------------- #
    async def verify(self, *, user_id: str, items: Sequence[Proposition],
                     output_text: str = "", attempt: int = 0, now: str = "",
                     reference_owners: Mapping[str, str | None] | None = None
                     ) -> QaVerdict:
        if not user_id or not str(user_id).strip():
            raise QaError("质检必须携带 user_id（计划书三、P4 隐私约束）")

        problem_items = [i for i in items if i.kind in (KIND_BILL, KIND_INGEST)]
        findings = self.verify_propositions(user_id=user_id, items=items)
        findings += self.reverse_search_unsupported(user_id=user_id, text=output_text)
        conflicts = detect_conflicts(items, wiki=self.wiki, user_id=user_id)
        privacy = self.check_privacy(user_id=user_id, items=items,
                                     output_text=output_text,
                                     reference_owners=reference_owners)

        # 计划书三、P6 步骤 4：账单与入库数据**强制**校验，不得空过
        forced = [i.kind for i in problem_items]
        missing_evidence = sum(1 for f in findings if f["kind"] == FINDING_MISSING_EVIDENCE)
        unsupported = sum(1 for f in findings if f["kind"] == FINDING_UNSUPPORTED)

        verdict = QaVerdict(
            user_id=user_id, action=ACTION_PASS,
            checked={"propositions": len(items), "forced_kinds": forced,
                     "conflicts": len(conflicts), "privacy": len(privacy)},
            findings=findings, conflicts=conflicts, privacy=privacy)

        # 结论判定：隐私问题一律拦截；其余按严重度降级
        if privacy:
            verdict.action = ACTION_BLOCK
            verdict.blocked_text = output_text
            verdict.alerts = [p["detail"] for p in privacy]
        elif conflicts or unsupported:
            verdict.action = ACTION_DOWNGRADE
        elif missing_evidence:
            verdict.action = ACTION_DOWNGRADE
        else:
            verdict.action = ACTION_PASS

        verdict.regeneration = self.regeneration_plan(attempt=attempt)
        if verdict.action != ACTION_PASS:
            verdict.open_questions = self.raise_open_questions(
                user_id=user_id,
                statements=[f["statement"] for f in findings if f.get("statement")][:3],
                now=now)
        verdict.credibility = await self.classify_credibility(
            user_id=user_id,
            digest={"privacy_count": len(privacy), "conflict_count": len(conflicts),
                    "missing_evidence": missing_evidence, "unsupported": unsupported})
        verdict.review_items = self.enqueue_review(user_id=user_id, verdict=verdict)
        return verdict


# ============================== 阶段 Agent ============================== #
def collect_propositions(state: Mapping[str, Any]) -> list[Proposition]:
    """从 State 收集待校验内容：**所有方案、所有账单、所有入库数据**（步骤 4）。"""
    out: list[Proposition] = []

    plan = state.get("p5_plan") or {}
    if isinstance(plan, dict):
        for item in (plan.get("items") or []):
            statement = str((item or {}).get("statement") or "").strip()
            if not statement:
                continue
            out.append(Proposition(
                statement=statement, kind=KIND_PLAN,
                claim_page_id=str(item.get("claim_page_id") or ""),
                evidence_ids=tuple(item.get("evidence_ids") or ()),
                confidence=item.get("confidence")))

    for bill in (state.get("bills") or []):
        statement = str((bill or {}).get("statement") or "").strip()
        if not statement:
            continue
        out.append(Proposition(
            statement=statement, kind=KIND_BILL,
            subject_id=str(bill.get("subject_id") or ""),
            object_id=str(bill.get("object_id") or ""),
            claim_page_id=str(bill.get("claim_page_id") or ""),
            evidence_ids=tuple(bill.get("evidence_ids") or ()),
            confidence=bill.get("confidence")))

    for row in (state.get("pending_ingest") or []):
        statement = str((row or {}).get("statement") or "").strip()
        if not statement:
            continue
        out.append(Proposition(
            statement=statement, kind=KIND_INGEST,
            subject_id=str(row.get("subject_id") or ""),
            claim_page_id=str(row.get("claim_page_id") or ""),
            evidence_ids=tuple(row.get("evidence_ids") or ()),
            confidence=row.get("confidence")))
    return out


@dataclass
class ContentQaAgent(PhaseAgent):
    """P6 内容质检 Agent（阶段视图 + 强制校验入口）。"""

    phase_code: ClassVar[str] = "P6"
    writes: ClassVar[tuple[str, ...]] = ("p6_verdict",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_WIKI_READ, CAP_RETRIEVAL, CAP_JEV)

    review_queue: ReviewQueue | None = None
    min_confidence: float | None = None

    def verifier(self) -> ContentVerifier:
        return ContentVerifier(
            wiki=self.tools.require(CAP_WIKI_READ),
            retrieval=self.tools.require(CAP_RETRIEVAL),
            jev=self.tools.require(CAP_JEV),
            review_queue=self.review_queue,
            settings=self.settings,
            min_confidence=self.min_confidence)

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        items = collect_propositions(state)
        output_text = str(state.get("plan_text") or state.get("p5_plan", {})
                          .get("draft") if isinstance(state.get("p5_plan"), dict)
                          else state.get("plan_text") or "")

        verdict = await self.verifier().verify(
            user_id=uid, items=items, output_text=output_text,
            attempt=int(state.get("regeneration_attempt") or 0))
        payload = verdict.as_dict()
        payload["phase"] = self.phase_code
        return {"p6_verdict": payload}
