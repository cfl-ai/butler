"""P4 规划 Agent。

计划书依据
----------
- 三、P2「对外统一检索 API，供**规划 Agent**、质检 Agent、Jev 决策节点调用」
- 三、P2 触发时机「每次用户生成方案、解析需求自动检索权威资料；
  返回结果携带 Agent-Wiki 证据 ID、置信度」
- 三、P4「Wiki 约束：子 Agent 产出事实优先写入对应用户私有 Vault；
  输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID」

范围说明
--------
本 Agent 只做计划书明确规定的事：**检索权威资料 → 产出带证据绑定的事实 → 写入本人私有 Vault**。
行程编排算法属旅游垂类业务，不在本阶段发明；因此这里产出的是「取证摘要」，
供后续垂类 Agent 消费。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_WRITE
from ai_butler.agents.facts import AgentFact, FactWriter

#: 单次取证最多产出多少条事实（实现参数）
DEFAULT_MAX_FACTS = 5


@dataclass
class PlannerAgent(SubAgent):
    name = "planner_agent"
    writes = ("planner_digest",)
    capabilities = (CAP_RETRIEVAL, CAP_WIKI_WRITE)
    responsibilities = "调用统一检索 API 取证；产出事实写入本人私有 Vault 并绑定 Claim/Evidence ID"

    tools: Any = None
    max_facts: int = DEFAULT_MAX_FACTS

    @staticmethod
    def claim_id_for(*, user_id: str, statement: str, evidence_ids: tuple[str, ...]) -> str:
        digest = hashlib.sha256(
            f"{user_id}|{statement}|{'|'.join(evidence_ids)}".encode("utf-8")).hexdigest()
        return f"claim-plan-{digest[:16]}"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        query = str(state.get("utterance") or "").strip()
        if not query:
            return {"planner_digest": {"user_id": uid, "query": "", "facts": [],
                                       "skipped": "无检索词"}}

        search = self.tools.require(CAP_RETRIEVAL)
        writer: FactWriter = self.tools.require(CAP_WIKI_WRITE)
        response = search.search(query, user_id=uid, top_k=self.max_facts * 2)

        facts: list[AgentFact] = []
        skipped: list[str] = []
        for hit in response.hits:
            if len(facts) >= self.max_facts:
                break
            if not hit.evidence_ids:
                # 「强制所有素材绑定溯源来源」——无证据的检索结果不得产出事实
                skipped.append(f"{hit.chunk_id}: 缺证据 ID")
                continue
            if hit.confidence is None:
                # 置信度不得凭空编造，缺则不出事实
                skipped.append(f"{hit.chunk_id}: 缺置信度")
                continue
            statement = hit.text.strip()[:200]
            facts.append(AgentFact(
                statement=statement,
                claim_page_id=self.claim_id_for(user_id=uid, statement=statement,
                                                evidence_ids=tuple(hit.evidence_ids)),
                evidence_ids=tuple(hit.evidence_ids),
                confidence=float(hit.confidence),
                # 声明主体＝这段素材的来源（公共素材溯源库或私有源页面），
                # 客体＝命中的具体切片，两者都可在 Agent-Wiki 里回溯到。
                subject_id=hit.source_id or hit.doc_id,
                object_id=hit.chunk_id,
                claim_type="fact",
            ))

        # 风控（台账 U41）：不可信输入产出的事实只能落成 SUSPECTED
        written = writer.write_many(
            facts, user_id=uid, untrusted=bool(state.get("input_untrusted")))
        return {"planner_digest": {
            "user_id": uid, "query": query,
            "facts": [{"claim_page_id": p.id, "confidence": p.confidence,
                       "evidence_ids": list(p.extra.get("evidence_ids") or [])}
                      for p in written],
            "skipped": skipped,
            "searched_scopes": list(response.searched_scopes),
        }}
