"""P0-1 统一 LLM 网关 + Jev 决策模型标准化调用接口。

计划书依据
----------
- 三、P0-1「建设统一 LLM 网关：多模型兼容、鉴权、限流、重试、负载均衡、流式转发」
- 三、P0-1「网关中间件：Jev 决策模型标准化调用接口，支持影子灰度模式，
  返回决策标签 + 置信概率，故障可降级」
- 一、项目概述「Jev 仅输出结构化枚举标签 + 置信概率，不输出自由文本」
- 三、P3「Jev 仅输出结构化枚举，不输出自由文本；支持影子灰度模式；Jev 故障可平滑降级」
- 八、jev_enable / jev_confidence_threshold=0.82 / jev_fallback_enable / jev_shadow_mode

文档未定义项（禁止 AI 自行实现）
------------------------------
【文档未定义项 #1】Jev 输出的「结构化枚举标签」清单，计划书未给出。
    → 本模块**不内置任何标签集**，允许标签一律由调用方传入 allowed_labels。
【文档未定义项 #2】Jev 决策模型本体（微调小模型 / 受限大模型）未定义。
    → 本模块只约定调用契约，模型由 model 参数或网关默认模型决定。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Sequence

import httpx

from ai_butler.core.config import Settings
from ai_butler.core.naming import redis_key
from ai_butler.core.resilience import CircuitOpenError, Resilience


class GatewayError(RuntimeError):
    pass


class GatewayUnavailableError(GatewayError):
    """全部供应商均不可用。按计划书要求应可平滑降级。"""


class GatewayRateLimited(GatewayError):
    pass


class ToolUnsupportedError(GatewayError):
    """该模型/供应商**不支持 function calling**（U112）。

    为什么要单独一个错误类：过去本机 `qwen2.5vl:3b` 无 tools 能力时，
    表现是「供应商都不可用」——排查方向被指到完全相反的地方。
    现在必须让「不支持工具」与「服务不可用」是两件事。
    """


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelToolCall:
    """模型表达的「我要调用某个工具」（function calling 的信封，U112）。

    `arguments` 保持**原始 dict**：由调度层负责校验并交给 `ToolDispatcher`。
    网关不做业务解释 —— 它只把模型的话原样带回来，漏字改字都由它负责。
    """

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    call_id: str = ""

    def arguments_json(self) -> str:
        return json.dumps(dict(self.arguments), ensure_ascii=False)


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    latency_ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    #: 模型本次要求调用的工具（U112）。**空元组 = 模型没有要求调用**，
    #: 与「不支持工具」是两回事（后者会在下发前抛 `ToolUnsupportedError`）。
    tool_calls: tuple[ModelToolCall, ...] = ()


def parse_openai_tool_calls(message: Mapping[str, Any]) -> tuple[ModelToolCall, ...]:
    """解析 OpenAI 兼容协议的 `message.tool_calls`。

    `arguments` 在该协议里是 **JSON 字符串**，解析失败就丢弃这一条 ——
    宁可少一次调用，也不让半截字符串进执行层。同一函数供 deepseek 与
    openai-compatible 复用：两个就不必各写一套，改一处即两处一致。
    """
    out: list[ModelToolCall] = []
    for raw in (message.get("tool_calls") or ()):
        if not isinstance(raw, Mapping):
            continue
        fn = raw.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        raw_args = fn.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
        except json.JSONDecodeError:
            continue
        if not isinstance(args, dict):
            continue
        out.append(ModelToolCall(name=name, arguments=args, call_id=str(raw.get("id") or "")))
    return tuple(out)


def _parse_ollama_tool_calls(message: Mapping[str, Any]) -> tuple[ModelToolCall, ...]:
    """解析 Ollama `/api/chat` 的 `message.tool_calls`。

    与 OpenAI 兼容协议唯一的差别：`arguments` 这里是**已经是对象**，不是 JSON 字符串。
    写成同一个函数靠 `isinstance` 分支也能收场，但那会让两个协议的差异藏在一句话里 ——
    宁可两个短函数各自说清楚（这也是 OpenAI 兼容两处**共用同一个** parse 的原因）。
    """
    out: list[ModelToolCall] = []
    for raw in (message.get("tool_calls") or ()):
        if not isinstance(raw, Mapping):
            continue
        fn = raw.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):                  # 个别版本仍给字符串
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        out.append(ModelToolCall(name=name, arguments=dict(args or {})))
    return tuple(out)


#: 澄清类别（枚举）。计划书三、P3「是否多意图冲突」+ U15 借 CLINC150 的 out-of-scope 类。
CLARIFY_MULTI_INTENT = "multi_intent"
CLARIFY_OUT_OF_SCOPE = "out_of_scope"
CLARIFICATION_CATEGORIES: tuple[str, ...] = (CLARIFY_MULTI_INTENT, CLARIFY_OUT_OF_SCOPE)


@dataclass(frozen=True)
class JevStructured:
    """Jev 输出的结构化内容。

    U01：主意图 + 候选意图列表；U14：require_clarification + category；
    计划书：**没有任何自由文本字段**（见 decisions.U25）。
    """

    label: str
    confidence: float
    candidate_labels: tuple[str, ...] = ()
    require_clarification: bool = False
    clarification_category: str = ""


@dataclass
class JevDecision:
    """Jev 结构化决策结果：只有枚举标签 + 置信概率，绝无自由文本。"""

    label: str | None
    confidence: float
    source: Literal["jev", "fallback"]
    shadow: bool = False
    reason: str = ""
    # --- U01 / U14 追加的结构化字段（仍全为枚举与数值）---
    candidate_labels: tuple[str, ...] = ()
    require_clarification: bool = False
    clarification_category: str = ""

    @property
    def accepted(self) -> bool:
        return self.source == "jev"


# --------------------------------------------------------------------------- #
# 供应商适配
# --------------------------------------------------------------------------- #
class Provider:
    name: str = ""
    #: **协议层**是否支持 function calling（U112）。模型层的收窄见 `_model_supports_tools`。
    supports_tools: bool = False

    def _model_supports_tools(self, model: str | None) -> bool:
        """模型层是否具备工具能力。默认「协议支持即支持」。"""
        return True

    def reject_unsupported_tools(self, tools: Sequence[Mapping[str, Any]] | None,
                                 model: str | None) -> None:
        """下发工具清单前的一道闸：不支持就说清楚**为什么不支持**。

        老教训：本机 qwen2.5vl:3b 没有 function calling 能力时，故障只在最后
        表现为「供应商都不可用」，看不出真因。这里两级判定必须各自给出可诊断的措辞。
        """
        if not tools:
            return
        if not self.supports_tools:
            raise ToolUnsupportedError(
                f"供应商 {self.name!r} 未在协议层支持 function calling（U112）："
                f"请把带工具的请求路由到支持 tools 的供应商，该 vendor 不支持工具")
        if not self._model_supports_tools(model):
            raise ToolUnsupportedError(
                f"模型 {model!r}（供应商 {self.name}）不具备 function calling 能力："
                f"请在 function_calling_models 白名单中登记具工具能力的模型后再下发工具")

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def chat(self, messages: Sequence[ChatMessage], *, model: str | None,
                   timeout: float, max_tokens: int | None,
                   api_key: str | None = None,
                   base_url: str | None = None,
                   temperature: float | None = None) -> ChatResult:
        """`api_key` / `base_url` 是**按调用传入**的覆盖值。

        `temperature` 为 None 时**不下发该字段**，沿用各厂默认值；
        判定类调用（Jev）传 0 —— 见 `JevClient.decide` 的说明。

        它们只允许来自 `core/model_access.ModelAccess.resolve()`（已做过归属校验、
        且密钥已解密），**绝不能直接取自请求体** —— 否则用户能把任意 key 塞进网关。
        """
        raise NotImplementedError

    async def stream(self, messages: Sequence[ChatMessage], *, model: str | None,
                     timeout: float, max_tokens: int | None,
                     api_key: str | None = None,
                     base_url: str | None = None) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""          # pragma: no cover


class OllamaProvider(Provider):
    name = "ollama"
    #: Ollama 新版 `/api/chat` 支持 tools，但**模型**是否具备工具能力是另一回事：
    #: 本机 qwen2.5vl:3b 明确不支持。协议支持 ≠ 模型支持，两级不能合并。
    supports_tools = True

    def _model_supports_tools(self, model: str | None) -> bool:
        """只认白名单：本机可用的小模型大多不带工具能力，默认保守。"""
        target = model or self.settings.ollama_model
        return target in tuple(getattr(self.settings, "function_calling_models", ()) or ())

    def _payload(self, messages, model, max_tokens, stream, tools=None):
        opts: dict[str, Any] = {}
        if max_tokens is not None:
            opts["num_predict"] = max_tokens
        return {
            "model": model or self.settings.ollama_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            **({"options": opts} if opts else {}),
            **({"tools": [dict(t) for t in tools]} if tools else {}),
        }

    async def chat(self, messages, *, model=None, timeout, max_tokens=None,
                   api_key=None, base_url=None, temperature=None,
                   tools=None) -> ChatResult:
        self.reject_unsupported_tools(tools, model)
        t0 = time.perf_counter()
        payload = self._payload(messages, model, max_tokens, False, tools=tools)
        if temperature is not None:
            # `_payload` 只在给了 max_tokens 时才带 options，这里必须 setdefault
            payload.setdefault("options", {})["temperature"] = temperature
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base_url or self.settings.ollama_base_url}/api/chat",
                                  json=payload)
            r.raise_for_status()
            d = r.json()
        message = d.get("message") or {}
        return ChatResult(
            text=message.get("content", ""),
            provider=self.name, model=d.get("model", model or self.settings.ollama_model),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            usage={"prompt_tokens": d.get("prompt_eval_count", 0),
                   "completion_tokens": d.get("eval_count", 0)},
            tool_calls=_parse_ollama_tool_calls(message),
        )

    async def stream(self, messages, *, model=None, timeout, max_tokens=None,
                     api_key=None, base_url=None) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{base_url or self.settings.ollama_base_url}/api/chat",
                                     json=self._payload(messages, model, max_tokens, True)) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break


class DeepSeekProvider(Provider):
    """OpenAI 兼容协议；同一适配可复用于其他兼容厂商（多模型兼容）。"""

    name = "deepseek"
    supports_tools = True

    async def chat(self, messages, *, model=None, timeout, max_tokens=None,
                   api_key=None, base_url=None, temperature=None,
                   tools=None) -> ChatResult:
        self.reject_unsupported_tools(tools, model)
        key = api_key or self.settings.deepseek_api_key
        if not key:
            raise GatewayUnavailableError("DEEPSEEK_API_KEY 未配置")
        body: dict[str, Any] = {
            "model": model or self.settings.deepseek_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [dict(t) for t in tools]
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{base_url or self.settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
            r.raise_for_status()
            d = r.json()
        bundle = d["choices"][0]["message"] or {}
        return ChatResult(
            text=bundle.get("content") or "",
            provider=self.name, model=d.get("model", body["model"]),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            usage=d.get("usage", {}),
            tool_calls=parse_openai_tool_calls(bundle),
        )

    async def stream(self, messages, *, model=None, timeout, max_tokens=None,
                     api_key=None, base_url=None) -> AsyncIterator[str]:
        key = api_key or self.settings.deepseek_api_key
        if not key:
            raise GatewayUnavailableError("DEEPSEEK_API_KEY 未配置")
        body: dict[str, Any] = {
            "model": model or self.settings.deepseek_model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{base_url or self.settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}"}, json=body,
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        piece = json.loads(data)["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if piece:
                        yield piece


class OpenAICompatProvider(Provider):
    """用户自带的 OpenAI 兼容端点（U70 BYOK）。

    与 `DeepSeekProvider` 的差别是**没有全局兜底**：`base_url` 与 `api_key`
    必须按调用传入。这样它就不可能"不小心"用了平台凭据去跑别人的端点 ——
    少一个默认值，就少一条越权路径。
    """

    name = "openai-compatible"
    supports_tools = True

    def _require(self, api_key: str | None, base_url: str | None) -> tuple[str, str]:
        if not api_key or not base_url:
            raise GatewayUnavailableError(
                "openai-compatible 需要调用方提供 base_url 与 api_key（来自用户自有绑定）"
            )
        return api_key, base_url.rstrip("/")

    async def chat(self, messages, *, model=None, timeout, max_tokens=None,
                   api_key=None, base_url=None, temperature=None,
                   tools=None) -> ChatResult:
        self.reject_unsupported_tools(tools, model)
        key, base = self._require(api_key, base_url)
        if not model:
            raise GatewayUnavailableError("openai-compatible 必须显式给出 model")
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [dict(t) for t in tools]
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}"}, json=body)
            r.raise_for_status()
            d = r.json()
        bundle = d["choices"][0]["message"] or {}
        return ChatResult(
            text=bundle.get("content") or "",
            provider=self.name, model=d.get("model", model),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            usage=d.get("usage", {}),
            tool_calls=parse_openai_tool_calls(bundle),
        )

    async def stream(self, messages, *, model=None, timeout, max_tokens=None,
                     api_key=None, base_url=None) -> AsyncIterator[str]:
        key, base = self._require(api_key, base_url)
        if not model:
            raise GatewayUnavailableError("openai-compatible 必须显式给出 model")
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{base}/chat/completions",
                                     headers={"Authorization": f"Bearer {key}"}, json=body) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        piece = json.loads(data)["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if piece:
                        yield piece


PROVIDERS: dict[str, type[Provider]] = {
    "ollama": OllamaProvider,
    "deepseek": DeepSeekProvider,
    "openai-compatible": OpenAICompatProvider,
}



# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #
class LLMGateway:
    """多模型兼容 · 鉴权 · 限流 · 重试 · 负载均衡 · 流式转发。"""

    def __init__(self, settings: Settings, redis=None, resilience: Resilience | None = None) -> None:
        self.settings = settings
        self._redis = redis
        # 计划书三、P0-1「接口熔断、超时、降级、防雪崩」——按供应商维度建立闸门
        self.resilience = resilience or Resilience(settings=settings, redis=redis)
        self._providers = {
            name: cls(settings) for name, cls in PROVIDERS.items()
            if name in settings.provider_order
        }

    # ---- 鉴权：下游一律不得绕过网关自带用户身份 ----
    @staticmethod
    def _require_user(user_id: str | None) -> str:
        if not user_id or not str(user_id).strip():
            raise GatewayError(
                "【鉴权】调用网关必须携带 user_id。计划书三、P3："
                "调用数据中台、RAG、Wiki 自动透传 state 中的 user_id；"
                "计划书七、11：未登录状态下所有业务接口拒绝执行。"
            )
        return str(user_id)

    # ---- 限流：按账号维度统计（计划书三、P3「限流按账号维度统计」）----
    async def _check_rate(self, user_id: str, *, bucket: str, limit: int) -> None:
        if self._redis is None or limit <= 0:
            return
        window = int(time.time() // 60)
        key = redis_key("ratelimit", bucket, user_id, window)
        used = await self._redis.incr(key)
        if used == 1:
            await self._redis.expire(key, 90)
        if used > limit:
            raise GatewayRateLimited(f"账号在当前分钟内的 {bucket} 调用已达上限 {limit}")

    def _order(self) -> list[str]:
        order = [p for p in self.settings.provider_order if p in self._providers]
        return order or list(self._providers)

    # ---- 非流式 ----
    async def chat(self, *, user_id: str, messages: Sequence[ChatMessage],
                   model: str | None = None, max_tokens: int | None = None,
                   rate_limit: int | None = None,
                   rate_bucket: str = "llm",
                   provider: str | None = None,
                   api_key: str | None = None,
                   base_url: str | None = None,
                   temperature: float | None = None,
                   tools: Sequence[Mapping[str, Any]] | None = None) -> ChatResult:
        """`provider` / `api_key` / `base_url` 来自 `ModelAccess.resolve()`。

        三者一旦显式给出，就**只走这一个供应商、不做降级**：
        用户选了"我的模型"，出故障时悄悄换到平台模型，费用与数据去向都变了 ——
        那属于"未经允许"。宁可报错让用户自己换。

        `tools` 为 models 的工具清单（U112，来自 `ToolRegistry.as_openai_tools()`）。
        带工具的请求在**不支持工具的模型上是明确失败**，不会被降级到"换个供应商
        接着试"这条路径上：降级后的模型可能同样不支持，级联一圈最后只会抛出
        「全部供应商不可用」—— 那正是本机 qwen2.5vl:3b 踩过的坑（真因被淹没）。
        """
        uid = self._require_user(user_id)
        # bucket 可切换：计划书三、P3 要求「Jev 调用独立配额统计」，
        # 若与普通调用共用 llm 桶，Jev 的配额就会被业务话务挤占，等于没有独立配额。
        await self._check_rate(uid, bucket=rate_bucket,
                               limit=rate_limit or self.settings.llm_rate_limit_per_minute)
        errors: list[str] = []
        tool_unsupported: list[str] = []
        for name in self._targets(provider):            # 负载均衡 / 故障降级
            provider_impl = self._provider(name)

            async def _invoke(p=provider_impl):
                kwargs: dict[str, Any] = {"temperature": temperature}
                # **只在真有工具时才加 tools 关键字**：第三方 Provider 实现（含测试替身）
                # 不一定接受该参数 —— 不带工具的请求必须与接线前逐字一致，
                # 否则「换个供应商实现」这件最常见的扩展行为会被无谓地打破。
                if tools:
                    kwargs["tools"] = tools
                return await p.chat(messages, model=model,
                                    timeout=self.settings.llm_timeout_seconds,
                                    max_tokens=max_tokens,
                                    api_key=api_key, base_url=base_url, **kwargs)

            for attempt in range(self.settings.llm_max_retries + 1):
                try:
                    # 防雪崩 → 熔断 → 超时（降级见下方 CircuitOpenError 分支）
                    return await self.resilience.call(
                        f"llm.{name}", _invoke, timeout=self.settings.llm_timeout_seconds)
                except ToolUnsupportedError as exc:     # 不支持工具：重试也没用
                    tool_unsupported.append(str(exc))
                    errors.append(f"{name}: {exc}")
                    break
                except CircuitOpenError as exc:         # 熔断打开 → 不重试，直接换供应商
                    errors.append(f"{name}: 熔断打开 {exc}")
                    break
                except Exception as exc:                # noqa: BLE001 — 逐供应商降级
                    errors.append(f"{name}#{attempt}: {type(exc).__name__}: {exc}")
                    if attempt < self.settings.llm_max_retries:
                        await asyncio.sleep(0.4 * (attempt + 1))
        # **按真因分类报错**：是「没有支持工具的供应商」，就说「没有支持工具的供应商」，
        # 绝不说「全部供应商不可用」。真因不能被降级日志淹没 —— 这是 U112 立项的直接原因。
        if tool_unsupported and len(tool_unsupported) >= len(errors):
            raise ToolUnsupportedError(
                "没有可用于 function calling 的模型供应商 → " + " | ".join(tool_unsupported))
        raise GatewayUnavailableError("全部模型供应商不可用 → " + " | ".join(errors))

    # ---- 流式转发 ----
    async def stream(self, *, user_id: str, messages: Sequence[ChatMessage],
                     model: str | None = None, max_tokens: int | None = None,
                     rate_limit: int | None = None,
                     provider: str | None = None,
                     api_key: str | None = None,
                     base_url: str | None = None) -> AsyncIterator[str]:
        uid = self._require_user(user_id)
        await self._check_rate(uid, bucket="llm",
                               limit=rate_limit or self.settings.llm_rate_limit_per_minute)
        errors: list[str] = []
        for name in self._targets(provider):
            provider_impl = self._provider(name)
            breaker = self.resilience.breaker(f"llm.{name}")
            try:
                await breaker.allow()                   # 熔断
            except CircuitOpenError as exc:
                errors.append(f"{name}: 熔断打开 {exc}")
                continue
            try:
                # 流式响应不设总时长上限（计划书三、P0-7 要求逐字流式播报），
                # 仅以并发闸防雪崩；整段流的成败回写给熔断器。
                async with self.resilience.bulkhead(f"llm.{name}"):
                    async for piece in provider_impl.stream(
                            messages, model=model,
                            timeout=self.settings.llm_timeout_seconds,
                            max_tokens=max_tokens,
                            api_key=api_key, base_url=base_url):
                        yield piece
                await breaker.record_success()
                return
            except Exception as exc:                    # noqa: BLE001
                await breaker.record_failure()
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        raise GatewayUnavailableError("全部模型供应商流式不可用 → " + " | ".join(errors))

    def _targets(self, provider: str | None) -> list[str]:
        """显式指定供应商时只走它；否则按配置顺序降级。"""
        if provider:
            return [provider]
        return self._order()

    def _provider(self, name: str) -> Provider:
        """按需构造供应商。

        `openai-compatible`（用户自带端点）**不在** `settings.provider_order` 里，
        所以不能只靠构造期的 `_providers` 字典 —— 取不到就现建一个。
        缓存是安全的：Provider 实例只持有 `settings`，**用户凭据是按调用传参**的，
        不驻留在实例上，因此不会把上一个用户的配置留给下一个用户。
        """
        existing = self._providers.get(name)
        if existing is not None:
            return existing
        cls = PROVIDERS.get(name)
        if cls is None:
            raise GatewayUnavailableError(f"未知的模型供应商：{name!r}")
        impl = cls(self.settings)
        self._providers[name] = impl
        return impl


# --------------------------------------------------------------------------- #
# Jev 决策模型标准化调用接口
# --------------------------------------------------------------------------- #
_JEV_SYSTEM = (
    "你是结构化决策节点。只允许输出一个 JSON 对象，形如 "
    '{"label": "<枚举标签>", "confidence": <0~1 的小数>, '
    '"candidate_labels": ["<枚举标签>", "..."], '
    '"require_clarification": <0 或 1>, '
    '"clarification_category": "<枚举标签或空字符串>"}。'
    "禁止输出任何解释、自然语言句子、Markdown 代码块或额外字段。"
    "label 必须从给定候选标签中原样选取。"
    "candidate_labels 只能取自给定候选标签。"
    "当请求包含多个无法自行排序的意图、或超出候选标签覆盖范围时，"
    "必须令 require_clarification=1，并在 clarification_category 填写 "
    f"{list(CLARIFICATION_CATEGORIES)} 之一。"
    "**澄清话术由系统生成，你不得输出任何询问语句**。"
)


@dataclass
class JevClient:
    """只产出「枚举标签 + 置信概率」。任何自由文本一律视为无效并降级。"""

    gateway: LLMGateway
    settings: Settings

    async def decide(self, *, user_id: str, question: str, allowed_labels: Sequence[str],
                     model: str | None = None) -> JevDecision:
        if not allowed_labels:
            raise ValueError(
                "【文档未定义项 #1】候选标签集为空。计划书只规定 Jev 输出结构化枚举标签，"
                "未给出标签清单，禁止 AI 自行编造；须由人工补充定义后传入。"
            )
        if not self.settings.jev_enable:
            return JevDecision(None, 0.0, "fallback", reason="jev_enable=false")

        try:
            result = await self.gateway.chat(
                user_id=user_id,
                messages=[
                    ChatMessage("system", _JEV_SYSTEM),
                    ChatMessage("user", f"候选标签：{list(allowed_labels)}\n待判定问题：{question}"),
                ],
                model=model, max_tokens=self.settings.jev_max_tokens,
                # **判定节点必须可复现**：温度不定时，同一条句子两次可能给出
                # 不同标签与置信，路由行为就成了不可复现的（评测也无从比较）。
                # 这不是「让模型更准」，是让结论稳定 —— 准不准由 prompt 与模型决定。
                temperature=0.0,
                # 计划书三、P3「Jev 调用独立配额统计」：走独立 bucket，不占业务话务配额
                rate_limit=self.settings.jev_rate_limit_per_minute,
                rate_bucket="jev",
            )
            structured = self._parse(result.text, allowed_labels)
        except Exception as exc:                        # noqa: BLE001 — 故障可降级
            return JevDecision(None, 0.0, "fallback",
                               reason=f"Jev 调用失败，降级下游路由：{type(exc).__name__}")

        def build(source: str, *, drop_clarification: bool = False,
                  **extra: Any) -> JevDecision:
            return JevDecision(
                structured.label, structured.confidence, source,      # type: ignore[arg-type]
                candidate_labels=structured.candidate_labels,
                # 关闭澄清采信时**必须同时清空这两个字段**：留着 True 会让
                # JevLayer 继续按 JEV-003 暂停分发，等于开关没起作用。
                require_clarification=(False if drop_clarification
                                       else structured.require_clarification),
                clarification_category=("" if drop_clarification
                                        else structured.clarification_category),
                **extra)

        if self.settings.jev_shadow_mode:
            # 影子模式：结果只做对照记录，不参与业务决策
            return build("fallback", shadow=True,
                         reason="jev_shadow_mode=true：结果仅记录，未采用")

        if structured.require_clarification:
            # 澄清是**安全动作**：不受置信阈值门控。
            # 若被阈值拦下，低置信时就可能漏掉「必须向用户确认」的多意图冲突。
            if not self.settings.jev_allow_clarify:
                # E-80：**只在弱模型上关**。本机 3B 把「信息不足」判成「多意图」，
                # 6 次澄清 6 次错；云端 deepseek-chat 实测澄清集中在 multi_intent
                # 与 unknown 两类（正是该问的），因此**默认是开的**。
                # 关掉的是「采信这个模型的澄清请求」，JEV-003 的机制原样保留：
                # 一旦采信，路由暂停分发照旧生效。
                # 关掉之后的后果是**退回确定性判据**，不是「自动选优先级」：
                # planner 的正则多意图判定仍然是权威澄清来源。
                return build("fallback", drop_clarification=True,
                             reason="模型要求澄清，但 jev_allow_clarify=false（弱模型误报），"
                                    "退回确定性分类")
            return build("jev")

        if structured.confidence < self.settings.jev_confidence_threshold:
            if self.settings.jev_fallback_enable:
                return build("fallback",
                             reason=f"置信 {structured.confidence:.3f} < 阈值 "
                                    f"{self.settings.jev_confidence_threshold}，向下流转")
            return build("fallback", reason="置信不足且未开启降级")
        return build("jev")

    @staticmethod
    def _parse(text: str, allowed_labels: Sequence[str]) -> JevStructured:
        """严格解析。解析不出结构化枚举即判无效——绝不把自由文本放行到下游。"""
        raw = text.strip()
        if raw.startswith("" + chr(96) * 3):
            raw = raw.strip(chr(96)).removeprefix("json").strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Jev 输出不是合法 JSON，判定为自由文本，禁止采用") from exc
        if not isinstance(obj, dict):
            raise ValueError("Jev 输出必须是 JSON 对象，禁止采用数组或标量")

        label = obj.get("label")
        if label not in allowed_labels:
            raise ValueError(f"Jev 输出标签 {label!r} 不在候选集内，禁止采用")
        try:
            confidence = float(obj.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Jev 未给出可解析的置信概率") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("置信概率必须落在 0~1")

        # --- U01：候选意图列表 ---
        raw_candidates = obj.get("candidate_labels") or []
        if not isinstance(raw_candidates, list):
            raise ValueError("candidate_labels 必须是列表")
        candidates: list[str] = []
        for item in raw_candidates:
            if item not in allowed_labels:
                raise ValueError(f"候选意图 {item!r} 不在候选集内，禁止采用")
            if item not in candidates:
                candidates.append(item)

        # --- U14：是否必须澄清 + 澄清类别（均为枚举，无自由文本）---
        flag = obj.get("require_clarification", False)
        if isinstance(flag, bool):
            require = flag
        elif isinstance(flag, int) and flag in (0, 1):
            require = bool(flag)
        else:
            raise ValueError("require_clarification 必须是 0/1 或布尔值")

        category = obj.get("clarification_category") or ""
        if require and category not in CLARIFICATION_CATEGORIES:
            raise ValueError(
                f"require_clarification=1 时必须给出澄清类别，可选 {list(CLARIFICATION_CATEGORIES)}")
        if not require and category:
            raise ValueError("未要求澄清时不得给出澄清类别")

        return JevStructured(label=label, confidence=confidence,
                             candidate_labels=tuple(candidates),
                             require_clarification=require,
                             clarification_category=category)
