"""P4 子 Agent 产出事实的契约与私有 Vault 回写通道。

计划书依据
----------
- 三、P4「Wiki 约束：子 Agent 产出事实**优先写入对应用户私有 Vault**；
  **输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID**」
- 三、P4「隐私约束：读写私有数据必须从 LangGraph State 获取 user_id，
  不允许硬编码、不能为空；禁止读取其他 user_id 数据」
- 三、P2「强制所有素材绑定溯源来源，对应 Wiki Evidence 证据条目」

约束如何变成代码
----------------
- 事实缺 claim_page_id 或缺 evidence_ids ⇒ 构造即抛错（不是靠调用方自觉）；
- 回写必须给出 user_id，且只写该账号的 Vault；
- 证据条目必须真实存在（公共 Workspace 或该账号 Vault 中找得到），否则拒绝落库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, WikiPage, new_page
from ai_butler.agent_wiki.workspace import WikiStore


class FactError(ValueError):
    """产出事实违反 P4 契约。"""


@dataclass(frozen=True)
class AgentFact:
    """子 Agent 产出的一条事实。

    计划书要求「输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID」，
    因此这两个字段**没有默认值**，缺失即无法构造。
    """

    statement: str
    claim_page_id: str
    evidence_ids: tuple[str, ...]
    confidence: float
    #: Claim 主体与客体。本体（ontology.REQUIRED_EXTRA[CLAIM]）把这两个字段列为必填，
    #: 因为「一条声明」必须说明它**关于什么**；留空会直接违反 U17 的字段约定。
    subject_id: str
    object_id: str
    claim_type: str = "fact"

    def __post_init__(self) -> None:
        if not self.statement or not str(self.statement).strip():
            raise FactError("事实内容不得为空")
        if not self.claim_page_id or not str(self.claim_page_id).strip():
            raise FactError(
                "事实必须绑定 Claim 声明 ID（计划书三、P4「输出事实绑定 Claim 声明 ID」）")
        ids = tuple(dict.fromkeys(e for e in self.evidence_ids if e))
        if not ids:
            raise FactError(
                "事实必须绑定至少一条 Evidence 证据 ID（计划书三、P4「绑定 Evidence 证据 ID」）")
        if not self.subject_id or not self.object_id:
            raise FactError(
                "声明必须给出 subject_id 与 object_id——本体要求 Claim 说明它关于什么（U17）")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise FactError(f"置信度必须落在 0~1，实际 {self.confidence!r}")
        object.__setattr__(self, "evidence_ids", ids)

    def as_claim_extra(self) -> dict[str, Any]:
        return {"claim_type": self.claim_type, "description": self.statement,
                "evidence_ids": list(self.evidence_ids),
                "subject_id": self.subject_id, "object_id": self.object_id}


class FactWriter:
    """子 Agent 产出事实的唯一出口：写入**对应用户私有 Vault**。"""

    def __init__(self, wiki: WikiStore, *, require_evidence_pages: bool = True) -> None:
        self.wiki = wiki
        self.require_evidence_pages = require_evidence_pages

    # ---------------- 校验 ---------------- #
    @staticmethod
    def _require_user(user_id: str | None) -> str:
        if not user_id or not str(user_id).strip():
            raise FactError(
                "回写事实必须携带 user_id，且不得为空（计划书三、P4 隐私约束）")
        return str(user_id)

    def find_evidence(self, evidence_id: str, *, user_id: str) -> tuple[str, WikiPage] | None:
        """证据可在该账号 Vault 或公共 Workspace；**绝不会去看别的账号**。"""
        private = self.wiki.read_page(evidence_id, user_id=user_id)
        if private is not None and private.type == EVIDENCE:
            return "vault", private
        public = self.wiki.read_page(evidence_id)
        if public is not None and public.type == EVIDENCE:
            return "workspace", public
        return None

    def verify(self, fact: AgentFact, *, user_id: str) -> None:
        self._require_user(user_id)
        if not self.require_evidence_pages:
            return
        missing = [e for e in fact.evidence_ids
                   if self.find_evidence(e, user_id=user_id) is None]
        if missing:
            raise FactError(
                f"证据条目不存在或不属于该账号：{missing}。"
                f"计划书三、P2：强制所有素材绑定溯源来源，对应 Wiki Evidence 证据条目。")

    # ---------------- 回写 ---------------- #
    #: 风控（台账 U41）：输入被标为不可信时，Claim 只能落成「待核实」。
    #: 这**是**「不得直接沉淀为事实」的可执行定义 —— 不是靠下游自觉。
    SUSPECTED_STATUS = "SUSPECTED"

    def write(self, fact: AgentFact, *, user_id: str,
              untrusted: bool = False) -> WikiPage:
        uid = self._require_user(user_id)
        self.verify(fact, user_id=uid)

        existing = self.wiki.read_page(fact.claim_page_id, user_id=uid)
        page = existing if existing is not None else new_page(
            CLAIM, fact.claim_page_id, title=fact.statement[:60], user_id=uid,
            confidence=fact.confidence, **fact.as_claim_extra())
        if existing is not None:
            if existing.user_id != uid:
                raise FactError(
                    f"Claim {fact.claim_page_id!r} 归属 {existing.user_id!r}，"
                    f"拒绝由 {uid!r} 改写")
            page.confidence = fact.confidence
            page.extra.update(fact.as_claim_extra())
        if untrusted:
            # 不可信输入：证据再全，也不得直接落成 TRUE。
            # 保留页面（便于人工复核），但状态降为 SUSPECTED 并留下标记。
            page.status = self.SUSPECTED_STATUS
            page.extra["untrusted_input"] = True
        # 「优先写入对应用户私有 Vault」——私有页必须带 user_id
        self.wiki.write_page(page, user_id=uid)
        return page

    def write_many(self, facts: Sequence[AgentFact], *, user_id: str,
                   untrusted: bool = False) -> list[WikiPage]:
        return [self.write(f, user_id=user_id, untrusted=untrusted) for f in facts]
