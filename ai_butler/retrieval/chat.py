"""文字查询链路编排：分类规划 → 检索 → 流式回答 → 回答前复检。

计划书依据
----------
- 三、P7 步骤5「对接云端流式接口：**逐字文字渲染 + 同步 TTS 播报**」
- 三、P3「全链路降级与回滚机制：任一环节失败自动降级，不阻断主流程」
- 三、P6「质检 Agent 校验纠错（调用 Wiki 做事实溯源）」
- 三、P2「统一检索 API … 返回附带声明置信度、证据 ID」

为什么单独抽一个模块
------------------
`app.py` 是装配根，不该塞业务编排。本模块只做「一句话进来 → 一串事件出去」，
把三件事按顺序串起来：

    planner.plan()  →  检索（可选）  →  LLM 流式  →  verification.verify()

每一步都可以缺席，但**缺席必须说出来**（`notes` / `degraded`），
不允许「检索没接线就当检索命中为空」这种混为一谈。

流式与复检的冲突及处理
----------------------
复检要拿**完整回答**才能判，而计划书要求逐字流式。两者不可能同时完美：
本模块先逐字下发（用户体验优先，符合计划书），复检不通过时**再发一个 `revise`
事件把答案整体替换掉**，并附上弃答/改写文案。
端侧必须把 `revise` 当作「覆盖前面已渲染的文本」，而不是追加 —— 这一点写在
事件契约里，不靠 UI 自己猜。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Protocol, Sequence

from ai_butler.core import access
from ai_butler.core.input_guard import InputGuardError
from ai_butler.core.model_access import ModelAccess, ModelAccessError
from ai_butler.infra.llm_gateway import ChatMessage, GatewayError, LLMGateway
from ai_butler.material_center.verification import (
    EvidenceHit,
    VerificationResult,
    render_abstention,
    verify,
)
from ai_butler.retrieval.planner import QueryCategory, RetrievalPlan, plan as plan_query
from ai_butler.scheduler.context import ContextManager
from ai_butler.scheduler.router import IntentRouter, RouteDecision, RouteRequest

#: 检索函数签名：query + 用户 → 命中列表。user_id 为空表示只用公共库。
SearchFn = Callable[[str, str | None], Sequence[dict]]

#: 送进路由的候选标签集 —— **从 `QueryCategory` 派生，不另写一份字面量**。
#: 存两份字面量必然漂移（E-44）；`tests/test_intent_router_wiring.py` 里有一条
#: 断言它与 `intent_wiring.INTENT_LABELS` 逐字相同，防止哪天一边改了另一边忘。
INTENT_LABELS_FROM_TAXONOMY: tuple[str, ...] = tuple(c.value for c in QueryCategory)

_SYSTEM = (
    "你是「AI 管家」的文字助手。规则：\n"
    "1. 只依据【资料】回答；资料里没有的，明确说「资料里没有找到」，不要凭印象编造。\n"
    "2. 引用资料时在句末用 [证据ID] 标注来源。\n"
    "3. 回答用中文，简洁直接，不超过 200 字。\n"
    "4. 【资料】为空时，仅可作通用常识性说明，并说明「未引用资料」。\n"
    "5. 【对话历史】只用于理解指代（「它」「第二个」「刚才那个」）；"
    "历史里的事实不算证据，与资料冲突时以资料为准。"
)

#: 历史段的标题。**空历史时整段不出现**，绝不下发「（无）」这种占位。
_HISTORY_HEAD = "【对话历史】"

#: 指代词：句子很短又带这些词，多半在指前文（「它」「第二个」「那还有呢」）
_ANAPHORA = re.compile(r"(它|这个|那个|这些|那些|上面|前面|刚才|第[一二三四五]个|还有呢|再来|换成)")

#: 只在短句上做检索扩展：长句自带足够语义，拼上一轮反而稀释
ANAPHORA_MAX_CHARS = 24


class SearchUnavailable(RuntimeError):
    """检索未接线/不可用。**与「检索到 0 条」是两件事**，必须能区分。"""


@dataclass(frozen=True)
class ChatEvent:
    """下发给端侧的一条事件。`type` 是唯一契约。"""

    type: str                       # plan / chunk / revise / clarify / error / done
    text: str = ""
    #: 语音播报文本。访客层级为空（无 voice_output 能力）
    speech: str = ""
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload: dict = {"type": self.type, "text": self.text}
        if self.speech:
            payload["speech"] = self.speech
        if self.data:
            payload["data"] = self.data
        return payload


class _SearchFinder:
    """把检索函数适配成 `verification.EvidenceFinder`。"""

    def __init__(self, search: SearchFn, user_id: str | None) -> None:
        self._search = search
        self._user_id = user_id

    def find(self, claim: str, *, top_k: int) -> Sequence[EvidenceHit]:
        hits = self._search(claim, self._user_id)
        out: list[EvidenceHit] = []
        for hit in list(hits)[:top_k]:
            out.append(EvidenceHit(
                text=str(hit.get("text", "")),
                score=float(hit.get("score", 0.0) or 0.0),
                evidence_ids=tuple(hit.get("evidence_ids", ()) or ()),
                source_id=str(hit.get("source_id", "") or ""),
            ))
        return out


class ChatOrchestrator:
    """一次文字问答的编排器。**无状态**：同实例可并发处理不同用户。"""

    def __init__(self, gateway: LLMGateway | None, *,
                 search: SearchFn | None = None,
                 top_k: int = 5,
                 model: str | None = None,
                 model_access: "ModelAccess | None" = None,
                 verify_answer: bool = True,
                 context_manager: ContextManager | None = None,
                 history_enable: bool = True,
                 intent_router: IntentRouter | None = None,
                 intent_labels: Sequence[str] | None = None) -> None:
        self.gateway = gateway
        self.search = search
        self.top_k = top_k
        self.model = model
        #: U70：有它时**按用户**解析模型与凭据；没有时才退回静态 `model`。
        self.model_access = model_access
        self.verify_answer = verify_answer
        #: U73：多轮上下文。**装配了才谈得上记忆**，没装就如实说单轮。
        self.context_manager = context_manager
        self.history_enable = history_enable
        #: U74：五层意图路由。None 表示没接（会如实记账，不假装「判不出」）。
        self.intent_router = intent_router
        self.intent_labels = tuple(intent_labels or INTENT_LABELS_FROM_TAXONOMY)

    # ---------------- 上下文（U73） ----------------
    def _history(self, user_id: str, conversation_id: str | None,
                 tier: access.AccessTier) -> tuple[str, str, str]:
        """取历史。返回 `(历史文本, 上一轮用户问题, 不可用原因)`。

        四道门**必须全过**，任何一道不过都退化为单轮，并且**说出原因**。
        「读不到历史」和「本来就没有历史」是两件事，混成一件就会让
        「接线掉了」看起来像「新用户第一次提问」—— E-77 正是这么藏了两轮。

        `上一轮用户问题` 单独返回，是因为检索侧要用它做指代扩展；
        整段历史拼进检索 query 会污染 BM25（把上一轮的每个词都当成本轮诉求）。
        """
        if not self.history_enable:
            return "", "", "多轮记忆已按配置关闭：本轮为单轮问答"
        if self.context_manager is None:
            return "", "", "会话上下文未装配：本轮为单轮问答"
        if tier is not access.AccessTier.MEMBER:
            return "", "", "访客不读写对话历史"
        if not conversation_id:
            return "", "", "未提供会话标识：本轮为单轮问答"
        try:
            manager = self.context_manager
            ctx = manager.load(user_id, conversation_id)
            window = ctx.window(manager.policy)
            last_question = ""
            for turn in reversed(window):
                if turn.role == "user":
                    last_question = turn.content
                    break
            return ctx.render(manager.policy), last_question, ""
        except Exception as exc:                  # noqa: BLE001 —— 读历史失败不阻断回答
            return "", "", f"历史读取失败（{type(exc).__name__}）：本轮为单轮问答"

    def _remember(self, user_id: str, conversation_id: str | None, *, question: str,
                  answer: str, category: str) -> str:
        """把一问一答落盘。返回空串表示成功，否则返回原因。"""
        if self.context_manager is None:
            # 不能静默返回成功：调用方会据此报 `remembered=true`，
            # 那就成了「报告记住了、实际没记」—— 比不记更坏。
            return "会话上下文未装配：本轮问答未写入历史"
        if not conversation_id:
            return ""                             # 原因已由 history 环节说明过，不重复
        if not answer.strip():
            return "回答为空，未写入历史"
        try:
            self.context_manager.append_exchange(
                user_id, conversation_id, question=question, answer=answer,
                intent=category)
        except Exception as exc:                  # noqa: BLE001 —— 写失败不推翻已回答
            return f"历史写入失败（{type(exc).__name__}）：下一轮将看不到本次问答"
        return ""

    # ---------------- 意图路由（U74） ----------------
    async def _route(self, user_id: str, query: str,
                     conversation_id: str | None) -> tuple[RouteDecision | None, str]:
        """跑一次五层意图路由。返回 `(决策, 不可用原因)`。

        **三类结果必须分开**，混起来就分不清「判不出」与「没接通」：

        - 决策 `label=None`：路由跑了，五层都没给出结论 —— 这是**正常**结果，
          planner 会退回自己的确定性分类（UNKNOWN → 最稳的 hybrid_rerank）；
        - `intent_router is None`：**没装配**，原因如实说出（E-77 的形态）；
        - 抛异常：路由本身坏了，降级为确定性分类，并把异常类型带上。
        """
        if self.intent_router is None:
            return None, "意图路由未装配：本轮按确定性规则分类"
        try:
            decision = await self.intent_router.route(RouteRequest(
                user_id=user_id,
                session_id=conversation_id or "single-turn",
                utterance=query,
                labels=self.intent_labels))
        except InputGuardError:
            raise                                 # 风控拒绝要原样上抛，由调用方转成 error
        except Exception as exc:                  # noqa: BLE001 —— 路由失败不阻断回答
            return None, f"意图路由失败（{type(exc).__name__}）：本轮按确定性规则分类"
        return decision, ""

    def _intent_view(self, decision: RouteDecision | None, note: str) -> dict:
        """下发给端侧/日志的路由视图。**没有就说没有**，不做零值伪装。"""
        if decision is None:
            return {"wired": False, "note": note}
        view: dict = {
            "wired": True,
            "layer": decision.layer,
            "label": decision.label,
            "confidence": round(decision.confidence, 3),
            "degraded": decision.degraded,
            "notes": list(decision.degradation_notes),
            "untrusted": decision.input_untrusted,
            # 影子态**不等结果**，所以这里只能报「本层未参与决策」，
            # 报不出它判了什么 —— 那正是旁路的代价，写在数据里比藏着好。
            "layers": [a.layer for a in decision.attempts],
        }
        if note:
            view["note"] = note
        return view

    # ---------------- 检索 ----------------
    def _retrieve(self, query: str, user_id: str | None) -> tuple[list[dict], str | None]:
        """返回 (命中, 降级说明)。**检索失败不当成「没找到」**。"""
        if self.search is None:
            return [], "检索未接线：本次回答未引用任何资料"
        try:
            return list(self.search(query, user_id))[:self.top_k], None
        except SearchUnavailable as exc:
            return [], f"检索不可用（{exc}）：本次回答未引用任何资料"
        except Exception as exc:                      # noqa: BLE001 —— 检索失败不阻断回答
            return [], f"检索失败（{type(exc).__name__}: {exc}）：本次回答未引用任何资料"

    @staticmethod
    def _render_prompt(query: str, hits: Sequence[dict], history: str = "") -> str:
        """拼提示词。**顺序固定**：历史 → 资料 → 当前问题。

        历史放在资料之前，是因为它只负责消解指代（「第二个」指什么），
        权威性低于资料；放在后面会让模型把历史当最近证据来引用。
        历史为空时整段消失，不留「（无历史）」占位 —— 占位会被当成一条事实。
        """
        blocks: list[str] = []
        if history.strip():
            blocks.append(f"{_HISTORY_HEAD}\n{history.strip()}")
        if not hits:
            blocks.append("【资料】\n（无）")
        else:
            lines = []
            for index, hit in enumerate(hits, start=1):
                ids = "、".join(str(e) for e in (hit.get("evidence_ids") or ())) or f"H{index}"
                source = hit.get("source_id") or "未标注来源"
                lines.append(f"[{ids}]（{source}）{hit.get('text', '')}")
            blocks.append("【资料】\n" + "\n".join(lines))
        blocks.append(f"【用户问题】{query}")
        return "\n\n".join(blocks)

    @staticmethod
    def _search_text(query: str, last_question: str) -> tuple[str, str]:
        """把「那第二个呢」这类指代句补上上一轮问题再送去检索。

        返回 `(检索文本, 说明)`。**不改写 query 本身**：改写会让
        「用户问了什么」与「检索了什么」对不上，复检和评测都无从对齐。
        只扩展检索侧，提示词里仍然是用户原话。
        """
        if not last_question.strip():
            return query, ""
        if len(query) > ANAPHORA_MAX_CHARS or not _ANAPHORA.search(query):
            return query, ""
        return f"{last_question} {query}", "检索已结合上一轮问题扩展（指代消解）"

    def _can_speak(self, tier: access.AccessTier) -> bool:
        return access.authorize(tier, access.Capability.VOICE_OUTPUT).allowed

    def _model_kwargs(self, user_id: str) -> dict[str, str]:
        """按用户解析出本次调用要用的模型与凭据（U70）。

        额度在这里扣：**扣不动就不发请求**，绝不"先用后报"。
        """
        if self.model_access is None:
            return {"model": self.model} if self.model else {}
        resolved = self.model_access.resolve(user_id=user_id)
        kwargs = {"model": resolved.model, "provider": resolved.provider}
        if resolved.api_key:
            kwargs["api_key"] = resolved.api_key
        if resolved.base_url:
            kwargs["base_url"] = resolved.base_url
        return kwargs

    async def stream(self, *, user_id: str, query: str,
                     tier: access.AccessTier | None = None,
                     conversation_id: str | None = None,
                     ) -> AsyncIterator[ChatEvent]:
        """主链路。**任何异常都转成 error 事件，不抛出到 ASGI 层**。

        `conversation_id`（U73）为空或层级不符时**退化为单轮**；
        退化的原因写进 `notes` —— 静默退化等于把「没接上」伪装成「新用户」。
        """
        resolved_tier = tier or access.AccessTier.from_user_id(user_id)
        try:
            access.require(resolved_tier, access.Capability.TEXT_QUERY)
        except access.AccessDenied as exc:
            yield ChatEvent("error", text=exc.hint or str(exc), data=exc.as_dict())
            return

        history, last_question, history_problem = self._history(
            user_id, conversation_id, resolved_tier)

        # ---- 意图路由（U74）：先问「这句话想干什么」，再决定怎么查 ----
        try:
            intent_decision, intent_note = await self._route(
                user_id, query, conversation_id)
        except InputGuardError as exc:
            # 风控 BLOCK：不进入业务链路，但要**说清是被拦了**，
            # 不能让它混成一次普通的回答失败（用户会一直重试同一种说法）。
            yield ChatEvent("error", text="这条输入未通过安全检查，已停止处理。",
                            data={"code": "input_blocked", "detail": str(exc)})
            return

        intent_notes: list[str] = [intent_note] if intent_note else []
        if intent_decision is not None and intent_decision.degraded:
            intent_notes.extend(f"路由 {n}" for n in intent_decision.degradation_notes)

        # JEV-003 在链路上的落点：路由判定必须澄清 → 暂停分发，不进检索。
        # 话术由系统按枚举模板渲染（U25），不是模型自由生成的句子。
        if intent_decision is not None and intent_decision.must_pause:
            question = intent_decision.clarifying_question
            remember_note = self._remember(
                user_id, conversation_id, question=query, answer=question,
                category="multi_intent")
            notes = intent_notes + [n for n in (history_problem, remember_note) if n]
            yield ChatEvent("clarify", text=question,
                            speech=question if self._can_speak(resolved_tier) else "",
                            data={"intent": self._intent_view(intent_decision, ""),
                                  "notes": notes})
            yield ChatEvent("done", data={"clarified": True, "source": "intent_router",
                                          "intent_layer": intent_decision.layer,
                                          "notes": notes,
                                          "conversation_id": conversation_id})
            return

        try:
            retrieval_plan = plan_query(
                query, tier=resolved_tier,
                intent_layer=intent_decision.layer if intent_decision else None,
                intent_label=intent_decision.label if intent_decision else None)
        except Exception as exc:                      # noqa: BLE001
            yield ChatEvent("error", text=f"请求无法规划：{exc}",
                            data={"code": "plan_failed"})
            return

        plan_payload = retrieval_plan.as_dict()
        plan_payload["intent"] = self._intent_view(intent_decision, intent_note)
        yield ChatEvent("plan", data=plan_payload)

        # 多意图：计划书要求「不自动选择优先级，必须向用户确认」（JEV-003）
        if retrieval_plan.needs_clarification:
            # 反问是**一个完整轮次**（用户看到了问题，并会据此重述），要落盘；
            # 否则用户重述时模型不知道刚才为什么要他重述。
            remember_note = self._remember(
                user_id, conversation_id, question=query,
                answer=retrieval_plan.clarify_question,
                category=retrieval_plan.category.value)
            yield ChatEvent("clarify", text=retrieval_plan.clarify_question,
                            speech=retrieval_plan.clarify_question
                            if self._can_speak(resolved_tier) else "")
            clarify_notes = intent_notes + [n for n in (history_problem, remember_note) if n]
            yield ChatEvent("done", data={"clarified": True, "notes": clarify_notes,
                                          "conversation_id": conversation_id})
            return

        # 闲聊不检索：检索只会捞出无关条文，浪费一次检索又污染上下文
        skip_retrieval = retrieval_plan.category is QueryCategory.SMALL_TALK
        search_text, expansion = self._search_text(query, last_question)
        hits, degraded = ([], "闲聊分类，未检索") if skip_retrieval else \
            self._retrieve(search_text,
                           user_id if resolved_tier is access.AccessTier.MEMBER else None)

        notes = list(retrieval_plan.notes) + intent_notes
        if degraded:
            notes.append(degraded)
        if expansion:
            notes.append(expansion)
        if history_problem:
            notes.append(history_problem)

        # 规则类问题必须有条文支撑：拿不到证据时**弃答**，不给「大概是这样」
        if retrieval_plan.category is QueryCategory.POLICY_RULE and not hits:
            text = ("资料里没有找到与这条规定相关的条文，我不能凭印象回答。"
                    "可以补充具体条款名或换个说法再问。")
            remember_note = self._remember(
                user_id, conversation_id, question=query, answer=text,
                category=retrieval_plan.category.value)
            if remember_note:
                notes.append(remember_note)
            yield ChatEvent("chunk", text=text,
                            speech=text if self._can_speak(resolved_tier) else "")
            yield ChatEvent("done", data={"abstained": True, "notes": notes,
                                          "intent_layer": intent_decision.layer
                                          if intent_decision else None,
                                          "conversation_id": conversation_id})
            return

        if self.gateway is None:
            text = "云端回答模型未配置，暂时无法回答。"
            yield ChatEvent("error", text=text, data={"code": "llm_unavailable"})
            return

        messages = [ChatMessage("system", _SYSTEM),
                    ChatMessage("user", self._render_prompt(query, hits, history))]

        try:
            model_kwargs = self._model_kwargs(user_id)
        except ModelAccessError as exc:
            # 额度用完 / 凭据缺失 / 归属不符，都在这里变成**可读的错误事件**。
            # 不退化成"随便找个模型回答"：那等于替用户改了选择（U70）。
            yield ChatEvent("error", text=exc.message,
                            data={"code": exc.code})
            return

        collected: list[str] = []
        try:
            async for piece in self.gateway.stream(user_id=user_id, messages=messages,
                                                   max_tokens=512, **model_kwargs):
                if not piece:
                    continue
                collected.append(piece)
                yield ChatEvent("chunk", text=piece,
                                speech=piece if self._can_speak(resolved_tier) else "")
        except GatewayError as exc:
            # 已经吐过一部分就保留它，并明确标注「回答被截断」——不假装完整
            yield ChatEvent("error",
                            text=("回答中断：" + str(exc)),
                            data={"code": "llm_failed",
                                  "partial": "".join(collected)})
            return

        answer = "".join(collected)
        verification: VerificationResult | None = None
        if self.verify_answer and answer.strip() and self.search is not None:
            try:
                verification = verify(answer, _SearchFinder(self.search,
                                                            user_id if resolved_tier
                                                            is access.AccessTier.MEMBER else None),
                                      top_k=3)
            except Exception as exc:                  # noqa: BLE001 —— 复检失败不推翻已回答
                notes.append(f"回答复检失败（{type(exc).__name__}: {exc}）")

        final_answer = answer
        if verification is not None and verification.abstained:
            replacement = render_abstention(verification)
            # 落盘的必须是**用户真正看到的那一版**：把被推翻的初稿写进历史，
            # 下一轮模型会把幻觉当成「自己上次说过的话」继续引用。
            final_answer = replacement
            yield ChatEvent("revise", text=replacement,
                            speech=replacement if self._can_speak(resolved_tier) else "",
                            data={"code": "abstained",
                                  "verification": verification.as_dict()})
        elif verification is not None and not verification.grounded:
            notes.append(
                f"部分声明缺少证据支撑（attribution_rate={verification.attribution_rate:.2f}），"
                "答案仅供参考")

        remember_note = self._remember(user_id, conversation_id, question=query,
                                       answer=final_answer,
                                       category=retrieval_plan.category.value)
        if remember_note:
            notes.append(remember_note)

        yield ChatEvent("done", data={
            "category": retrieval_plan.category.value,
            "decided_by": retrieval_plan.decided_by,
            "tools": list(retrieval_plan.tools),
            "hits": len(hits),
            # 素材 id 必须吐出来：评测的召回率维度要的是「召回了哪些」，
            # 不是「召回了几个」。只给数量时，复评无法判断漏掉的是哪条，
            # 缺陷只能写成「召回不足」而说不出该补什么。
            "retrieved_ids": [str(eid) for hit in hits
                              for eid in (hit.get("evidence_ids") or ())],
            "verified": verification.grounded if verification else None,
            "notes": notes,
            "history": bool(history.strip()),
            "conversation_id": conversation_id,
            # 路由的结论要**能被下游核对**：`intent_layer` 是「这句话走完了哪一层」，
            # 没有它，评测只能看到分类结果，看不出分类是规则给的还是模型给的。
            "intent_layer": intent_decision.layer if intent_decision else None,
            "intent_label": intent_decision.label if intent_decision else None,
            "intent_wired": self.intent_router is not None,
            "remembered": self.context_manager is not None and bool(conversation_id)
                          and not remember_note and bool(final_answer.strip()),
        })
