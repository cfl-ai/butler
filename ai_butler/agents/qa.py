"""P4 质检 Agent。

计划书依据
----------
- 七、19「**矛盾、证据缺失、过期素材可自动产出报告；质检 Agent 可依据证据做事实校验**」
- 三、P2「对外统一检索 API，供规划 Agent、**质检 Agent**、Jev 决策节点调用」
- 三、P2「Jev 决策可直接读取声明置信度」

范围
----
质检 Agent 做的是**依据证据的事实校验**，共四类判定，全部来自七、19 点名的项：
1. 证据缺失；2. 矛盾声明；3. 过期素材；4. 低置信声明。
它**不下结论、不改写声明**——只产出报告，交人工复核或调度层决策。
"""

from __future__ import annotations

# 直接运行本文件（python xxx.py / PyCharm 右键）时，sys.path[0] 是本文件所在目录
# 而非项目根，`import ai_butler` 会直接 ModuleNotFoundError。此处把项目根补进去；
# 作为包被 import 时 __package__ 非空，这段不执行。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    del _sys, _Path

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import CAP_WIKI_READ
from ai_butler.agent_wiki.ontology import CLAIM, SOURCE, SOURCE_ARCHIVED

MISSING_EVIDENCE = "证据缺失"
CONTRADICTION = "矛盾声明"
STALE_SOURCE = "过期素材"
LOW_CONFIDENCE = "低置信声明"
FINDING_KINDS: tuple[str, ...] = (MISSING_EVIDENCE, CONTRADICTION, STALE_SOURCE,
                                  LOW_CONFIDENCE)

#: 低置信判定阈值（实现参数：沿用【八】已有的 0.82，不新造第二个数字）
DEFAULT_MIN_CONFIDENCE_FALLBACK = 0.82


@dataclass
class QualityAgent(SubAgent):
    name = "qa_agent"
    writes = ("qa_report",)
    capabilities = (CAP_WIKI_READ,)
    responsibilities = "依据证据做事实校验：证据缺失 / 矛盾 / 过期 / 低置信，只出报告不改数据"

    tools: Any = None
    min_confidence: float | None = None      # None → 取 settings.jev_confidence_threshold
    settings: Any = None

    # ------------------------------------------------------------------ #
    @property
    def threshold(self) -> float:
        if self.min_confidence is not None:
            return float(self.min_confidence)
        if self.settings is not None:
            return float(self.settings.jev_confidence_threshold)
        return DEFAULT_MIN_CONFIDENCE_FALLBACK

    @staticmethod
    def _is_stale(page) -> bool:
        raw = page.extra.get("stale_after")
        if not raw:
            return False
        try:
            deadline = datetime.fromisoformat(str(raw))
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= deadline

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        store = self.tools.require(CAP_WIKI_READ)
        claims = store.list_pages_for_retrieval(uid, page_type=CLAIM)
        sources = store.list_pages_for_retrieval(uid, page_type=SOURCE)

        findings: list[dict[str, Any]] = []
        for page in claims:
            evidence = [e for e in (page.extra.get("evidence_ids") or []) if e]
            if not evidence:
                findings.append({"kind": MISSING_EVIDENCE, "page_id": page.id,
                                 "detail": "声明未绑定任何证据 ID"})
            # contradicts 是 WikiPage 的顶层字段（见 ontology.WikiPage），不在 extra 里
            if page.contradicts:
                findings.append({"kind": CONTRADICTION, "page_id": page.id,
                                 "detail": list(page.contradicts)})
            if page.confidence is not None and float(page.confidence) < self.threshold:
                findings.append({"kind": LOW_CONFIDENCE, "page_id": page.id,
                                 "detail": float(page.confidence)})

        for page in sources:
            if page.status == SOURCE_ARCHIVED or self._is_stale(page):
                findings.append({"kind": STALE_SOURCE, "page_id": page.id,
                                 "detail": page.extra.get("stale_after") or page.status})

        by_kind = {kind: sum(1 for f in findings if f["kind"] == kind)
                   for kind in FINDING_KINDS}
        return {"qa_report": {
            "user_id": uid,
            "claims_checked": len(claims),
            "sources_checked": len(sources),
            "threshold": self.threshold,
            "findings": findings,
            "by_kind": by_kind,
            "passed": not findings,
        }}

if __name__ == "__main__":  # 直接运行本文件：打印本 Agent 档案（不带参数不碰外部依赖）
    import sys

    from ai_butler.agents.cli import main

    raise SystemExit(main(sys.modules[__name__]))
