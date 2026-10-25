"""P0-1 统一 LLM 网关与 Jev 决策接口测试。"""

from __future__ import annotations

import asyncio
import json

import pytest

from ai_butler.infra.llm_gateway import (
    ChatMessage,
    ChatResult,
    GatewayError,
    GatewayUnavailableError,
    JevClient,
    LLMGateway,
    Provider,
)


class StubProvider(Provider):
    def __init__(self, settings, *, name="stub", text="", fail=False):
        super().__init__(settings)
        self.name = name
        self._text = text
        self._fail = fail

    async def chat(self, messages, *, model=None, timeout=0, max_tokens=None,
                   api_key=None, base_url=None, temperature=None):
        """`temperature` 是 U74 新增的判定确定性参数，桩必须跟着契约走。"""
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        return ChatResult(text=self._text, provider=self.name, model="stub", latency_ms=1)

    async def stream(self, messages, *, model=None, timeout=0, max_tokens=None,
                     api_key=None, base_url=None):
        if self._fail:
            raise RuntimeError(f"{self.name} down")
        for ch in ["流", "式", "转发"]:
            yield ch


def _gw(settings, providers):
    gw = LLMGateway(settings, redis=None)
    gw._providers = {p.name: p for p in providers}
    gw.settings = settings.model_copy(update={"llm_provider_order": ",".join(p.name for p in providers)})
    return gw


def _msg():
    return [ChatMessage("user", "你好")]


def test_gateway_requires_user_id(settings):
    """鉴权：下游不得绕过网关自带用户身份。"""
    gw = _gw(settings, [StubProvider(settings, text="x")])

    async def main():
        with pytest.raises(GatewayError):
            await gw.chat(user_id="", messages=_msg())

    asyncio.run(main())


def test_provider_failover(settings):
    """负载均衡 / 故障降级：首选故障时自动切换。"""
    gw = _gw(settings, [StubProvider(settings, name="p1", fail=True),
                        StubProvider(settings, name="p2", text="备用成功")])

    async def main():
        return await gw.chat(user_id="u-1", messages=_msg())

    r = asyncio.run(main())
    assert r.text == "备用成功" and r.provider == "p2"


def test_all_providers_down(settings):
    gw = _gw(settings, [StubProvider(settings, name="p1", fail=True)])

    async def main():
        with pytest.raises(GatewayUnavailableError):
            await gw.chat(user_id="u-1", messages=_msg())

    asyncio.run(main())


def test_streaming_forward(settings):
    gw = _gw(settings, [StubProvider(settings, text="x")])

    async def main():
        return [c async for c in gw.stream(user_id="u-1", messages=_msg())]

    assert "".join(asyncio.run(main())) == "流式转发"


# ------------------------- Jev 决策模型接口 ------------------------- #
LABELS = ["旅游", "点餐", "支付"]


def _jev(settings, text, **over):
    s = settings.model_copy(update=over)
    return JevClient(gateway=_gw(s, [StubProvider(s, text=text)]), settings=s)


def test_jev_accepts_structured_enum_above_threshold(settings):
    payload = json.dumps({"label": "旅游", "confidence": 0.95})
    d = asyncio.run(_jev(settings, payload, jev_shadow_mode=False)
                    .decide(user_id="u-1", question="帮我安排行程", allowed_labels=LABELS))
    assert d.accepted and d.label == "旅游" and d.confidence == 0.95


def test_jev_never_passes_free_text_downstream(settings):
    """计划书：Jev 只输出结构化枚举标签 + 置信概率，禁止输出自由自然语言文本。"""
    d = asyncio.run(_jev(settings, "我觉得用户可能想去旅游吧", jev_shadow_mode=False)
                    .decide(user_id="u-1", question="?", allowed_labels=LABELS))
    assert not d.accepted and d.label is None and d.source == "fallback"


def test_jev_rejects_label_outside_allowed_set(settings):
    payload = json.dumps({"label": "订机票", "confidence": 0.99})
    d = asyncio.run(_jev(settings, payload, jev_shadow_mode=False)
                    .decide(user_id="u-1", question="?", allowed_labels=LABELS))
    assert not d.accepted


def test_jev_below_threshold_falls_back_downstream(settings):
    """计划书：置信不足向下流转（jev_confidence_threshold=0.82）。"""
    payload = json.dumps({"label": "旅游", "confidence": 0.5})
    d = asyncio.run(_jev(settings, payload, jev_shadow_mode=False)
                    .decide(user_id="u-1", question="?", allowed_labels=LABELS))
    assert not d.accepted and d.label == "旅游" and "0.82" in d.reason


def test_jev_shadow_mode_records_but_does_not_decide(settings):
    payload = json.dumps({"label": "旅游", "confidence": 0.99})
    d = asyncio.run(_jev(settings, payload, jev_shadow_mode=True)
                    .decide(user_id="u-1", question="?", allowed_labels=LABELS))
    assert not d.accepted and d.shadow is True and d.label == "旅游"


def test_jev_fault_degrades_smoothly(settings):
    s = settings.model_copy(update={"jev_shadow_mode": False})
    jev = JevClient(gateway=_gw(s, [StubProvider(s, name="p1", fail=True)]), settings=s)
    d = asyncio.run(jev.decide(user_id="u-1", question="?", allowed_labels=LABELS))
    assert not d.accepted and "降级" in d.reason


def test_jev_disabled(settings):
    d = asyncio.run(_jev(settings, "{}", jev_enable=False)
                    .decide(user_id="u-1", question="?", allowed_labels=LABELS))
    assert not d.accepted and "jev_enable=false" in d.reason


def test_jev_requires_label_set_from_human(settings):
    """【文档未定义项 #1】计划书未给出标签清单，禁止 AI 编造。"""
    with pytest.raises(ValueError) as ei:
        asyncio.run(_jev(settings, "{}").decide(user_id="u-1", question="?", allowed_labels=[]))
    assert "文档未定义项" in str(ei.value)


@pytest.mark.live
def test_live_ollama_through_gateway(settings):
    """真实联调：经网关调用本机 Ollama。"""
    s = settings.model_copy(update={"llm_provider_order": "ollama"})
    gw = LLMGateway(s, redis=None)
    r = asyncio.run(gw.chat(user_id="u-live",
                            messages=[ChatMessage("user", "只回复两个字：连通")],
                            max_tokens=16))
    assert r.provider == "ollama" and r.text.strip()


# --------------------------------------------------------------------------- #
# 计划书三、P3「Jev 调用独立配额统计」
# --------------------------------------------------------------------------- #
def test_jev_uses_an_independent_rate_bucket(settings):
    """Jev 与业务话务必须各占一个配额桶，否则独立配额形同虚设。"""
    import asyncio

    import fakeredis.aioredis

    from ai_butler.core.naming import redis_key
    from ai_butler.infra.llm_gateway import GatewayRateLimited

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gw = LLMGateway(settings, redis=redis)
    gw._providers = {"stub": StubProvider(settings, text="ok")}
    gw.settings = settings.model_copy(update={
        "llm_provider_order": "stub",
        "llm_rate_limit_per_minute": 1,
        "jev_rate_limit_per_minute": 1,
    })

    async def main():
        # 业务话务把 llm 桶用满
        await gw.chat(user_id="u-1", messages=_msg())
        with pytest.raises(GatewayRateLimited):
            await gw.chat(user_id="u-1", messages=_msg())

        # Jev 走独立桶：业务配额已满也不受影响
        await gw.chat(user_id="u-1", messages=_msg(), rate_limit=1, rate_bucket="jev")

        keys = [k async for k in redis.scan_iter(match="*ratelimit*")]
        keys.sort()
        assert any(":llm:" in k for k in keys), keys
        assert any(":jev:" in k for k in keys), keys
        await redis.aclose()

    asyncio.run(main())


def test_jev_client_passes_the_dedicated_bucket(settings):
    """接口层回归锁定：JevClient 必须显式请求 jev 桶，不能沿用默认 llm 桶。"""
    import inspect

    from ai_butler.infra.llm_gateway import JevClient

    gw = LLMGateway(settings, redis=None)
    jev = JevClient(gateway=gw, settings=settings)
    captured: dict[str, object] = {}

    async def fake_chat(**kwargs):
        captured.update(kwargs)
        return ChatResult(text='{"label": "a", "confidence": 0.9}', provider="stub",
                          model="stub", latency_ms=1)

    gw.chat = fake_chat                       # type: ignore[assignment]
    import asyncio
    asyncio.run(jev.decide(user_id="u-1", question="q", allowed_labels=["a"]))
    assert captured.get("rate_bucket") == "jev"
    assert "rate_bucket" in inspect.signature(LLMGateway.chat).parameters
