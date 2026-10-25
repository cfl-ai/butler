"""P3 五层意图路由（冻结，不得变更顺序与层数）。

计划书依据
----------
- 三、P3「落地五层意图路由：硬规则 → Jev 决策 → 向量相似度 → 垂类专用决策模型 → 大模型兜底」
- 三、P3「实现多意图冲突处理：**不自动选择优先级**，必须向用户确认」
- 一、项目概述「Jev 仅输出结构化枚举标签 + 置信概率，不输出自由文本」
- 八、五层路由冻结：jev_confidence_threshold=0.82 / multi_intent_auto_resolve=False
- 三、P3「全链路降级与回滚机制：任一环节失败自动降级，不阻断主流程」

相关裁定
--------
U01 意图标签分层（home-assistant/intents）
U02 Jev 走受限小模型
U03 第四层照 RouteLLM 的 bert router 形态，阈值须在中文验证集上重新标定
U14 把「禁止自动选优先级」编码成模型必出字段 require_clarification
U15 置信概率不得取自约束解码的 token 概率
U25 澄清话术由系统渲染 —— 计划书三处禁止 Jev 输出自由文本，效力高于 U14 的借鉴项

JEV-003（本模块的核心不变量）
---------------------------
Jev 返回 require_clarification 后，路由层**必须暂停分发**，
且**任何更低层都不得覆盖该结论**——这是 multi_intent_auto_resolve=False 的落地。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ai_butler.core.input_guard import InputGuard, InputGuardError
from ai_butler.infra.llm_gateway import (
    CLARIFICATION_CATEGORIES,
    CLARIFY_MULTI_INTENT,
    CLARIFY_OUT_OF_SCOPE,
    ChatMessage,
    JevClient,
    JevDecision,
    LLMGateway,
)

#: 五层顺序（计划书三、P3 逐字顺序，冻结）
LAYER_HARD_RULE = "hard_rule"
LAYER_JEV = "jev"
LAYER_VECTOR = "vector"
LAYER_VERTICAL = "vertical_model"
LAYER_LLM = "llm_fallback"
LAYERS: tuple[str, ...] = (LAYER_HARD_RULE, LAYER_JEV, LAYER_VECTOR, LAYER_VERTICAL,
                           LAYER_LLM)

#: 澄清话术模板（计划书三、P3「必须向用户确认」）
#: 键为**枚举**，话术由系统渲染 —— 见 decisions.U25
CLARIFICATION_TEMPLATES: dict[str, str] = {
    CLARIFY_MULTI_INTENT:
        "您这次的请求里包含多个意图（{candidates}），请问要先处理哪一个？",
    CLARIFY_OUT_OF_SCOPE:
        "抱歉，这个需求暂时不在我能处理的范围内（{candidates}），"
        "可以换一种说法，或补充更多信息吗？",
}

#: 每层的默认超时预算（实现参数）；超时即降级到下一层
DEFAULT_LAYER_TIMEOUT_SECONDS = 5.0
#: 向量层接受阈值（实现参数；U03 要求第四层阈值须在中文验证集标定，同理适用于本层）
DEFAULT_VECTOR_THRESHOLD = 0.62
#: 垂类模型层默认阈值（实现参数，**占位**：U03 要求在自建中文验证集上重新标定后才可上线）
DEFAULT_VERTICAL_THRESHOLD = 0.55


class RouteError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteRequest:
    user_id: str
    session_id: str
    utterance: str
    labels: tuple[str, ...]
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.user_id or not str(self.user_id).strip():
            raise RouteError("路由必须携带 user_id（计划书三、P4 禁止裸数据访问）")
        if not self.utterance or not str(self.utterance).strip():
            raise RouteError("路由请求的用户话语不得为空")
        if not self.labels:
            raise RouteError(
                "候选意图标签集为空。【文档未定义项 #1】计划书未给出标签清单，"
                "禁止自行编造；须由人工补充定义后传入。"
            )


@dataclass(frozen=True)
class LayerVerdict:
    """单层的判定结果。accepted=False 表示本层放弃，交由下一层。"""

    accepted: bool = False
    label: str | None = None
    confidence: float = 0.0
    require_clarification: bool = False
    clarification_category: str = ""
    candidate_labels: tuple[str, ...] = ()
    note: str = ""
    shadow: bool = False


@dataclass(frozen=True)
class LayerAttempt:
    """一层一次的尝试痕迹，供可观测与排障。"""

    layer: str
    accepted: bool
    label: str | None
    confidence: float
    elapsed_ms: int
    note: str = ""


@dataclass(frozen=True)
class RouteDecision:
    user_id: str
    session_id: str
    trace_id: str
    layer: str
    label: str | None
    confidence: float
    require_clarification: bool = False
    clarifying_question: str = ""
    clarification_category: str = ""
    candidate_labels: tuple[str, ...] = ()
    degraded: bool = False
    degradation_notes: tuple[str, ...] = ()
    #: 风控结论（台账 U41）：输入是否被标为不可信。
    #: TAINT 时下游**不得**把它沉淀为事实。
    input_untrusted: bool = False
    attempts: tuple[LayerAttempt, ...] = ()

    @property
    def must_pause(self) -> bool:
        """是否必须暂停分发、等待用户确认（JEV-003）。"""
        return self.require_clarification

    @property
    def resolved(self) -> bool:
        """是否得到了可执行的意图。"""
        return bool(self.label) and not self.require_clarification

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id, "session_id": self.session_id,
            "trace_id": self.trace_id, "layer": self.layer, "label": self.label,
            "confidence": self.confidence,
            "require_clarification": self.require_clarification,
            "clarifying_question": self.clarifying_question,
            "clarification_category": self.clarification_category,
            "candidate_labels": list(self.candidate_labels),
            "degraded": self.degraded, "degradation_notes": list(self.degradation_notes),
            "attempts": [{"layer": a.layer, "accepted": a.accepted, "label": a.label,
                          "confidence": a.confidence, "elapsed_ms": a.elapsed_ms,
                          "note": a.note} for a in self.attempts],
        }


def render_clarification(category: str, candidates: Sequence[str]) -> str:
    """用枚举键模板渲染澄清话术。**不由模型生成**（计划书禁止 Jev 输出自由文本）。"""
    template = CLARIFICATION_TEMPLATES.get(category, CLARIFICATION_TEMPLATES[CLARIFY_MULTI_INTENT])
    shown = "、".join(candidates) if candidates else "多个"
    return template.format(candidates=shown)


# --------------------------------------------------------------------------- #
# 层协议
# --------------------------------------------------------------------------- #
class IntentLayer(Protocol):
    name: str

    async def evaluate(self, request: RouteRequest) -> LayerVerdict | None:
        ...


# --------------------------------------------------------------------------- #
# 第 1 层：硬规则
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HardRule:
    pattern: str
    label: str
    priority: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        if not self.pattern or not self.label:
            raise RouteError("硬规则必须同时给出 pattern 与 label")
        try:
            re.compile(self.pattern)      # 提前暴露非法正则，避免运行期才炸
        except re.error as exc:
            raise RouteError(f"硬规则正则非法：{self.pattern!r} —— {exc}") from exc


class HardRuleLayer:
    """确定性规则层：命中即定，不做概率判断。"""

    name = LAYER_HARD_RULE

    def __init__(self, rules: Sequence[HardRule]) -> None:
        self.rules = sorted(rules, key=lambda r: -r.priority)

    async def evaluate(self, request: RouteRequest) -> LayerVerdict | None:
        for rule in self.rules:
            if rule.label not in request.labels:
                continue
            if re.search(rule.pattern, request.utterance):
                return LayerVerdict(accepted=True, label=rule.label, confidence=1.0,
                                    note=f"硬规则命中 {rule.pattern!r}"
                                         + (f"：{rule.note}" if rule.note else ""))
        return None


# --------------------------------------------------------------------------- #
# 第 2 层：Jev 决策
# --------------------------------------------------------------------------- #
class ShadowTasks:
    """影子旁路的任务登记簿。

    **为什么影子必须旁路**：原实现把 `shadow_mode` 判在「决策拿到之后」，
    也就是模型仍被完整调用一次，而 `route()` 是 `await`、单层超时 5 秒。
    于是「只记录不决策」在成本上等于「照常调用一次模型」—— 影子期不是
    无感观测，是给每个请求白加一次模型延迟（最坏 5 秒）。
    影子要能看到真实分布，就不能改变被观测系统的行为，延迟也算行为。

    `drain()` 给测试与优雅关闭用：生产路径**不等**它，这正是旁路的含义。
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def submit(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        # 取出结果，避免 "Task exception was never retrieved" 噪音；
        # 影子任务内部已自行兜异常，这里的 exc 只是双保险。
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def drain(self, timeout: float = 30.0) -> int:
        """等待所有影子任务结束，返回处理条数。超时不清空，避免假装跑完。"""
        tasks = list(self._tasks)
        if not tasks:
            return 0
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        return len(done) if not pending else len(done)


class JevLayer:
    name = LAYER_JEV

    def __init__(self, client: JevClient, *, on_shadow=None,
                 shadow: bool | None = None) -> None:
        """`shadow` 三态，含义不能混：

        - `None`：沿用旧行为 —— 完整调用一次，由决策自带的 `shadow` 标记
          决定是否采信。**这条路径会阻塞**，只为兼容既有调用方保留。
        - `True`：**旁路影子** —— 立即返回「不采信」，模型调用丢后台任务。
          生产装配走这条（`intent_wiring.build_intent_router`）。
        - `False`：正常决策。
        """
        self.client = client
        self.on_shadow = on_shadow
        self.shadow = shadow
        self.tasks = ShadowTasks()

    async def evaluate(self, request: RouteRequest) -> LayerVerdict | None:
        if self.shadow is True:
            self.tasks.submit(self._probe(request))
            return LayerVerdict(accepted=False, shadow=True,
                                note="影子模式（旁路执行）：后台记录，不参与决策")

        decision: JevDecision = await self.client.decide(
            user_id=request.user_id, question=request.utterance,
            allowed_labels=request.labels)

        if decision.shadow:
            # 影子模式：只记录不决策（计划书三、P3）
            if self.on_shadow is not None:
                self.on_shadow(decision)
            return LayerVerdict(accepted=False, label=decision.label,
                                confidence=decision.confidence, shadow=True,
                                note=decision.reason or "影子模式：仅记录")

        if decision.require_clarification:
            # JEV-003：必须暂停分发，交由用户确认
            return LayerVerdict(accepted=True, label=decision.label,
                                confidence=decision.confidence,
                                require_clarification=True,
                                clarification_category=decision.clarification_category,
                                candidate_labels=decision.candidate_labels,
                                note="Jev 判定必须向用户确认，暂停分发")

        if decision.accepted and decision.label:
            return LayerVerdict(accepted=True, label=decision.label,
                                confidence=decision.confidence,
                                candidate_labels=decision.candidate_labels,
                                note=decision.reason or "Jev 高置信决策")
        return LayerVerdict(accepted=False, label=decision.label,
                            confidence=decision.confidence,
                            note=decision.reason or "Jev 未给出可用结论，向下流转")

    async def _probe(self, request: RouteRequest) -> None:
        """影子探测。**任何异常都吞掉** —— 影子故障不得影响主链路。"""
        try:
            decision = await self.client.decide(
                user_id=request.user_id, question=request.utterance,
                allowed_labels=request.labels)
        except Exception:                             # noqa: BLE001
            return
        if self.on_shadow is not None:
            try:
                self.on_shadow(decision)
            except Exception:                         # noqa: BLE001
                return


# --------------------------------------------------------------------------- #
# 第 3 层：向量相似度
# --------------------------------------------------------------------------- #
class VectorLayer:
    name = LAYER_VECTOR

    def __init__(self, embedder, exemplars: dict[str, Sequence[str]], *,
                 threshold: float = DEFAULT_VECTOR_THRESHOLD) -> None:
        if not exemplars:
            raise RouteError("向量层需要示例语料，否则无法计算相似度")
        self.embedder = embedder
        self.threshold = threshold
        self.labels: list[str] = []
        self._vectors: list[list[float]] = []
        for label, samples in exemplars.items():
            for text in samples:
                self.labels.append(label)
                self._vectors.append(embedder.encode([text])[0])

    @staticmethod
    def _dot(a: Sequence[float], b: Sequence[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    async def evaluate(self, request: RouteRequest) -> LayerVerdict | None:
        query = self.embedder.encode([request.utterance])[0]
        best_label, best_score = None, -1.0
        for label, vec in zip(self.labels, self._vectors):
            if label not in request.labels:
                continue
            score = self._dot(query, vec)
            if score > best_score:
                best_label, best_score = label, score
        if best_label is None:
            return None
        if best_score < self.threshold:
            return LayerVerdict(accepted=False, label=best_label, confidence=max(0.0, best_score),
                                note=f"向量相似度 {best_score:.3f} < 阈值 {self.threshold}")
        return LayerVerdict(accepted=True, label=best_label,
                            confidence=max(0.0, min(1.0, best_score)),
                            note=f"向量相似度命中 {best_score:.3f}")


# --------------------------------------------------------------------------- #
# 第 4 层：垂类专用决策模型
# --------------------------------------------------------------------------- #
class VerticalDecisionModel(Protocol):
    """U03：照 RouteLLM 的 bert router 形态 —— 中文基座小分类器 + 阈值路由。"""

    name: str

    def predict(self, utterance: str, labels: Sequence[str]) -> tuple[str, float] | None:
        ...


class VerticalModelLayer:
    name = LAYER_VERTICAL

    def __init__(self, model: VerticalDecisionModel, *,
                 threshold: float = DEFAULT_VERTICAL_THRESHOLD) -> None:
        self.model = model
        self.threshold = threshold

    async def evaluate(self, request: RouteRequest) -> LayerVerdict | None:
        outcome = self.model.predict(request.utterance, request.labels)
        if not outcome:
            return None
        label, confidence = outcome
        if label not in request.labels:
            return LayerVerdict(accepted=False, label=label, confidence=confidence,
                                note=f"垂类模型给出候选集外的标签 {label!r}，丢弃")
        if confidence < self.threshold:
            return LayerVerdict(accepted=False, label=label, confidence=confidence,
                                note=f"垂类模型置信 {confidence:.3f} < 阈值 {self.threshold}")
        return LayerVerdict(accepted=True, label=label, confidence=confidence,
                            note=f"垂类模型命中 {confidence:.3f}")


# --------------------------------------------------------------------------- #
# 第 5 层：大模型兜底
# --------------------------------------------------------------------------- #
_FALLBACK_SYSTEM = (
    "你是意图分类器。只允许输出一个 JSON 对象，形如 "
    '{"label": "<枚举标签>", "confidence": <0~1 的小数>}。'
    "禁止输出解释、句子或额外字段；label 必须从候选标签中原样选取；"
    "若无法判断，confidence 填 0。"
)


class LLMFallbackLayer:
    name = LAYER_LLM

    def __init__(self, gateway: LLMGateway, *, model: str | None = None,
                 threshold: float = 0.0) -> None:
        self.gateway = gateway
        self.model = model
        self.threshold = threshold

    async def evaluate(self, request: RouteRequest) -> LayerVerdict | None:
        result = await self.gateway.chat(
            user_id=request.user_id,
            messages=[ChatMessage("system", _FALLBACK_SYSTEM),
                      ChatMessage("user", f"候选标签：{list(request.labels)}\n"
                                          f"用户请求：{request.utterance}")],
            model=self.model, max_tokens=48)
        label, confidence = self._parse(result.text, request.labels)
        if confidence < self.threshold:
            return LayerVerdict(accepted=False, label=label, confidence=confidence,
                                note=f"大模型兜底置信 {confidence:.3f} 过低")
        return LayerVerdict(accepted=True, label=label, confidence=confidence,
                            note="大模型兜底给出结论")

    @staticmethod
    def _parse(text: str, allowed: Sequence[str]) -> tuple[str, float]:
        raw = text.strip()
        if raw.startswith("" + chr(96) * 3):
            raw = raw.strip(chr(96)).removeprefix("json").strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RouteError("大模型兜底输出不是合法 JSON，禁止采用") from exc
        label = obj.get("label")
        if label not in allowed:
            raise RouteError(f"大模型兜底标签 {label!r} 不在候选集内，禁止采用")
        try:
            confidence = float(obj.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise RouteError("大模型兜底未给出可解析的置信度") from exc
        if not 0.0 <= confidence <= 1.0:
            raise RouteError("置信度必须落在 0~1")
        return label, confidence


# --------------------------------------------------------------------------- #
# 路由编排
# --------------------------------------------------------------------------- #
class IntentRouter:
    """五层顺序执行；层内异常/超时一律降级到下一层，不阻断主流程。"""

    def __init__(self, layers: Sequence[IntentLayer], *,
                 layer_timeout_seconds: float = DEFAULT_LAYER_TIMEOUT_SECONDS,
                 guard: InputGuard | None = None) -> None:
        names = [layer.name for layer in layers]
        unknown = [n for n in names if n not in LAYERS]
        if unknown:
            raise RouteError(f"未知的路由层 {unknown}；五层路由已冻结，不得增删")
        if len(set(names)) != len(names):
            raise RouteError(f"路由层重复注册：{names}")
        self.layers = list(layers)
        self.layer_timeout_seconds = layer_timeout_seconds
        #: 前置风控闸门。**不是第六层路由** —— 五层已冻结；
        #: 它在进入五层之前判定输入风险，见 core/input_guard.py。
        self.guard = guard if guard is not None else InputGuard()

    @property
    def layer_names(self) -> tuple[str, ...]:
        return tuple(layer.name for layer in self.layers)

    async def drain_shadow(self, timeout: float = 30.0) -> int:
        """等影子任务跑完（测试与优雅关闭用）。生产主链路**不调它**。"""
        total = 0
        for layer in self.layers:
            tasks = getattr(layer, "tasks", None)
            if isinstance(tasks, ShadowTasks):
                total += await tasks.drain(timeout=timeout)
        return total

    async def route(self, request: RouteRequest) -> RouteDecision:
        trace_id = request.trace_id or uuid.uuid4().hex[:16]
        attempts: list[LayerAttempt] = []
        notes: list[str] = []

        # ---- 前置风控闸门（用户指令：防虚假信息与编码混淆）----
        # 放在五层之前、但不属于五层：BLOCK 直接拒绝，TAINT 只降可信度。
        guard_verdict = self.guard.guard(request.utterance)
        if guard_verdict.action == "block":
            raise InputGuardError(
                "输入未通过风控：" + "；".join(guard_verdict.reasons or ("无具体原因",)))
        if guard_verdict.untrusted:
            notes.append("风控: 输入标记为不可信（" + "；".join(guard_verdict.reasons) + "）")

        for layer in self.layers:
            started = time.perf_counter()
            try:
                verdict = await asyncio.wait_for(layer.evaluate(request),
                                                 timeout=self.layer_timeout_seconds)
            except asyncio.TimeoutError:
                note = f"超时（>{self.layer_timeout_seconds}s）"
                attempts.append(LayerAttempt(layer.name, False, None, 0.0, _ms(started), note))
                notes.append(f"{layer.name}: {note}")
                continue
            except Exception as exc:                    # noqa: BLE001 — 任一环节失败自动降级
                note = f"{type(exc).__name__}: {exc}"
                attempts.append(LayerAttempt(layer.name, False, None, 0.0, _ms(started), note))
                notes.append(f"{layer.name}: {note}")
                continue

            if verdict is None:
                attempts.append(LayerAttempt(layer.name, False, None, 0.0, _ms(started), "未命中"))
                continue

            # 命中说明只进 attempts；degraded 只记**异常/超时**这类真正的降级
            attempts.append(LayerAttempt(layer.name, verdict.accepted, verdict.label,
                                         verdict.confidence, _ms(started), verdict.note))

            if verdict.require_clarification:
                # JEV-003 —— 立即返回，**不继续向下层**：任何更低层都不得覆盖「必须澄清」
                return self._clarify(request, trace_id, layer.name, verdict, attempts, notes,
                                  input_untrusted=guard_verdict.untrusted)

            if verdict.accepted and verdict.label:
                return RouteDecision(
                    user_id=request.user_id, session_id=request.session_id, trace_id=trace_id,
                    layer=layer.name, label=verdict.label, confidence=verdict.confidence,
                    candidate_labels=verdict.candidate_labels,
                    degraded=bool(notes), degradation_notes=tuple(notes),
                input_untrusted=guard_verdict.untrusted,
                    attempts=tuple(attempts))

        # 五层全部未能给出结论：不阻断主流程，交由 Graph 走「无法识别」分支
        notes.append("五层路由均未给出可用意图")
        return RouteDecision(user_id=request.user_id, session_id=request.session_id,
                             trace_id=trace_id, layer=LAYER_LLM, label=None, confidence=0.0,
                             degraded=True, degradation_notes=tuple(notes),
                input_untrusted=guard_verdict.untrusted,
                             attempts=tuple(attempts))

    @staticmethod
    def _clarify(request: RouteRequest, trace_id: str, layer: str, verdict: LayerVerdict,
                 attempts: list[LayerAttempt], notes: list[str],
               *, input_untrusted: bool = False) -> RouteDecision:
        candidates = verdict.candidate_labels or ((verdict.label,) if verdict.label else ())
        return RouteDecision(
            user_id=request.user_id, session_id=request.session_id, trace_id=trace_id,
            layer=layer, label=verdict.label, confidence=verdict.confidence,
            require_clarification=True,
            # 话术由系统按枚举模板渲染，模型不产出询问语句（decisions.U25）
            clarifying_question=render_clarification(verdict.clarification_category, candidates),
            clarification_category=verdict.clarification_category or CLARIFY_MULTI_INTENT,
            candidate_labels=tuple(candidates),
            degraded=bool(notes), degradation_notes=tuple(notes),
            input_untrusted=input_untrusted,
            attempts=tuple(attempts))


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def default_layers(*, hard_rules: Sequence[HardRule] = (), jev_client: JevClient | None = None,
                   embedder=None, exemplars: dict[str, Sequence[str]] | None = None,
                   vertical_model: VerticalDecisionModel | None = None,
                   gateway: LLMGateway | None = None,
                   vector_threshold: float = DEFAULT_VECTOR_THRESHOLD,
                   vertical_threshold: float = DEFAULT_VERTICAL_THRESHOLD,
                   on_shadow=None, jev_shadow: bool | None = None) -> list[IntentLayer]:
    """按冻结顺序组装实际可用的层；缺席的层直接跳过（仍保持相对顺序）。"""
    layers: list[IntentLayer] = []
    if hard_rules:
        layers.append(HardRuleLayer(hard_rules))
    if jev_client is not None:
        layers.append(JevLayer(jev_client, on_shadow=on_shadow, shadow=jev_shadow))
    if embedder is not None and exemplars:
        layers.append(VectorLayer(embedder, exemplars, threshold=vector_threshold))
    if vertical_model is not None:
        layers.append(VerticalModelLayer(vertical_model, threshold=vertical_threshold))
    if gateway is not None:
        layers.append(LLMFallbackLayer(gateway))
    if not layers:
        raise RouteError("至少要装配一层，否则路由无法工作")
    return layers
