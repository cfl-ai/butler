"""Agent-Wiki v2 编译流水线。

计划书依据
----------
- 一、项目概述「同时支持人类可读 Markdown 页面 + 机器编译缓存
  （agent-digest.json / claims.jsonl / pages.json）」
- 三、P0-1「提供编译流水线，输入 Markdown 页面输出机器可读缓存，
  自动检测声明、证据、矛盾、过期素材」
- 三、P2「过期来源标记为 status:archived 归档」
- 七、19「矛盾、证据缺失、过期素材可自动产出报告；质检 Agent 可依据证据做事实校验」
- 八、wiki_compile_auto_run / wiki_auto_report_contradiction /
      wiki_auto_report_low_confidence / wiki_max_digest_size

开源借鉴依据
-----------
- U17：claims.jsonl 字段以 Microsoft GraphRAG covariates 为起点
- U18：编译产物形态参考 Markdown + frontmatter → 机器可读缓存的范式
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_butler.agent_wiki.ontology import CLAIM, CONTRADICTION, EVIDENCE, SOURCE, WikiPage
from ai_butler.agent_wiki.workspace import SCOPE_VAULT, SCOPE_WORKSPACE, WikiStore

PAGES_JSON = "pages.json"
CLAIMS_JSONL = "claims.jsonl"
DIGEST_JSON = "agent-digest.json"
REPORT_JSON = "report.json"


@dataclass
class CompileReport:
    scope: str
    user_id: str | None
    page_count: int = 0
    claim_count: int = 0
    contradictions: list[str] = field(default_factory=list)
    low_confidence: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    digest_bytes: int = 0
    digest_truncated: bool = False
    outputs: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope, "user_id": self.user_id,
            "page_count": self.page_count, "claim_count": self.claim_count,
            "contradictions": self.contradictions,
            "low_confidence": self.low_confidence,
            "missing_evidence": self.missing_evidence,
            "archived": self.archived,
            "digest_bytes": self.digest_bytes,
            "digest_truncated": self.digest_truncated,
            "outputs": self.outputs,
        }


class WikiCompiler:
    def __init__(self, store: WikiStore) -> None:
        self.store = store
        self.settings = store.settings

    # ---------------- 对外入口 ----------------
    def compile_workspace(self) -> CompileReport:
        return self._compile(scope=SCOPE_WORKSPACE, user_id=None,
                             base=self.store.workspace_dir)

    def compile_vault(self, user_id: str) -> CompileReport:
        return self._compile(scope=SCOPE_VAULT, user_id=user_id,
                             base=self.store.vault_dir(user_id))

    # ---------------- 核心 ----------------
    def _compile(self, *, scope: str, user_id: str | None, base: Path) -> CompileReport:
        pages = self.store.list_pages(user_id=user_id)
        report = CompileReport(scope=scope, user_id=user_id, page_count=len(pages))

        claims = [p for p in pages if p.type == CLAIM]
        report.claim_count = len(claims)

        # 矛盾报告（八、wiki_auto_report_contradiction）
        if self.settings.wiki_auto_report_contradiction:
            report.contradictions = sorted(
                {p.id for p in pages if p.type == CONTRADICTION} |
                {c for p in pages for c in p.contradicts}
            )

        # 低置信报告（八、wiki_auto_report_low_confidence）
        # 阈值不新造数值：沿用计划书【八】已有的 jev_confidence_threshold = 0.82
        if self.settings.wiki_auto_report_low_confidence:
            threshold = self.settings.jev_confidence_threshold
            report.low_confidence = sorted(
                p.id for p in claims
                if p.confidence is not None and p.confidence < threshold
            )

        # 证据缺失（七、19）
        report.missing_evidence = sorted(
            p.id for p in claims if not p.extra.get("evidence_ids")
        )

        # 过期素材归档（三、P2）
        report.archived = sorted(
            p.id for p in pages
            if (p.type == SOURCE and p.status == "archived") or p.status == "ARCHIVED"
        )

        outputs: dict[str, str] = {}
        if self.settings.wiki_compile_auto_run:
            out_dir = base / "_compiled"
            out_dir.mkdir(parents=True, exist_ok=True)
            outputs[PAGES_JSON] = self._write_pages(out_dir, pages)
            outputs[CLAIMS_JSONL] = self._write_claims(out_dir, claims)
            digest, size, truncated = self.build_digest(claims)
            report.digest_bytes, report.digest_truncated = size, truncated
            digest_path = out_dir / DIGEST_JSON
            digest_path.write_text(json.dumps(digest, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            outputs[DIGEST_JSON] = str(digest_path)
            report_path = out_dir / REPORT_JSON
            report_path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            outputs[REPORT_JSON] = str(report_path)
        report.outputs = outputs
        return report

    @staticmethod
    def _write_pages(out_dir: Path, pages: list[WikiPage]) -> str:
        path = out_dir / PAGES_JSON
        index = [{"id": p.id, "type": p.type, "title": p.title, "status": p.status,
                  "confidence": p.confidence, "user_id": p.user_id,
                  "invalid_at": p.invalid_at, "contradicts": p.contradicts}
                 for p in pages]
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    @staticmethod
    def _write_claims(out_dir: Path, claims: list[WikiPage]) -> str:
        """claims.jsonl —— 每行一条声明。字段以 U17（GraphRAG covariates）为起点。"""
        path = out_dir / CLAIMS_JSONL
        with path.open("w", encoding="utf-8") as fh:
            for c in claims:
                row = {
                    "claim_id": c.id,                                   # U17
                    "claim_type": c.extra.get("claim_type"),            # U17
                    "description": c.extra.get("description"),          # U17
                    "subject_id": c.extra.get("subject_id"),            # U17
                    "object_id": c.extra.get("object_id"),              # U17
                    "status": c.status,                                 # U17 TRUE|FALSE|SUSPECTED
                    "confidence": c.confidence,
                    "valid_from": c.valid_from,                         # U17 ISO8601
                    "valid_to": c.valid_to,                             # U17 ISO8601
                    "invalid_at": c.invalid_at,                         # U16 失效时间
                    "contradicts": c.contradicts,                       # U16 矛盾关系
                    "evidence_ids": c.extra.get("evidence_ids") or [],
                    "user_id": c.user_id,                               # 公共页为 null
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(path)

    def build_digest(self, claims: list[WikiPage]) -> tuple[dict[str, Any], int, bool]:
        """构建注入 LangGraph State 的 wiki_digest。

        计划书三、P3：State 必填字段 wiki_digest；
        计划书八：wiki_max_digest_size = 12000（字节上限）。
        超限时按置信度从低到高裁剪，并置 truncated 标记，绝不静默丢内容。
        """
        limit = int(self.settings.wiki_max_digest_size)
        ordered = sorted(claims, key=lambda c: (c.confidence if c.confidence is not None else 0.0),
                         reverse=True)
        kept: list[dict[str, Any]] = []
        truncated = False
        for claim in ordered:
            entry = {
                "claim_id": claim.id,
                "subject_id": claim.extra.get("subject_id"),
                "object_id": claim.extra.get("object_id"),
                "status": claim.status,
                "confidence": claim.confidence,
            }
            candidate = {"max_digest_size": limit, "claim_count": len(kept) + 1,
                         "truncated": False, "claims": kept + [entry]}
            if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > limit:
                truncated = True
                break
            kept.append(entry)
        digest = {"max_digest_size": limit, "claim_count": len(kept),
                  "truncated": truncated, "claims": kept}
        size = len(json.dumps(digest, ensure_ascii=False).encode("utf-8"))
        return digest, size, truncated
