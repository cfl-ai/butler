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

from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Protocol, Sequence

from ai_butler.core import access
from ai_butler.core.model_access import ModelAccess, ModelAccessError
from ai_butler.infra.llm_gateway import ChatMessage, GatewayError, LLMGateway
from ai_butler.material_center.verification import (
    EvidenceHit,
    VerificationResult,
    render_abstention,
    verify,
)
from ai_butler.retrieval.planner import QueryCategory, RetrievalPlan, plan as plan_query

#: 检索函数签名：query + 用户 → 命中列表。user_id 为空表示只用公共库。
SearchFn = Callable[[str, str | None], Sequence[dict]]

_SYSTEM = (
    "你是「AI 管家」的文字助手。规则：\n"
    "1. 只依据【资料】回答；资料里没有的，明确说「资料里没有找到」，不要凭印象编造。\n"
    "2. 引用资料时在句末用 [证据ID] 标注来源。\n"
    "3. 回答用中文，简洁直接，不超过 200 字。\n"
    "4. 【资料】为空时，仅可作通用常识性说明，并说明「未引用资料」。"
)


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
                 verify_answer: bool = True) -> None:
        self.gateway = gateway
        self.search = search
        self.top_k = top_k
        self.model = model
        #: U70：有它时**按用户**解析模型与凭据；没有时才退回静态 `model`。
        self.model_access = model_access
        self.verify_answer = verify_answer

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
    def _render_prompt(query: str, hits: Sequence[dict]) -> str:
        if not hits:
            return f"【资料】\n（无）\n\n【用户问题】{query}"
        lines = []
        for index, hit in enumerate(hits, start=1):
            ids = "、".join(str(e) for e in (hit.get("evidence_ids") or ())) or f"H{index}"
            source = hit.get("source_id") or "未标注来源"
            lines.append(f"[{ids}]（{source}）{hit.get('text', '')}")
        return "【资料】\n" + "\n".join(lines) + f"\n\n【用户问题】{query}"

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
                     ) -> AsyncIterator[ChatEvent]:
        """主链路。**任何异常都转成 error 事件，不抛出到 ASGI 层**。"""
        resolved_tier = tier or access.AccessTier.from_user_id(user_id)
        try:
            access.require(resolved_tier, access.Capability.TEXT_QUERY)
        except access.AccessDenied as exc:
            yield ChatEvent("error", text=exc.hint or str(exc), data=exc.as_dict())
            return

        try:
            retrieval_plan = plan_query(query, tier=resolved_tier)
        except Exception as exc:                      # noqa: BLE001
            yield ChatEvent("error", text=f"请求无法规划：{exc}",
                            data={"code": "plan_failed"})
            return

        yield ChatEvent("plan", data=retrieval_plan.as_dict())

        # 多意图：计划书要求「不自动选择优先级，必须向用户确认」（JEV-003）
        if retrieval_plan.needs_clarification:
            yield ChatEvent("clarify", text=retrieval_plan.clarify_question,
                            speech=retrieval_plan.clarify_question
                            if self._can_speak(resolved_tier) else "")
            yield ChatEvent("done", data={"clarified": True})
            return

        # 闲聊不检索：检索只会捞出无关条文，浪费一次检索又污染上下文
        skip_retrieval = retrieval_plan.category is QueryCategory.SMALL_TALK
        hits, degraded = ([], "闲聊分类，未检索") if skip_retrieval else \
            self._retrieve(query, user_id if resolved_tier is access.AccessTier.MEMBER else None)

        notes = list(retrieval_plan.notes)
        if degraded:
            notes.append(degraded)

        # 规则类问题必须有条文支撑：拿不到证据时**弃答**，不给「大概是这样」
        if retrieval_plan.category is QueryCategory.POLICY_RULE and not hits:
            text = ("资料里没有找到与这条规定相关的条文，我不能凭印象回答。"
                    "可以补充具体条款名或换个说法再问。")
            yield ChatEvent("chunk", text=text,
                            speech=text if self._can_speak(resolved_tier) else "")
            yield ChatEvent("done", data={"abstained": True, "notes": notes})
            return

        if self.gateway is None:
            text = "云端回答模型未配置，暂时无法回答。"
            yield ChatEvent("error", text=text, data={"code": "llm_unavailable"})
            return

        messages = [ChatMessage("system", _SYSTEM),
                    ChatMessage("user", self._render_prompt(query, hits))]

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

        if verification is not None and verification.abstained:
            replacement = render_abstention(verification)
            yield ChatEvent("revise", text=replacement,
                            speech=replacement if self._can_speak(resolved_tier) else "",
                            data={"code": "abstained",
                                  "verification": verification.as_dict()})
        elif verification is not None and not verification.grounded:
            notes.append(
                f"部分声明缺少证据支撑（attribution_rate={verification.attribution_rate:.2f}），"
                "答案仅供参考")

        yield ChatEvent("done", data={
            "category": retrieval_plan.category.value,
            "tools": list(retrieval_plan.tools),
            "hits": len(hits),
            "verified": verification.grounded if verification else None,
            "notes": notes,
        })
