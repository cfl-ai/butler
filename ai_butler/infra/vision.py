"""图文识别与图文输出（P7 交互入口的「图片」那一路）。

计划书依据与台账
----------------
- 三、P7 步骤「双输入」原文只定义了**语音 + 文字**两种输入，
  **图片输入与图文输出计划书未定义**。本次由人工指令（2026-10-24
  「支持图文识别和输出」）追加，按项目铁律登记台账（见 decisions U61/U62），
  不冒充计划书既有条目。
- 端云分层（一、项目概述）：移动端只负责上传与渲染，**识别在云端完成**。

失败语义（fail-closed，本项目最看重的一条）
-------------------------------------------
没有可用视觉模型时**必须抛错**，绝不许返回「看起来像结果」的空文本：
用户拿一段模型编造的描述去下单，比拿到一句「未配置视觉模型」危险得多。
同理，图片解不开、格式不支持、超限，一律拒绝而不是降级成猜测。
"""

from __future__ import annotations

import base64
import io
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from PIL import Image, ImageDraw, ImageFont

from ai_butler.core.config import Settings

# 默认识别指令：先逐行抄文字，再给关键信息；明确禁止编造。
DEFAULT_PROMPT = (
    "请识别这张图片。要求：\n"
    "1. 先逐行输出图中**所有可读文字**，保持原有顺序与换行；\n"
    "2. 再给出关键信息（例如单据号、金额、日期、人名、商品名、地址）；\n"
    "3. 不要编造图中不存在的内容；无法辨认的字用「□」代替；\n"
    "4. 若图中没有任何文字，就客观描述画面内容。"
)

MAX_IMAGE_PIXELS = 40_000_000      # 防解压炸弹：超出直接拒绝，而不是把内存吃穿


class VisionError(Exception):
    """视觉能力的统一错误基类。"""


class VisionUnavailableError(VisionError):
    """没有任何可用的视觉供应商（未配置 / 模型未拉取 / 服务不可达）。"""


class VisionRejectedError(VisionError):
    """输入不被接受（非图片、损坏、尺寸或体积超限）。"""


@dataclass
class VisionResult:
    """一次图文识别的结果。

    `text` 是模型输出；`facts` 是**不依赖模型**的客观图像事实
    （尺寸、格式、主色、亮度），任何情况下都真实可得，用于交叉校验，
    避免用户只拿到一段无法验证的模型文本。
    """

    text: str
    provider: str
    model: str
    elapsed_ms: int
    facts: dict[str, Any] = field(default_factory=dict)
    card_png: bytes | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 图像基础处理（真事实，不依赖模型）
# --------------------------------------------------------------------------- #
def decode_image(raw: bytes) -> Image.Image:
    """把字节解码成 PIL 图像。**任何异常都转成拒绝**，不做猜测性修复。"""
    if not raw:
        raise VisionRejectedError("图片内容为空")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as error:                     # noqa: BLE001 - 统一转成业务错误
        raise VisionRejectedError(f"无法解析图片：{error}") from error
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise VisionRejectedError(
            f"图片像素过大（{img.width}x{img.height}），上限 {MAX_IMAGE_PIXELS}")
    return img


def normalize_to_png(raw: bytes, *, max_side: int = 1600) -> tuple[bytes, dict[str, Any]]:
    """统一成 PNG（按 EXIF 摆正、超长边等比缩小），同时给出客观事实。"""
    img = decode_image(raw)
    original = (img.width, img.height)

    # 手机拍的图常带 EXIF 方向标记，不摆正会「躺着」进模型
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:                              # noqa: BLE001 - EXIF 损坏不影响主流程
        pass

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max_side and max(img.width, img.height) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()

    facts = image_facts(img, original=original, png_bytes=png)
    return png, facts


def image_facts(
    img: Image.Image, *, original: tuple[int, int] | None = None,
    png_bytes: bytes | None = None,
) -> dict[str, Any]:
    """客观图像事实：尺寸、主色、亮度。**这些结论不依赖任何模型。**"""
    small = img.convert("RGB").resize((64, 64), Image.LANCZOS)
    # 用 tobytes 而不是 getdata：后者在 Pillow 12 已弃用（2027 移除）
    raw = small.tobytes()
    total = len(raw) // 3 or 1
    pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw) - 2, 3)]
    avg = tuple(round(sum(p[i] for p in pixels) / total) for i in range(3))
    brightness = round((avg[0] * 299 + avg[1] * 587 + avg[2] * 114) / 1000)

    quantized: dict[tuple[int, int, int], int] = {}
    for r, g, b in pixels:
        key = (r // 32 * 32, g // 32 * 32, b // 32 * 32)
        quantized[key] = quantized.get(key, 0) + 1
    palette = [
        {"rgb": list(color), "share": round(count / total, 3)}
        for color, count in sorted(quantized.items(), key=lambda kv: kv[1], reverse=True)[:3]
    ]

    return {
        "width": img.width,
        "height": img.height,
        "original_width": (original or (img.width, img.height))[0],
        "original_height": (original or (img.width, img.height))[1],
        "avg_rgb": list(avg),
        "brightness": brightness,
        "palette": palette,
        "png_bytes": len(png_bytes) if png_bytes else None,
    }


# --------------------------------------------------------------------------- #
# 供应商
# --------------------------------------------------------------------------- #
class VisionProvider(Protocol):
    name: str

    def unavailable_reason(self) -> str | None:
        """返回 None 表示可用；否则返回**可执行**的原因说明。"""

    async def recognize(
        self, png: bytes, *, prompt: str, timeout: float
    ) -> tuple[str, str]:
        """返回 (识别文本, 实际使用的模型名)。"""


class OllamaVisionProvider:
    """本地 Ollama 视觉模型（llava / moondream / qwen2.5vl 等）。

    为什么优先用它：完全离线、无额外密钥，且「模型没拉取」这种状态可以
    通过 /api/tags **查出来**，不需要靠调用失败去猜。
    """

    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.model = (settings.ollama_vision_model or settings.ollama_model or "").strip()

    def unavailable_reason(self) -> str | None:
        if not self.model:
            return "未配置 OLLAMA_VISION_MODEL（也没有 OLLAMA_MODEL 可回退）"
        try:
            with httpx.Client(timeout=5.0) as client:
                tags = client.get(f"{self.base_url}/api/tags").json()
        except Exception as error:                 # noqa: BLE001
            return f"Ollama 服务不可达（{self.base_url}）：{error}"
        names = {m.get("name", "") for m in tags.get("models", [])}
        # ollama 的 name 可能带 :latest 后缀，按前缀宽松匹配
        if not any(n == self.model or n.startswith(self.model + ":") for n in names):
            return (f"Ollama 已安装的模型中没有 {self.model}；"
                    f"现有：{', '.join(sorted(names)) or '（空）'}。"
                    f"执行 `ollama pull {self.model}` 后重试")
        return None

    async def recognize(self, png: bytes, *, prompt: str, timeout: float) -> tuple[str, str]:
        body = {
            "model": self.model,
            "prompt": prompt,
            "images": [base64.b64encode(png).decode("ascii")],
            "stream": False,
            "options": {"temperature": 0},          # 识别要可复现，不要发挥
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{self.base_url}/api/generate", json=body)
            response.raise_for_status()
            payload = response.json()
        text = str(payload.get("response") or "").strip()
        if not text:
            raise VisionError("Ollama 返回了空文本（模型可能不接受图片输入）")
        return text, self.model


class OpenAICompatVisionProvider:
    """任意 OpenAI 兼容的多模态端点（自建 / 云端皆可），按需配置。"""

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.openai_vision_base_url.rstrip("/")
        self.api_key = settings.openai_vision_api_key
        self.model = settings.openai_vision_model

    def unavailable_reason(self) -> str | None:
        if not (self.base_url and self.api_key and self.model):
            return ("未配置 OPENAI_VISION_BASE_URL / API_KEY / MODEL 三件套")
        return None

    async def recognize(self, png: bytes, *, prompt: str, timeout: float) -> tuple[str, str]:
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "temperature": 0,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise VisionError(f"多模态端点返回结构不可识别：{error}") from error
        text = str(text or "").strip()
        if not text:
            raise VisionError("多模态端点返回了空文本")
        return text, self.model


PROVIDERS: dict[str, type] = {
    "ollama": OllamaVisionProvider,
    "openai": OpenAICompatVisionProvider,
}


# --------------------------------------------------------------------------- #
# 网关
# --------------------------------------------------------------------------- #
class VisionGateway:
    """按配置顺序择一可用供应商做识别，失败即如实报错（不静默降级为空结果）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        order = settings.vision_provider_order_list
        self._providers: list[VisionProvider] = [
            PROVIDERS[name](settings) for name in order if name in PROVIDERS
        ]
        if not self._providers:                    # 配了不认识的供应商，别装没事
            raise VisionUnavailableError(
                f"VISION_PROVIDER_ORDER 中没有已知供应商：{order}；"
                f"可选：{', '.join(PROVIDERS)}")

    def availability(self) -> list[dict[str, str | None]]:
        """各供应商的可用性。**给运维看的真话，不是装饰。**"""
        return [{"provider": p.name, "reason": p.unavailable_reason()} for p in self._providers]

    async def understand(
        self, image_bytes: bytes, *, prompt: str | None = None,
        want_card: bool = True, card_title: str = "图文识别结果",
    ) -> VisionResult:
        started = time.perf_counter()
        png, facts = normalize_to_png(image_bytes)

        reasons: list[str] = []
        for provider in self._providers:
            reason = provider.unavailable_reason()
            if reason:
                reasons.append(f"{provider.name}: {reason}")
                continue
            text, model = await provider.recognize(
                png, prompt=prompt or DEFAULT_PROMPT,
                timeout=self.settings.vision_timeout_seconds)
            elapsed = int((time.perf_counter() - started) * 1000)
            card = render_card(card_title, text, facts) if want_card else None
            return VisionResult(text=text, provider=provider.name, model=model,
                                elapsed_ms=elapsed, facts=facts, card_png=card)

        # 一个都没成：把所有原因一次说清，用户才知道下一步做什么
        raise VisionUnavailableError(
            "没有可用的视觉供应商：" + "；".join(reasons))


# --------------------------------------------------------------------------- #
# 图文输出：结果卡片（把模型文本 + 客观事实排成一张图）
# --------------------------------------------------------------------------- #
_CJK_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",     # 黑体
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _load_font(size: int) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
    """返回 (字体, 是否支持中日韩)。**不支持的实话要说出来**，不能装作排版正常。"""
    for path in _CJK_FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size), True
            except Exception:                      # noqa: BLE001
                continue
    return ImageFont.load_default(), False


def _wrap(text: str, font, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    """按像素宽度折行。中文没有空格，只能逐字试排。"""
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        current = ""
        for char in raw_line:
            if draw.textlength(current + char, font=font) <= max_width:
                current += char
            else:
                lines.append(current)
                current = char
        lines.append(current)
    return lines


def render_card(title: str, text: str, facts: dict[str, Any],
                *, width: int = 900) -> bytes:
    """把识别结果渲染成一张 PNG 卡片 —— 这就是「图文输出」的图那一半。"""
    body_font, has_cjk = _load_font(24)
    title_font, _ = _load_font(32)
    small_font, _ = _load_font(18)

    probe = Image.new("RGB", (width, 10), "white")
    probe_draw = ImageDraw.Draw(probe)
    pad = 32
    body_lines = _wrap(text.strip() or "（无文字）", body_font, probe_draw, width - pad * 2)
    line_height = 36
    fact_lines = [
        f"尺寸 {facts.get('width')}x{facts.get('height')}"
        f"（原图 {facts.get('original_width')}x{facts.get('original_height')}）",
        f"平均色 RGB{facts.get('avg_rgb')} · 亮度 {facts.get('brightness')}",
    ]
    height = pad * 2 + 46 + 20 + len(body_lines) * line_height + 16 + len(fact_lines) * 26

    card = Image.new("RGB", (width, max(height, 240)), "white")
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, width, 6], fill=(0, 128, 128))

    draw.text((pad, pad), title, font=title_font, fill=(20, 20, 20))
    y = pad + 56
    for line in body_lines:
        draw.text((pad, y), line, font=body_font, fill=(35, 35, 35))
        y += line_height

    y += 10
    draw.line([pad, y, width - pad, y], fill=(220, 220, 220), width=1)
    y += 10
    for line in fact_lines:
        draw.text((pad, y), line, font=small_font, fill=(110, 110, 110))
        y += 26

    if not has_cjk:
        # 没有中文字体时不能默默出方块图：直接把这件事写进卡片
        draw.text((pad, y + 4), "(no CJK font on server)", font=small_font,
                  fill=(200, 60, 60))

    buffer = io.BytesIO()
    card.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
