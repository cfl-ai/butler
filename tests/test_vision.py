"""图文识别 / 图文输出的守护测试（台账 U61/U62）。

测什么、为什么测这些
--------------------
1. **不接受的输入必须拒绝**：非图片、空图、像素炸弹 —— 一律 VisionRejectedError，
   不许「猜一个结果」。
2. **不可用必须报错**：没有视觉供应商时抛 VisionUnavailableError 且原因可执行
   （例如「先 ollama pull」），这是本项目 fail-closed 的底线。
3. **客观事实必须真**：尺寸/亮度/主色不依赖模型，必须能被独立验证。
4. **图文输出的图必须真是 PNG**：卡片要能解码，不能只返回一段 base64 字符串。
5. **接口层的状态码要对**：401/400/413/502/503 各归其位，别把配置问题伪装成成功。
"""

from __future__ import annotations

import asyncio
import base64
import io

import httpx
import pytest
from PIL import Image

from ai_butler.app import MAX_BASE64_CHARS, create_app
from ai_butler.core.users import UserRegistry
from ai_butler.infra import vision as vision_mod
from ai_butler.infra.vision import (
    VisionError,
    VisionGateway,
    VisionRejectedError,
    VisionResult,
    VisionUnavailableError,
    decode_image,
    normalize_to_png,
    render_card,
)
from ai_butler.middleware.session_auth import login as issue_session


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _png(width: int = 320, height: int = 120, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _is_png(raw: bytes) -> bool:
    return raw[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# 输入拒绝（fail-closed 第一层）
# --------------------------------------------------------------------------- #
def test_empty_bytes_rejected():
    with pytest.raises(VisionRejectedError):
        decode_image(b"")


def test_non_image_bytes_rejected():
    with pytest.raises(VisionRejectedError):
        decode_image(b"this is definitely not an image")


def test_pixel_bomb_rejected(monkeypatch):
    """像素炸弹：不拦住会把内存吃穿，必须拒绝而不是试着解码。"""
    monkeypatch.setattr(vision_mod, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(VisionRejectedError) as info:
        decode_image(_png(320, 120))
    assert "像素" in str(info.value)


# --------------------------------------------------------------------------- #
# 归一化与客观事实
# --------------------------------------------------------------------------- #
def test_normalize_downscales_long_side():
    png, facts = normalize_to_png(_png(3000, 1000), max_side=1600)
    assert _is_png(png)
    assert facts["width"] == 1600, "长边必须缩到上限"
    assert facts["height"] == 533            # 等比：1000 * 1600/3000
    assert facts["original_width"] == 3000, "原尺寸要如实保留，别把缩小后的当原图"


def test_facts_reflect_real_pixels():
    _, facts = normalize_to_png(_png(64, 64, "white"))
    assert facts["brightness"] >= 250, "纯白图亮度必须接近 255"
    # 调色板按 32 量化（255 → 224），这是**有意的**：目的是归并相近色，
    # 所以断言「接近纯白且占比压倒性」，而不是断言精确 255。
    top = facts["palette"][0]
    assert all(channel >= 224 for channel in top["rgb"])
    assert top["share"] > 0.9


def test_facts_dark_image_has_low_brightness():
    _, facts = normalize_to_png(_png(64, 64, "black"))
    assert facts["brightness"] <= 5


# --------------------------------------------------------------------------- #
# 图文输出：卡片
# --------------------------------------------------------------------------- #
def test_card_is_decodable_png():
    raw = render_card("识别结果", "第一行\n第二行", {"width": 10, "height": 10})
    assert _is_png(raw)
    img = Image.open(io.BytesIO(raw))
    assert img.width >= 300 and img.height >= 200


def test_card_wraps_long_text():
    """长文本要折行变高，不能溢出画布。"""
    short = render_card("t", "短", {"width": 1, "height": 1})
    long = render_card("t", "长" * 800, {"width": 1, "height": 1})
    assert len(long) > len(short)


def test_card_flags_missing_cjk_font(monkeypatch):
    """没有中文字体时必须如实标注，不能默默出方块图。"""
    monkeypatch.setattr(vision_mod, "_CJK_FONTS", ())
    font, has_cjk = vision_mod._load_font(20)
    assert has_cjk is False
    raw = render_card("标题", "正文", {"width": 1, "height": 1})
    assert _is_png(raw)


# --------------------------------------------------------------------------- #
# 网关：可用性与 fail-closed
# --------------------------------------------------------------------------- #
class _FakeProvider:
    def __init__(self, name: str, reason: str | None = None, text: str = "识别成功"):
        self.name = name
        self._reason = reason
        self._text = text
        self.calls = 0

    def unavailable_reason(self) -> str | None:
        return self._reason

    async def recognize(self, png: bytes, *, prompt: str, timeout: float):
        self.calls += 1
        assert _is_png(png), "喂给模型的必须是归一化后的 PNG"
        assert prompt, "识别指令不能为空"
        return self._text, f"{self.name}-model"


def test_gateway_rejects_unknown_provider_order(settings):
    broken = settings.model_copy(update={"vision_provider_order": "nope"})
    with pytest.raises(VisionUnavailableError):
        VisionGateway(broken)


def test_gateway_fail_closed_when_no_provider(settings):
    gateway = VisionGateway(settings)
    gateway._providers = [_FakeProvider("a", reason="模型未拉取"),
                          _FakeProvider("b", reason="未配置密钥")]
    with pytest.raises(VisionUnavailableError) as info:
        asyncio.run(gateway.understand(_png()))
    message = str(info.value)
    assert "模型未拉取" in message and "未配置密钥" in message, \
        "必须把所有不可用原因一次说清"


def test_gateway_skips_unavailable_and_uses_next(settings):
    gateway = VisionGateway(settings)
    dead = _FakeProvider("dead", reason="模型未拉取")
    alive = _FakeProvider("alive", text="订单编号 AB-2026-1024")
    gateway._providers = [dead, alive]

    async def main():
        return await gateway.understand(_png())

    result = asyncio.run(main())
    assert isinstance(result, VisionResult)
    assert result.provider == "alive"
    assert dead.calls == 0, "不可用的供应商不该被调用"
    assert "AB-2026-1024" in result.text
    assert result.card_png and _is_png(result.card_png), "默认要产出图文输出的卡片"


def test_gateway_can_skip_card(settings):
    gateway = VisionGateway(settings)
    gateway._providers = [_FakeProvider("ok", text="x")]
    result = asyncio.run(gateway.understand(_png(), want_card=False))
    assert result.card_png is None


def test_gateway_propagates_provider_error(settings):
    class _Broken(_FakeProvider):
        async def recognize(self, png, *, prompt, timeout):
            raise VisionError("模型返回空文本")

    gateway = VisionGateway(settings)
    gateway._providers = [_Broken("ok")]
    with pytest.raises(VisionError):
        asyncio.run(gateway.understand(_png()))


def test_ollama_reason_is_actionable_when_model_missing(settings):
    """模型没拉取时，原因里必须给出「ollama pull」这种可执行动作。"""
    provider = vision_mod.OllamaVisionProvider(
        settings.model_copy(update={"ollama_vision_model": "definitely-missing:1b"}))

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            request = httpx.Request("GET", url)
            return httpx.Response(200, json={"models": [{"name": "moondream:latest"}]},
                                  request=request)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(vision_mod.httpx, "Client", _Client)
    try:
        reason = provider.unavailable_reason()
    finally:
        monkeypatch.undo()
    assert reason and "ollama pull" in reason
    assert "moondream" in reason, "要把现有模型列出来，用户才知道能换成什么"


def test_ollama_reason_reports_unreachable(settings):
    provider = vision_mod.OllamaVisionProvider(
        settings.model_copy(update={"ollama_vision_model": "moondream"}))

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise RuntimeError("connection refused")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(vision_mod.httpx, "Client", _Boom)
    try:
        reason = provider.unavailable_reason()
    finally:
        monkeypatch.undo()
    assert reason and "不可达" in reason


# --------------------------------------------------------------------------- #
# 接口层
# --------------------------------------------------------------------------- #
def _client(settings, redis_client):
    app = create_app(settings, redis_client)
    return app, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t")


def _seed(app, *, user_id="u-vision", password="correct-horse"):
    registry: UserRegistry = app.state.users
    try:
        registry.register(user_id=user_id, password=password)
    except Exception:
        pass


async def _token(app, settings, user_id="u-vision"):
    return await issue_session(store=app.state.session_store, settings=settings,
                               user_id=user_id, device_id="d-1")


def _stub_gateway(monkeypatch, *, result=None, error=None):
    class _Gateway:
        def __init__(self, settings):
            pass

        def availability(self):
            return [{"provider": "stub", "reason": None}]

        async def understand(self, raw, *, prompt=None, want_card=True, card_title="x"):
            if error:
                raise error
            return result

    monkeypatch.setattr("ai_butler.app.VisionGateway", _Gateway)


def test_recognize_requires_session(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        async with client:
            return await client.post("/api/vision/recognize",
                                     json={"image_base64": "AAAA"})

    response = asyncio.run(main())
    assert response.status_code == 401


def test_recognize_rejects_bad_base64(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        token = await _token(app, settings)
        async with client:
            return await client.post("/api/vision/recognize",
                                     headers={"Authorization": f"Bearer {token.token}"},
                                     json={"image_base64": "!!!not-base64!!!"})

    response = asyncio.run(main())
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_recognize_rejects_oversized_payload(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        token = await _token(app, settings)
        async with client:
            return await client.post("/api/vision/recognize",
                                     headers={"Authorization": f"Bearer {token.token}"},
                                     json={"image_base64": "A" * (MAX_BASE64_CHARS + 4)})

    response = asyncio.run(main())
    assert response.status_code == 413


def test_recognize_success_returns_text_and_image(settings, redis_client, monkeypatch):
    card = render_card("图文识别结果", "订单编号 AB-2026-1024", {"width": 1, "height": 1})
    _stub_gateway(monkeypatch, result=VisionResult(
        text="订单编号 AB-2026-1024", provider="stub", model="stub-m",
        elapsed_ms=12, facts={"width": 1, "height": 1}, card_png=card))

    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        token = await _token(app, settings)
        async with client:
            return await client.post(
                "/api/vision/recognize",
                headers={"Authorization": f"Bearer {token.token}"},
                json={"image_base64": base64.b64encode(_png()).decode()})

    response = asyncio.run(main())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "订单编号 AB-2026-1024"
    kinds = [b["type"] for b in body["blocks"]]
    assert kinds == ["text", "image"], "图文输出必须同时含文字与图片两块"
    image_block = body["blocks"][1]
    assert image_block["purpose"] == "card"
    assert _is_png(base64.b64decode(image_block["data_base64"]))


def test_recognize_returns_503_when_no_provider(settings, redis_client, monkeypatch):
    _stub_gateway(monkeypatch, error=VisionUnavailableError("没有可用的视觉供应商"))

    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        token = await _token(app, settings)
        async with client:
            return await client.post(
                "/api/vision/recognize",
                headers={"Authorization": f"Bearer {token.token}"},
                json={"image_base64": base64.b64encode(_png()).decode()})

    response = asyncio.run(main())
    assert response.status_code == 503
    assert response.json()["code"] == "vision_unavailable"


def test_recognize_returns_400_on_rejected_image(settings, redis_client, monkeypatch):
    _stub_gateway(monkeypatch, error=VisionRejectedError("无法解析图片"))

    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        token = await _token(app, settings)
        async with client:
            return await client.post(
                "/api/vision/recognize",
                headers={"Authorization": f"Bearer {token.token}"},
                json={"image_base64": base64.b64encode(b"nope").decode()})

    response = asyncio.run(main())
    assert response.status_code == 400
    assert response.json()["code"] == "image_rejected"


def test_availability_endpoint_reports_reasons(settings, redis_client):
    async def main():
        app, client = _client(settings, redis_client)
        _seed(app)
        token = await _token(app, settings)
        async with client:
            return await client.get("/api/vision/availability",
                                    headers={"Authorization": f"Bearer {token.token}"})

    response = asyncio.run(main())
    assert response.status_code == 200
    body = response.json()
    assert "providers" in body and isinstance(body["available"], bool)


# --------------------------------------------------------------------------- #
# 超时：视觉接口必须单独放宽（端到端实测过 504，见台账 U61）
# --------------------------------------------------------------------------- #
def test_timeout_middleware_honours_path_override():
    import asyncio as _asyncio

    from ai_butler.middleware.global_middleware import TimeoutMiddleware

    async def slow_app(scope, receive, send):
        await _asyncio.sleep(0.25)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = TimeoutMiddleware(slow_app, timeout_seconds=0.05,
                                   path_timeouts={"/api/vision/": 2.0})

    async def call(path):
        statuses: list[int] = []

        async def send(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        await middleware({"type": "http", "path": path, "method": "POST"},
                         _receive_noop, send)
        return statuses

    async def _receive_noop():
        return {"type": "http.request", "body": b"", "more_body": False}

    assert asyncio.run(call("/api/vision/recognize")) == [200], \
        "视觉接口走单独的超时，不该被通用 30s/0.05s 掐断"
    assert asyncio.run(call("/api/other")) == [504], \
        "其它接口仍然受通用超时约束"
