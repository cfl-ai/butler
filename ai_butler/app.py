"""P0-1 应用装配根 —— 中间件装配顺序的唯一真值源。

计划书依据
----------
- 三、P0-1「会话鉴权中间件：校验会话 Token，校验失败直接拒绝；网关透传解析后的 user_id
  至下游全部服务，下游禁止绕过网关接收外部请求」
- 三、P0-1「开发全局中间件：接口熔断、超时、降级、防雪崩机制」
- 三、P0-1「日志脱敏规则：…保留 user_id 用于排障」
- 三、P0-1「三层网络隔离：外网业务层、内网服务层、沙箱隔离层」——本文件只暴露外网业务层入口

装配顺序（外 → 内）
------------------
    RequestLogMiddleware  →  TimeoutMiddleware  →  SessionAuthMiddleware  →  业务路由

顺序不可颠倒：
- RequestLog 必须在最外层，才能读到内层 SessionAuth 写入 scope["state"]["user_id"]；
- Timeout 在 SessionAuth 之外，「会话校验自身卡住」同样受超时保护；
- SessionAuth 紧贴业务，未通过校验的请求绝不进入业务路由（计划书七、11）。

注意：Starlette 的 add_middleware 是「后加的先执行」，故下方按 **内 → 外** 顺序添加。
本顺序由 tests/test_app.py 端到端断言，不靠注释保证。

关于 /health
-----------
基础设施探活端点，非业务接口；公开路径白名单只有它（计划书要求未登录拒绝一切业务接口）。
API 文档端点按隐私优先原则关闭。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ai_butler.core import access
from ai_butler.core.config import Settings
from ai_butler.core.log_safety import get_logger, setup_logging
from ai_butler.core.naming import ECOSYSTEM
from ai_butler.core.storage import FileStore
from ai_butler.infra.llm_gateway import LLMGateway
from ai_butler.infra.speech import (
    AudioRejected,
    SpeechModelPaths,
    SpeechRuntime,
    SpeechUnavailable,
    VoiceprintStore,
    decode_wav,
)
from ai_butler.infra.vision import (
    VisionError,
    VisionGateway,
    VisionRejectedError,
    VisionUnavailableError,
)
from ai_butler.core.session_store import SessionStore
from ai_butler.middleware.global_middleware import RequestLogMiddleware, TimeoutMiddleware
from ai_butler.core.users import UserError, UserRegistry
from ai_butler.middleware.session_auth import SessionAuthMiddleware, login as issue_session
from ai_butler.retrieval import describe_categories, describe_tools, plan as plan_query
from ai_butler.retrieval.chat import ChatOrchestrator
from ai_butler.retrieval.wiring import build_search_fn

#: 公开路径白名单：只有基础设施探活 + 登录/访客入口。
#: **访客凭据不是白名单免鉴权**：它也走同一套中间件校验（U64），
#: 这里放行的只是「签发凭据」这个动作本身。
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    # 登录本身不可能携带会话 —— 计划书三、P7 步骤3「账号密码登录」。
    "/api/session/login",
    # 访客入口（U64）：未登录也能进文字查询，但拿到的凭据只有 text_query 一项能力
    "/api/session/guest",
})

#: 图文识别的上传上限（base64 字符数），约合 9MB 原始图片。
#: 手机直出照片 3~8MB，留了余量；再大就该在端上先压缩，而不是让云端硬吃。
MAX_BASE64_CHARS: int = 12_000_000


def create_app(settings: Settings, redis, *, chat_gateway=None,
               chat_search=None) -> FastAPI:
    """装配应用。

    `chat_gateway` / `chat_search` 是**测试缝**：文字查询链路的模型与检索
    都可以从外部注入。生产路径不传，走 `LLMGateway` 与 P2 混合检索；
    测试注入假实现，就能在不连 Ollama、不加载 embedding 的前提下验证
    「访客只能文字」「额度用完要提示」「事件契约」这些规则。
    没有这条缝，这些规则只能靠人工点着试，回归不了。
    """
    # 日志存储（三、P0-1「统一文件存储、日志存储」）：同时落 stdout 与统一存储作用域
    files = FileStore(settings.storage_root, settings=settings)
    logger = setup_logging(settings.log_level, log_file=str(files.log_path()))
    store = SessionStore(
        redis,
        ttl_seconds=settings.session_ttl_seconds,
        idle_timeout_seconds=settings.session_idle_timeout_seconds,
    )

    users = UserRegistry(Path(settings.storage_root) / "users.json")

    app = FastAPI(
        title=f"{ECOSYSTEM} — P0 底座",
        docs_url=None,          # 隐私优先：默认关闭 API 文档与 schema 端点
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "ecosystem": ECOSYSTEM})


    @app.post("/api/session/login")
    async def session_login(request: Request) -> JSONResponse:
        """账号密码登录（计划书三、P7 步骤3「账号密码登录」）。

        口令只在此处校验一次，**不写入任何日志或响应**。
        成功返回会话 Token 与有效期，客户端据此发起后续业务请求。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        account = str(body.get("account") or "").strip()
        password = str(body.get("password") or "")
        device_id = str(body.get("device_id") or "").strip()
        if not account or not password or not device_id:
            return JSONResponse({"code": "bad_request",
                                 "message": "account / password / device_id 均必填"},
                                status_code=400)
        if not users.verify(user_id=account, password=password):
            # 账号不存在与口令错误返回同一结果，不泄露账号是否存在
            return JSONResponse({"code": "invalid_credentials", "message": "账号或密码错误"},
                                status_code=401)
        token = await issue_session(store=store, settings=settings,
                                    user_id=account, device_id=device_id)
        return JSONResponse({
            "user_id": account,
            "token": token.token,
            "expires_in_seconds": settings.session_ttl_seconds,
            "idle_timeout_seconds": settings.session_idle_timeout_seconds,
        })

    @app.post("/api/device/report")
    async def device_report(request: Request) -> JSONResponse:
        """设备信息上报（计划书三、P7 步骤7「上报携带会话身份」）。

        **user_id 以会话为准，不采信请求体**：body 里的 user_id 与会话不一致即拒绝，
        防止一方用自己的会话替别人上报。
        """
        authed = str(getattr(request.state, "user_id", "") or "")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        claimed = str(body.get("user_id") or "")
        if authed and claimed and claimed != authed:
            return JSONResponse(
                {"code": "user_mismatch",
                 "message": "请求体 user_id 与会话身份不一致，拒绝上报"},
                status_code=403)
        if not authed:
            return JSONResponse({"code": "unauthorized", "message": "缺少会话身份"},
                                status_code=401)
        # 当前只做身份校验与受理；落库形态见台账 U42（与账号表同属未定义项）。
        return JSONResponse({"status": "accepted", "user_id": authed})

    # ---- 图文识别 / 图文输出（台账 U61/U62；计划书未定义，人工指令追加）----
    # 网关构造失败**不许连坐整个 App**：视觉不可用时其它业务照常跑，
    # 只有这几个接口按月返回 503 并给出可执行原因（例如「先 ollama pull」）。
    try:
        vision: VisionGateway | None = VisionGateway(settings)
        vision_init_error: str | None = None
    except VisionUnavailableError as error:
        vision = None
        vision_init_error = str(error)

    @app.get("/api/vision/availability")
    async def vision_availability(request: Request) -> JSONResponse:
        """视觉供应商可用性自检。给运维与真机联调用，返回的是真话不是装饰。"""
        if not str(getattr(request.state, "user_id", "") or ""):
            return JSONResponse({"code": "unauthorized", "message": "缺少会话身份"},
                                status_code=401)
        if vision is None:
            return JSONResponse({"available": False, "reason": vision_init_error,
                                 "providers": []})
        providers = vision.availability()
        return JSONResponse({
            "available": any(p["reason"] is None for p in providers),
            "providers": providers,
        })

    @app.post("/api/vision/recognize")
    async def vision_recognize(request: Request) -> JSONResponse:
        """图片 → 文字（识别）→ 图文（文字 + 结果卡片）。"""
        authed = str(getattr(request.state, "user_id", "") or "")
        if not authed:
            return JSONResponse({"code": "unauthorized", "message": "缺少会话身份"},
                                status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)

        encoded = str(body.get("image_base64") or "").strip()
        if not encoded:
            return JSONResponse({"code": "bad_request", "message": "image_base64 必填"},
                                status_code=400)
        if len(encoded) > MAX_BASE64_CHARS:
            return JSONResponse(
                {"code": "image_too_large",
                 "message": f"图片过大（base64 {len(encoded)} 字符，上限 {MAX_BASE64_CHARS}）"},
                status_code=413)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception:
            return JSONResponse({"code": "bad_request", "message": "image_base64 不是合法 base64"},
                                status_code=400)

        if vision is None:
            return JSONResponse({"code": "vision_unavailable", "message": vision_init_error},
                                status_code=503)

        prompt = str(body.get("prompt") or "").strip() or None
        want_card = bool(body.get("want_card", True))
        card_title = str(body.get("card_title") or "图文识别结果")
        try:
            result = await vision.understand(raw, prompt=prompt,
                                             want_card=want_card, card_title=card_title)
        except VisionRejectedError as error:
            return JSONResponse({"code": "image_rejected", "message": str(error)},
                                status_code=400)
        except VisionUnavailableError as error:
            return JSONResponse({"code": "vision_unavailable", "message": str(error)},
                                status_code=503)
        except VisionError as error:
            return JSONResponse({"code": "vision_failed", "message": str(error)},
                                status_code=502)

        blocks: list[dict] = [{"type": "text", "text": result.text}]
        if result.card_png:
            blocks.append({
                "type": "image", "mime": "image/png", "purpose": "card",
                "data_base64": base64.b64encode(result.card_png).decode("ascii"),
            })
        return JSONResponse({
            "user_id": authed,
            "text": result.text,
            "provider": result.provider,
            "model": result.model,
            "elapsed_ms": result.elapsed_ms,
            "facts": result.facts,
            "blocks": blocks,
            "notes": result.notes,
        })

    # ---- 访客凭据 + 能力档案（台账 U64）----
    # 「未登录只能文字查询」不是放宽鉴权，而是**另发一种能力受限期短的凭据**：
    # 访客同样过 SessionAuthMiddleware，只是能力表里只有 text_query。
    guest_budget = access.GuestBudget(
        limit=settings.guest_text_query_budget, ttl_seconds=float(
            settings.guest_session_ttl_seconds))

    def _tier(request: Request) -> access.AccessTier:
        return access.AccessTier.from_user_id(
            str(getattr(request.state, "user_id", "") or ""))

    def _require(request: Request, capability: access.Capability) -> access.AccessTier:
        """接口级能力闸。**抛 AccessDenied，由本文件的统一处理转成 403**。"""
        tier = _tier(request)
        access.require(tier, capability)
        return tier

    @app.post("/api/session/guest")
    async def session_guest(request: Request) -> JSONResponse:
        """签发访客凭据。

        **能力受限 + 期限更短 + 有额度**，三件事缺一都不能叫「安全地允许未登录查询」：
        期限防长期占用，额度防匿名刷模型，能力表防越界。
        """
        if not settings.guest_enable:
            return JSONResponse({"code": "guest_disabled",
                                 "message": "访客模式已关闭，请登录后使用"},
                                status_code=403)
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001 —— 访客入口允许空体
            body = {}
        device_id = str((body or {}).get("device_id") or "").strip() or "guest-device"
        guest_id = access.new_guest_id()
        token = await issue_session(
            store=store, settings=settings, user_id=guest_id, device_id=device_id,
            ttl_seconds=settings.guest_session_ttl_seconds)
        return JSONResponse({
            "user_id": guest_id,
            "token": token.token,
            "tier": access.AccessTier.GUEST.value,
            "expires_in_seconds": settings.guest_session_ttl_seconds,
            "capabilities": [c.value for c in access.capabilities_of(access.AccessTier.GUEST)],
            "text_query_budget": settings.guest_text_query_budget,
        })

    @app.get("/api/access/profile")
    @app.post("/api/access/profile")
    async def access_profile(request: Request) -> JSONResponse:
        """当前身份的能力档案。**端侧按它渲染按钮状态，不得自己写一份能力表**。"""
        user_id = str(getattr(request.state, "user_id", "") or "")
        if not user_id:
            return JSONResponse({"code": "unauthorized", "message": "缺少会话身份"},
                                status_code=401)
        tier = access.AccessTier.from_user_id(user_id)
        payload: dict[str, object] = {
            "user_id": user_id,
            "tier": tier.value,
            "capabilities": [c.value for c in access.capabilities_of(tier)],
            "matrix": access.describe_matrix(),
        }
        if tier is access.AccessTier.GUEST:
            payload["text_query_remaining"] = guest_budget.remaining(user_id)
        return JSONResponse(payload)

    # ---- 检索规划（U66）：分类 + 工具选择，可单独调用以核对判据 ----
    @app.post("/api/retrieval/plan")
    async def retrieval_plan_endpoint(request: Request) -> JSONResponse:
        """给一句话，返回「它属于哪一类、该用哪些工具、为什么」。"""
        tier = _require(request, access.Capability.TEXT_QUERY)
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        query = str((body or {}).get("query") or "").strip()
        if not query:
            return JSONResponse({"code": "bad_request", "message": "query 必填"},
                                status_code=400)
        try:
            planned = plan_query(query, tier=tier)
        except Exception as exc:                # noqa: BLE001
            return JSONResponse({"code": "plan_failed", "message": str(exc)},
                                status_code=400)
        return JSONResponse({"plan": planned.as_dict(),
                             "categories": describe_categories(),
                             "tools": describe_tools()})

    # ---- 语音链路（P7；U63/U65）----
    # 运行时构造**不加载模型**（懒加载），所以这里失败只可能是路径/参数问题。
    try:
        speech: SpeechRuntime | None = SpeechRuntime(
            SpeechModelPaths.resolve(settings.speech_models_root),
            max_audio_seconds=settings.speech_max_audio_seconds,
            tts_speaker=settings.speech_tts_speaker,
            tts_speed=settings.speech_tts_speed,
            num_threads=settings.speech_num_threads,
        ) if settings.speech_enable else None
        speech_init_error: str | None = None if settings.speech_enable else "语音链路已关闭"
    except Exception as exc:                    # noqa: BLE001 —— 语音不可用不许连坐整个 App
        speech = None
        speech_init_error = f"{type(exc).__name__}: {exc}"

    voiceprints = VoiceprintStore(
        Path(settings.storage_root) / "voiceprints.json",
        max_samples_per_user=settings.voiceprint_max_samples)

    def _decode_audio(body: dict) -> tuple[list[float], int]:
        encoded = str(body.get("audio_base64") or "").strip()
        if not encoded:
            raise AudioRejected("audio_base64 必填")
        if len(encoded) > MAX_BASE64_CHARS:
            raise AudioRejected(f"音频过大（base64 {len(encoded)} 字符）")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:                # noqa: BLE001
            raise AudioRejected(f"audio_base64 不是合法 base64：{exc}") from exc
        return decode_wav(raw)

    def _speech_or_error() -> SpeechRuntime:
        if speech is None:
            raise SpeechUnavailable("speech", speech_init_error or "语音链路未装配")
        return speech

    @app.get("/api/voice/availability")
    async def voice_availability(request: Request) -> JSONResponse:
        """语音组件自检。返回的是真话：缺哪个文件、跑哪个脚本。"""
        _require(request, access.Capability.VOICE_INPUT)
        if speech is None:
            return JSONResponse({"available": False, "reason": speech_init_error,
                                 "components": []})
        payload = speech.availability()
        payload["available"] = all(payload["ready"].values()) and not payload["init_errors"]
        return JSONResponse(payload)

    @app.post("/api/voice/asr")
    async def voice_asr(request: Request) -> JSONResponse:
        """语音 → 文字。**登录后才可用**（U64：语音能力属成员层级）。"""
        _require(request, access.Capability.VOICE_INPUT)
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            samples, rate = _decode_audio(body or {})
            result = _speech_or_error().recognize(samples, sample_rate=rate)
        except AudioRejected as exc:
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=400)
        except SpeechUnavailable as exc:
            return JSONResponse(exc.as_dict(), status_code=503)
        except Exception as exc:                # noqa: BLE001
            return JSONResponse({"code": "asr_failed", "message": str(exc)},
                                status_code=502)
        return JSONResponse(result.as_dict())

    @app.post("/api/voice/tts")
    async def voice_tts(request: Request) -> JSONResponse:
        """文字 → 语音（WAV base64）。端侧没有本地 TTS 模型时由此兜底（U21）。"""
        _require(request, access.Capability.VOICE_OUTPUT)
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        text = str((body or {}).get("text") or "").strip()
        if not text:
            return JSONResponse({"code": "bad_request", "message": "text 必填"},
                                status_code=400)
        speaker_id = (body or {}).get("speaker_id")
        speed = (body or {}).get("speed")
        try:
            result = _speech_or_error().synthesize(
                text, speaker_id=int(speaker_id) if speaker_id is not None else None,
                speed=float(speed) if speed is not None else None)
        except AudioRejected as exc:
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=400)
        except SpeechUnavailable as exc:
            return JSONResponse(exc.as_dict(), status_code=503)
        except Exception as exc:                # noqa: BLE001
            return JSONResponse({"code": "tts_failed", "message": str(exc)},
                                status_code=502)
        payload = result.as_dict()
        payload["audio_base64"] = base64.b64encode(result.wav).decode("ascii")
        return JSONResponse(payload)

    @app.post("/api/voice/speaker/enroll")
    async def speaker_enroll(request: Request) -> JSONResponse:
        """声纹登记（U63）。**只存 embedding，不存音频原件**。"""
        _require(request, access.Capability.SPEAKERPRINT)
        user_id = str(getattr(request.state, "user_id", "") or "")
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            samples, rate = _decode_audio(body or {})
            embedding = _speech_or_error().embed_speaker(samples, sample_rate=rate)
        except AudioRejected as exc:
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=400)
        except SpeechUnavailable as exc:
            return JSONResponse(exc.as_dict(), status_code=503)
        except Exception as exc:                # noqa: BLE001
            return JSONResponse({"code": "speaker_failed", "message": str(exc)},
                                status_code=502)
        enrolled = voiceprints.enroll(user_id, embedding)
        return JSONResponse({"status": "enrolled", **enrolled,
                             "threshold": settings.voiceprint_threshold})

    @app.post("/api/voice/speaker/verify")
    async def speaker_verify(request: Request) -> JSONResponse:
        """声纹校验（1:1，只比对自己的登记样本）。

        **1:N 只在本账号内做**：跨账号比对会让「谁是谁」变成全库检索，
        一旦阈值偏松就是大规模冒认。跨账号场景不在本阶段范围内。
        """
        _require(request, access.Capability.SPEAKERPRINT)
        user_id = str(getattr(request.state, "user_id", "") or "")
        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        if not voiceprints.has(user_id):
            return JSONResponse(
                {"code": "not_enrolled", "matched": False, "score": 0.0,
                 "message": "尚未登记声纹，请先说三句话完成登记"},
                status_code=409)
        try:
            samples, rate = _decode_audio(body or {})
            embedding = _speech_or_error().embed_speaker(samples, sample_rate=rate)
        except AudioRejected as exc:
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=400)
        except SpeechUnavailable as exc:
            return JSONResponse(exc.as_dict(), status_code=503)
        except Exception as exc:                # noqa: BLE001
            return JSONResponse({"code": "speaker_failed", "message": str(exc)},
                                status_code=502)
        score = voiceprints.score(user_id, embedding)
        verdict = access.judge_voiceprint(score, threshold=settings.voiceprint_threshold,
                                         speaker_id=user_id if score else None)
        payload = verdict.as_dict()
        payload["code"] = "voiceprint_matched" if verdict.matched else "voiceprint_rejected"
        return JSONResponse(payload, status_code=200 if verdict.matched else 403)

    # ---- 文字查询（U64/U66）：访客可用的唯一业务链路 ----
    # 网关与检索都**懒装配**：模型/向量库加载失败只影响本链路，
    # 不连坐登录、设备上报、图文识别。
    gateway: LLMGateway | None = chat_gateway
    if gateway is None:
        try:
            gateway = LLMGateway(settings, redis)
        except Exception as exc:                # noqa: BLE001
            gateway = None
            logger.error("LLM 网关装配失败，文字查询将不可用", exc)

    search = chat_search if chat_search is not None else build_search_fn(settings)
    orchestrator = ChatOrchestrator(
        gateway, search=search,
        top_k=settings.chat_top_k,
        model=settings.ollama_model or settings.ollama_vision_model or None,
        verify_answer=settings.chat_verify_enable)

    @app.post("/api/chat/stream")
    async def chat_stream(request: Request) -> StreamingResponse:
        """文字查询（SSE）。**访客可用，但只走文字**。

        SSE 事件契约（端侧按 `type` 分支，不猜顺序）：
        `plan` → `chunk`* → [`revise`] → `done`；异常走 `error` / `clarify`。
        """
        user_id = str(getattr(request.state, "user_id", "") or "")
        tier = _tier(request)

        async def _events(error: dict | None = None,
                          text: str = "") -> AsyncIterator[bytes]:
            if error is not None:
                yield _sse({"type": "error", "text": text, "data": error})
                return

        def _sse(payload: dict) -> bytes:
            return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

        try:
            body = await request.json()
        except Exception:                       # noqa: BLE001
            return StreamingResponse(
                _events({"code": "bad_request"}, "请求体不是 JSON"),
                media_type="text/event-stream")
        utterance = str((body or {}).get("utterance") or "").strip()
        if not utterance:
            return StreamingResponse(
                _events({"code": "bad_request"}, "utterance 必填"),
                media_type="text/event-stream")

        # 能力闸放在流之前：宁可回一个明确的 error 事件，也不要先开流再断
        try:
            access.require(tier, access.Capability.TEXT_QUERY)
        except access.AccessDenied as exc:
            return StreamingResponse(_events(exc.as_dict(), exc.hint or str(exc)),
                                     media_type="text/event-stream")

        if tier is access.AccessTier.GUEST:
            allowed, remaining = guest_budget.consume(user_id)
            if not allowed:
                return StreamingResponse(
                    _events({"code": "guest_budget_exhausted",
                             "message": "访客问答次数已用完，登录后可不限次数",
                             "remaining": remaining},
                            "访客额度已用完，请登录后继续"),
                    media_type="text/event-stream")

        async def generator() -> AsyncIterator[bytes]:
            # 首字节尽快出去，避免 TimeoutMiddleware 的首字节截止把整条流掐掉
            yield _sse({"type": "accepted", "tier": tier.value})
            async for event in orchestrator.stream(user_id=user_id, query=utterance,
                                                   tier=tier):
                yield _sse(event.as_dict())

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.exception_handler(access.AccessDenied)
    async def _access_denied(request: Request, exc: access.AccessDenied) -> JSONResponse:
        """能力不足统一回 403 + 机器可读 code。

        端侧据此区分两件事：「去登录就能用」与「真的坏了」——
        都用 500 或都用 401 会让客户端只能弹一句含糊的「操作失败」。
        """
        return JSONResponse(exc.as_dict(), status_code=403)

    # 内 → 外添加（Starlette 后加的先执行，最终外层为 RequestLog）
    app.add_middleware(
        SessionAuthMiddleware,
        settings=settings,
        store=store,
        public_paths=PUBLIC_PATHS,
    )
    # 视觉接口单独放宽：识别要走视觉模型，几十秒是预期内耗时，
    # 用通用 30s 会必然 504（端到端实测过一次，见台账 U61）。
    # 语音与文字查询同理：前者要跑 VAD/ASR/TTS 推理，后者首次请求要加载
    # embedding 模型。它们都属于「预期内计算」，不是「接口卡住」。
    # 注意：流式响应本身按「首字节截止」处理，这些值管的是**首字节之前**的等待。
    app.add_middleware(
        TimeoutMiddleware,
        timeout_seconds=settings.request_timeout_seconds,
        path_timeouts={
            "/api/vision/": settings.vision_timeout_seconds + 10,
            "/api/voice/": 120.0,
            "/api/chat/": 90.0,
        },
    )
    # 必须用 aibutler.http 通道，而不是 setup_logging 返回的根日志器：
    # 否则请求日志会混入根通道，按通道过滤/采集（日志存储）失效。
    app.add_middleware(RequestLogMiddleware, logger=get_logger("http"))

    app.state.session_store = store
    app.state.file_store = files
    app.state.settings = settings
    app.state.users = users
    app.state.chat_orchestrator = orchestrator
    app.state.guest_budget = guest_budget
    return app
