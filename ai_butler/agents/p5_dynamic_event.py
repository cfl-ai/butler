"""P5 动态事件运营 Agent —— 核心智能方案生成引擎。

计划书依据
----------
- 三、P5「动态事件运营 Agent｜核心智能方案生成引擎」
- 三、P5 核心能力「多约束推理、动态重规划、流式输出、场景自适应；基于 Agent-Wiki 声明
  与证据生成方案；**重规划条件判断交由 Jev 高速决策，条件成立才调用大模型生成**」
- 三、P5 开发步骤 1「对接数据中台读取用户画像、习惯、历史数据；传入 state 的 user_id，
  SDK 自动过滤，仅读取该账号画像；**读取私有 Vault 实体与偏好声明**」
- 三、P5 开发步骤 2「对接 RAG-Agent-Wiki 知识库获取实时权威素材；**合并公共 Workspace
  与用户私有 Vault**，携带证据、置信度」
- 三、P5 开发步骤 3「开发多约束智能推理逻辑；**低置信 Wiki 信息输出时做风险标注**」
- 三、P5 开发步骤 4「开发**位置、天气、时间、突发场景自动重规划**机制；
  **重规划条件交由 Jev 结构化判定**」
- 三、P5 开发步骤 5「接入 LangGraph **循环熔断、无增量终止**机制」
- 三、P5 开发步骤 6「开发**流式 Token 输出**，支撑手机端逐字文字 + 语音同步播报」
- 三、P5 触发时机「用户提问、场景变化、设备变化、规划条件变更自动触发；前提为登录会话有效」

快慢思考的落点
--------------
本 Agent 是**慢思考（System-Two）**的执行体，但**不自行决定何时慢思考**：
先由 Jev（快思考）结构化判定「要不要重新生成」，条件成立才调大模型。
这样昂贵的开放式生成不会在每一轮都发生。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Sequence

from ai_butler.agent_wiki.ontology import CLAIM, ENTITY
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_JEV,
    CAP_LLM,
    CAP_RETRIEVAL,
    CAP_WIKI_READ,
)
from ai_butler.agents.phase import PhaseAgent
from ai_butler.agents.risk import (
    AnnotatedItem,
    OutputItem,
    RiskAnnotator,
    RiskPolicy,
    SpeechChunk,
    split_for_speech,
)
from ai_butler.infra.llm_gateway import ChatMessage
from ai_butler.scheduler.graph import MAX_ROLLBACKS

#: 重规划判定的枚举标签（Jev 只吃枚举，见三、P3）
REPLAN = "replan"
REPLAN_SKIP = "skip_replan"
REPLAN_LABELS: tuple[str, ...] = (REPLAN, REPLAN_SKIP)

#: 约束类别（计划书三、P5 开发步骤 4 逐字点名）
CONSTRAINT_LOCATION = "location"
CONSTRAINT_WEATHER = "weather"
CONSTRAINT_TIME = "time"
CONSTRAINT_DISRUPTION = "disruption"
CONSTRAINT_PREFERENCE = "preference"
CONSTRAINT_KINDS: tuple[str, ...] = (CONSTRAINT_LOCATION, CONSTRAINT_WEATHER,
                                     CONSTRAINT_TIME, CONSTRAINT_DISRUPTION,
                                     CONSTRAINT_PREFERENCE)

#: 生成方案的默认提示模板（计划书三、P0-2 的 Prompt 版本库负责其版本管理）
DEFAULT_PLAN_PROMPT = (
    "你是行程方案生成引擎。只能依据「已检索到的权威素材」作答，"
    "不得引入素材之外的事实。\n"
    "用户诉求：{query}\n"
    "约束条件：{constraints}\n"
    "可用素材（每条带证据与置信度）：{evidence}\n"
    "请输出一段简洁的中文方案，不要编造素材中没有的信息。"
)

#: 方案里最多回传多少条流式分片（实现参数）
MAX_SPEECH_CHUNKS = 40


class PlanningError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlanConstraint:
    kind: str
    value: str
    source: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CONSTRAINT_KINDS:
            raise PlanningError(f"未知约束类别 {self.kind!r}，可选 {list(CONSTRAINT_KINDS)}")
        if not str(self.value).strip():
            raise PlanningError("约束取值不得为空")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "source": self.source}


def collect_constraints(state: Mapping[str, Any]) -> list[PlanConstraint]:
    """从 State 收集位置 / 天气 / 时间 / 突发四类约束。

    只搬运 State 与感知快照里已有的信息，**不臆造缺失的约束**。
    """
    out: list[PlanConstraint] = []

    location = str(state.get("location") or "").strip()
    if location:
        out.append(PlanConstraint(CONSTRAINT_LOCATION, location, source="state.location"))

    weather = state.get("weather_snapshot") or {}
    if isinstance(weather, dict):
        query = str(weather.get("query") or "").strip()
        if query:
            out.append(PlanConstraint(CONSTRAINT_WEATHER, query,
                                      source="weather_snapshot"))

    progress = state.get("time_progress") or {}
    if isinstance(progress, dict) and progress.get("remaining_seconds") is not None:
        out.append(PlanConstraint(
            CONSTRAINT_TIME, f"剩余 {int(progress['remaining_seconds'])} 秒",
            source="time_progress"))

    disruption = str(state.get("disruption") or "").strip()
    if disruption:
        out.append(PlanConstraint(CONSTRAINT_DISRUPTION, disruption,
                                  source="state.disruption"))
    return out


@dataclass
class DynamicEventAgent(PhaseAgent):
    phase_code: ClassVar[str] = "P5"
    writes: ClassVar[tuple[str, ...]] = ("p5_plan",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_DATA_READ, CAP_WIKI_READ,
                                               CAP_RETRIEVAL, CAP_LLM, CAP_JEV)

    prompt_template: str = DEFAULT_PLAN_PROMPT
    max_items: int = 5

    # ------------------------------------------------------------------ #
    @staticmethod
    def _preference_constraints(claims: Sequence[Any]) -> list[PlanConstraint]:
        """把私有 Vault 里的偏好声明转成约束（计划书「读取私有 Vault 实体与偏好声明」）。"""
        out: list[PlanConstraint] = []
        for page in claims:
            description = str(page.extra.get("description") or "").strip()
            if description and str(page.extra.get("claim_type")) == "preference":
                out.append(PlanConstraint(CONSTRAINT_PREFERENCE, description,
                                          source=page.id))
        return out

    @staticmethod
    def _evidence_lines(hits: Sequence[Any]) -> tuple[list[str], list[OutputItem]]:
        lines: list[str] = []
        items: list[OutputItem] = []
        for hit in hits:
            statement = str(hit.text).strip()
            evidence = tuple(getattr(hit, "evidence_ids", ()) or ())
            confidence = getattr(hit, "confidence", None)
            lines.append(f"- {statement}（证据 {list(evidence)}，"
                         f"置信 {confidence if confidence is not None else '未知'}）")
            items.append(OutputItem(statement=statement, confidence=confidence,
                                    evidence_ids=evidence,
                                    claim_page_id=str(getattr(hit, "page_id", "") or "")))
        return lines, items

    # ------------------------------------------------------------------ #
    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        # 计划书三、P5 触发时机：前提为登录会话有效（SubAgent.__call__ 已保证）
        uid = self.require_user_id(state)
        query = str(state.get("utterance") or "").strip()

        sdk = self.tools.require(CAP_DATA_READ)
        wiki = self.tools.require(CAP_WIKI_READ)
        service = self.tools.require(CAP_RETRIEVAL)

        # --- 步骤 1：用户画像 + 私有 Vault 实体与偏好声明 ---
        profile = await sdk.read_profile(user_id=uid, limit=50)
        entities = wiki.list_pages_for_retrieval(uid, page_type=ENTITY)
        claims = wiki.list_pages_for_retrieval(uid, page_type=CLAIM)

        # --- 步骤 2：检索权威素材（公共 Workspace + 本人私有 Vault）---
        response = service.search(query, user_id=uid,
                                  top_k=self.max_items * 2) if query else None
        hits = list(response.hits) if response is not None else []
        evidence_lines, items = self._evidence_lines(hits)

        # --- 步骤 3：多约束收集 ---
        constraints = (collect_constraints(state)
                       + self._preference_constraints(claims))

        # --- 步骤 5：循环熔断（先于任何昂贵调用判断）---
        rollback = int(state.get("rollback_count") or 0)
        circuit_broken = rollback >= MAX_ROLLBACKS

        # --- 步骤 4：重规划条件交由 Jev 结构化判定 ---
        jev = self.tools.require(CAP_JEV)
        replan = {"required": False, "label": "", "confidence": 0.0, "reason": ""}
        if circuit_broken:
            replan["reason"] = f"已达循环熔断上限 {MAX_ROLLBACKS} 次，不再判定重规划"
        elif not query:
            replan["reason"] = "无用户诉求，不触发重规划"
        else:
            decision = await jev.decide(
                user_id=uid,
                question=("是否需要用大模型重新生成行程方案？"
                          f"诉求：{query}；约束：{[c.value for c in constraints]}；"
                          f"已有方案：{'有' if state.get('p5_plan') else '无'}"),
                allowed_labels=REPLAN_LABELS)
            replan = {"required": bool(decision.accepted and decision.label == REPLAN),
                      "label": decision.label or "", "confidence": decision.confidence,
                      "reason": decision.reason or decision.source}

        # --- 步骤 4 续：**条件成立才调用大模型生成** ---
        draft, generated = "", False
        if replan["required"] and not circuit_broken:
            llm = self.tools.require(CAP_LLM)
            prompt = self.prompt_template.format(
                query=query,
                constraints="；".join(f"{c.kind}={c.value}" for c in constraints) or "无",
                evidence="\n".join(evidence_lines) or "无可用素材")
            result = await llm.chat(user_id=uid,
                                    messages=[ChatMessage("system", prompt)],
                                    max_tokens=512)
            draft, generated = result.text.strip(), True
        elif state.get("previous_plan"):
            draft = str(state["previous_plan"])

        # --- 步骤 3 续：低置信信息风险标注 ---
        annotator = RiskAnnotator(RiskPolicy(
            min_confidence=float(getattr(self.settings, "jev_confidence_threshold",
                                         0.82))))
        annotated: list[AnnotatedItem] = annotator.annotate(items)

        # --- 步骤 6：流式分片，供逐字渲染 + 同步 TTS ---
        payload_text = draft or "".join(i.statement for i in annotated)
        chunks: list[SpeechChunk] = split_for_speech(payload_text)[:MAX_SPEECH_CHUNKS]

        return {"p5_plan": {
            "user_id": uid, "phase": self.phase_code, "query": query,
            "profile_rows": len(profile),
            "vault_entities": len(entities), "vault_claims": len(claims),
            "constraints": [c.as_dict() for c in constraints],
            "replan": replan,
            "generated_by_llm": generated,
            "draft": draft,
            "items": [i.as_dict() for i in annotated],
            "risks": annotator.risks(annotated),
            "risk_banner": annotator.banner(annotated),
            "speech_chunks": [c.as_dict() for c in chunks],
            "stream_ready": bool(chunks),
            "searched_scopes": list(response.searched_scopes) if response else [],
            "circuit_broken": circuit_broken,
            "max_rollbacks": MAX_ROLLBACKS,
            "plan_id": hashlib.sha256(f"{uid}|{query}".encode("utf-8")).hexdigest()[:16],
        }}
