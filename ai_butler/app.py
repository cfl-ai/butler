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
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

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
from ai_butler.core.event_journal import (
    CHAT_JOURNAL_ID,
    EventJournal,
    FileJournalStore,
    chat_journal_path,
)
from ai_butler.evaluation.journal_source import write_trace
from ai_butler.evaluation.producer import trace_from_events
from ai_butler.middleware.global_middleware import RequestLogMiddleware, TimeoutMiddleware
from ai_butler.core import accounts as accounts_mod
from ai_butler.core import conversation_index as conv_idx_mod
from ai_butler.core import geo as geo_mod
from ai_butler.core import model_access as model_mod
from ai_butler.core.security import FieldCipher
from ai_butler.core.users import UserError, UserRegistry
from ai_butler.middleware.session_auth import (
    SessionAuthMiddleware,
    force_logout,
    login as issue_session,
)
from ai_butler.retrieval import describe_categories, describe_tools, plan as plan_query
from ai_butler.retrieval.chat import ChatOrchestrator
from ai_butler.retrieval.wiring import build_search_fn
from ai_butler.scheduler.context import ContextManager, ContextPolicy
from ai_butler.scheduler import intent_wiring
from ai_butler.verticals.tour.places import load_places

#: 模块级日志通道：给**装配点函数**用（`create_app` 内部自己拿 `setup_logging`
#: 的实例，那一个是请求期用的；模块级函数没有它，写成 `logger.xxx` 会直接 NameError）。
logger = get_logger("app")

#: 公开路径白名单：只有基础设施探活 + 登录/访客入口。
#: **访客凭据不是白名单免鉴权**：它也走同一套中间件校验（U64），
#: 这里放行的只是「签发凭据」这个动作本身。
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    # 登录本身不可能携带会话 —— 计划书三、P7 步骤3「账号密码登录」。
    "/api/session/login",
    # 访客入口（U64）：未登录也能进文字查询，但拿到的凭据只有 text_query 一项能力
    "/api/session/guest",
    # 注册 / 找回 / 重置（U67/U68）：这三件事**必然发生在登录之前**，
    # 不放行就永远拿不到会话。注意它们放行的只是"动作本身"，
    # 重置令牌仍然是服务端签发、一次性的。
    "/api/session/register",
    "/api/session/forgot",
    "/api/session/reset",
    # 昵称建议（U69）：注册前就要能点"随机一个"，无状态、不落库
    "/api/accounts/nickname/suggest",
    # 常规入口的 Web 页面（U116）。**页面本身不含任何数据**，
    # 放行的只是「把这张 HTML 交出去」这个动作；它内部每一个业务请求
    # 仍然带 Bearer 凭据、仍然走同一套会话校验与能力闸。
    "/ui",
    "/ui/",
})

#: 图文识别的上传上限（base64 字符数），约合 9MB 原始图片。
#: 手机直出照片 3~8MB，留了余量；再大就该在端上先压缩，而不是让云端硬吃。
MAX_BASE64_CHARS: int = 12_000_000

#: Web 对话页（U116）的单文件产物。**读文件而不是把 HTML 嵌进 Python 字符串**：
#: 页面改版不该让后端模块跟着重编译，也不该在 Python 里维护一段转义过的 HTML。
UI_INDEX_PATH: Path = Path(__file__).with_name("web") / "index.html"


def build_chat_context_manager(files, settings: Settings) -> ContextManager:
    """装配多轮上下文（U73）。**唯一装配点**，测试直接断言它。"""
    return ContextManager(files, policy=ContextPolicy(
        max_turns=settings.chat_history_max_turns,
        max_chars=settings.chat_history_max_chars,
        keep_recent_turns=settings.chat_history_keep_turns,
        summary_max_chars=settings.chat_summary_max_chars,
    ))


def build_chat_orchestrator(*, gateway, search, settings: Settings,
                            model_access=None,
                            context_manager: ContextManager | None = None,
                            intent_router=None,
                            wiki_digest=None,
                            tool_answer=None,
                            scene=None,
                            ) -> ChatOrchestrator:
    """文字查询链路的**唯一装配点**（U73 / U74）。

    为什么单独立一个函数：E-77 的教训是「`ContextManager` 有实现、有测试，
    但全项目没有一个地方构造它」—— 测试只能证明类可用，证明不了链路在用。
    把构造收成一个可导入的函数后，「有没有插电」就能被一条断言钉住
    （`test_chat_history.py::test_orchestrator_is_wired_with_context`）。

    注意 `context_manager` 允许为 None：那是**显式**的单轮模式（测试缝），
    不是「忘了接」。生产路径由 `create_app` 无条件传入。
    `intent_router` 同理，但多一条纪律：**开关开着却没装配，直接报错**。
    否则「路由没插上」会退化成一个安静的性能问题（每次都按正则分类）。
    """
    if settings.intent_router_enable and intent_router is None:
        raise ValueError(
            "intent_router_enable=true 但未装配意图路由："
            "这会让五层路由静默退化为纯正则分类（E-77 的形态）。"
            "要么传入 build_intent_router() 的结果，要么显式关掉开关。")
    return ChatOrchestrator(
        gateway, search=search,
        top_k=settings.chat_top_k,
        model=settings.ollama_model or settings.ollama_vision_model or None,
        model_access=model_access,
        verify_answer=settings.chat_verify_enable,
        context_manager=context_manager,
        history_enable=settings.chat_history_enable,
        intent_router=intent_router,
        intent_labels=intent_wiring.INTENT_LABELS,
        # U115：wiki 编译产物注入源（None = 没装，对话会如实说明未注入）
        wiki_digest=wiki_digest,
        # U114：对话入口的 function calling 承接层（None = 未装，走既有链路）
        tool_answer=tool_answer,
        # U111：主状态机入口（None = 未装，垂类链路不存在）
        scene=scene,
    )


def build_entry_wiki_digest(settings: Settings):
    """装配对话链路的 `wiki_digest` 供给器（U115）。**唯一装配点**。

    与 `build_chat_orchestrator` 同样的理由（E-77）：构造收成可导入的函数，
    「有没有插电」才能被一条断言钉住。
    """
    from ai_butler.agent_wiki.wiring import WikiDigestProvider

    try:
        return WikiDigestProvider(settings)
    except Exception as exc:                      # noqa: BLE001 —— 装配失败不连坐聊天
        logger.error("Wiki digest 供给器装配失败：本次对话不会注入已知事实", exc)
        return None


def build_entry_tool_answer(settings: Settings, *, gateway, wiki_store):
    """装配对话入口的 function calling 承接层（U114）。**唯一装配点**。

    **开关关闭时返回 None**：不装配 = 该路径在生产里根本不存在。
    这与「装了但开关关了」不同 —— 后者在排障时会让人以为链路是好的。
    """
    if not settings.function_calling_enable:
        return None
    from ai_butler.retrieval.wiring import SearchWiring
    from ai_butler.scheduler.chat_intake import answer_with_tools
    from ai_butler.scheduler.tool_catalog import build_tool_registry

    # 检索是懒装配的：传工厂而不是现成 service，缺 embedding 权重时
    # 只让「这一次工具调用」失败，不会把整个入口装配打断。
    # `settings` 一并传下去：外部只读工具（天气/POI，U118）要按配置决定是否注册 ——
    # 没有高德 key 就不注册 search_poi，让模型看不见一个必然失败的工具。
    registry = build_tool_registry(
        search_service=SearchWiring(settings).service, wiki_store=wiki_store,
        settings=settings)
    if not registry.names():
        logger.error("工具清单为空：function_calling_enable=true 但没有任何工具"
                     "（检索/知识库都没装配）")
        return None

    async def _tool_answer(*, user_id: str, utterance: str,
                           intent_label: str = "", model_kwargs=None):
        return await answer_with_tools(
            gateway=gateway, registry=registry, user_id=user_id,
            utterance=utterance, intent_label=intent_label, settings=settings,
            model_kwargs=model_kwargs)

    return _tool_answer


def build_entry_scene(settings: Settings):
    """装配主状态机的对话入口（U111）。**唯一装配点**。

    这补的是一个真实的缺口：`SceneScheduler` 之前全项目只在脚本与测试里被构造，
    「L4 命中 → 垂类执行」有实现、有守护、能跑，但**没有入口**（E-86 完整形态）。

    装配的三件事
    ------------
    - 路由：只装 L4 垂类层。阶段链（`DispatchPlan.chains`）在生产里仍由既有对话
      链路承担，这里再建一套等于同一句话说两遍；空 `chains` 让非垂类命中
      直接终止，不会误派给未注册的子 Agent。
    - 垂类：`VerticalDispatcher()`（惰性解析 `build_tour_agent`，命中才付装配成本）。
    - 取点：景点候选走高德官方 POI（无 key 时不装配取点，垂类会如实说取不到点）。

    开关关闭时返回 None：**不装** = 这条链路在生产里不存在，而不是「装了但没开」。
    """
    if not getattr(settings, "scene_scheduler_enable", False):
        return None

    from ai_butler.scheduler.graph import DispatchPlan, SceneScheduler
    from ai_butler.scheduler.router import IntentRouter
    from ai_butler.scheduler.scene_entry import answer_via_scene
    from ai_butler.scheduler.vertical import (
        VerticalDispatchLayer,
        VerticalDispatcher,
        build_vertical_model,
    )

    try:
        scheduler = SceneScheduler(
            router=IntentRouter([VerticalDispatchLayer(build_vertical_model())]),
            agents={}, plan=DispatchPlan(chains={}),
            verticals=VerticalDispatcher())
    except Exception as exc:                      # noqa: BLE001 —— 装配失败不连坐聊天
        logger.error("主状态机装配失败：垂类链路本次不存在", exc)
        return None

    poi_search = _build_poi_searcher(settings) if _amap_ready(settings) else None
    if poi_search is None:
        logger.warning("未配置高德 key：旅游垂类可规划但取不到景点候选，"
                       "行程会如实报「方案生成失败」而不编景点")

    async def _scene(*, user_id: str, session_id: str = "", utterance: str = "",
                     labels=()):
        return await answer_via_scene(
            scheduler=scheduler, user_id=user_id, session_id=session_id,
            utterance=utterance, labels=tuple(labels or ()), poi_search=poi_search)

    return _scene


def _amap_ready(settings: Settings) -> bool:
    """有没有高德 key。**读配置，不试探网络**：试探会把启动拖成几十秒。"""
    return bool((getattr(settings, "amap_key", "") or "").strip())


def _build_poi_searcher(settings: Settings):
    """景点候选检索：高德 `v3/place/text` → `candidate_stops` 行（U111）。

    返回的是 `TourAgent._stops()` 认的行结构，坐标取高德的 `[经度, 纬度]`。
    票价为 0（高德 POI 不提供票价）：**不填一个编出来的价格**，
    合计门票因此只反映真实取到的部分。
    """
    from ai_butler.scheduler.external_tools import build_amap_source, search_poi

    source = build_amap_source(settings)
    if source is None:
        return None

    def _search(city: str, *, limit: int = 6) -> list[dict]:
        result = search_poi("景点", city=city, limit=limit, source=source)
        rows: list[dict] = []
        for index, poi in enumerate(result.get("pois") or (), 1):
            location = poi.get("location") or []
            if len(location) != 2:
                continue                       # 没坐标的点排不进时间轴
            rows.append({
                "stop_id": f"poi-{index}",
                "name": poi.get("name") or f"景点{index}",
                "latitude": float(location[1]),
                "longitude": float(location[0]),
                "dwell_minutes": 90,
                "ticket_price": 0.0,
                "evidence_ids": (f"amap:{poi.get('adcode') or ''}",),
            })
        return rows

    return _search


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

    # ---- 账号 / 模型 / 地点（U67~U71）----
    # 构造失败**不连坐整个 App**（与视觉网关同一条政策）：只有这几个接口返回 503，
    # 并说明缺什么。但「重置通道不安全」是例外 —— 那条必须让启动直接失败（U68）。
    accounts_root = Path(settings.account_store_root or settings.storage_root)
    accounts: accounts_mod.AccountStore | None = None
    model_access: model_mod.ModelAccess | None = None
    accounts_problem = ""
    try:
        field_cipher = FieldCipher(settings.field_encryption_key)
    except ValueError as exc:
        field_cipher = None
        accounts_problem = f"隐私字段加密密钥未配置（FIELD_ENCRYPTION_KEY）：{exc}"
    if field_cipher is not None:
        channel = accounts_mod.build_channel(
            settings.reset_channel,
            environment=settings.environment,
            smtp={
                "host": settings.smtp_host,
                "port": settings.smtp_port,
                "sender": settings.smtp_sender,
                "username": settings.smtp_user,
                "password": settings.smtp_password,
                "use_ssl": settings.smtp_ssl,
            },
        )
        accounts = accounts_mod.AccountStore(
            accounts_root,
            cipher=field_cipher,
            ident_secret=settings.session_token_secret,
            channel=channel,
        )
        model_access = model_mod.ModelAccess(
            accounts_root,
            cipher=field_cipher,
            platform_keys={"DEEPSEEK_API_KEY": settings.deepseek_api_key},
            free_daily_calls=settings.model_free_daily_calls,
            trial_daily_calls=settings.model_trial_daily_calls,
        )

    app = FastAPI(
        title=f"{ECOSYSTEM} — P0 底座",
        docs_url=None,          # 隐私优先：默认关闭 API 文档与 schema 端点
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/ui")
    @app.get("/ui/")
    async def web_ui() -> Any:
        """常规入口的 Web 对话页（U116，dsh 形态：左会话 / 中对话 / 右过程）。

        单文件静态页，无构建链 —— 它只消费 `/api/chat/stream` 的 SSE 事件，
        不引入第二套后端契约。页面在 `PUBLIC_PATHS` 里，但页面里的每个请求
        仍然要 Bearer 凭据（登录或访客），鉴权没有被绕过。
        """
        from fastapi.responses import HTMLResponse

        return HTMLResponse(UI_INDEX_PATH.read_text(encoding="utf-8"))

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
        account = str(body.get("account") or body.get("identifier") or "").strip()
        password = str(body.get("password") or "")
        device_id = str(body.get("device_id") or "").strip()
        kind = str(body.get("kind") or "").strip().lower()
        if not account or not password or not device_id:
            return JSONResponse({"code": "bad_request",
                                 "message": "account / password / device_id 均必填"},
                                status_code=400)

        # 带 kind 的请求走账号体系（U67：手机号/邮箱）；不带的仍走 U42 的
        # storage_root/users.json 旧账号 —— 两条路都保留，避免已注册用户突然登不上。
        # 新增注册一律进账号体系，旧表只减不增。
        if kind in accounts_mod.IDENT_KINDS:
            unavailable = _accounts_or_503()
            if unavailable:
                return unavailable
            try:
                account_row = accounts.login(  # type: ignore[union-attr]
                    kind=kind, identifier=account, password=password)
            except accounts_mod.AccountError as exc:
                # 账号不存在 / 口令错 / 已锁定，都走同一个 401 出口；
                # code 区分是为了让端上能提示「已锁定」，但**文案不透露账号是否存在**
                return JSONResponse({"code": exc.code, "message": exc.message},
                                    status_code=401)
            token = await issue_session(store=store, settings=settings,
                                        user_id=account_row.user_id, device_id=device_id)
            return JSONResponse({
                "user_id": account_row.user_id,
                "token": token.token,
                "expires_in_seconds": settings.session_ttl_seconds,
                "idle_timeout_seconds": settings.session_idle_timeout_seconds,
                "profile": accounts.public_profile(account_row.user_id),  # type: ignore[union-attr]
            })

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

    # ================= 账号与隐私（U67 / U68 / U69）================= #
    def _accounts_or_503() -> JSONResponse | None:
        if accounts is None:
            return JSONResponse(
                {"code": "accounts_unavailable",
                 "message": "账号服务不可用：" + (accounts_problem or "未初始化")},
                status_code=503,
            )
        return None

    def _body(payload: object) -> dict:
        return payload if isinstance(payload, dict) else {}

    async def _read_json(request: Request) -> dict | None:
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _authed(request: Request) -> tuple[str, access.AccessTier]:
        uid = str(getattr(request.state, "user_id", "") or "")
        return uid, access.AccessTier.from_user_id(uid)

    @app.post("/api/session/register")
    async def session_register(request: Request) -> JSONResponse:
        """注册（U67）。手机号或邮箱二选一，注册成功直接签发成员会话。

        注册接口**会**暴露"这个号已被占用" —— 这是必要的：否则用户永远登不上
        也不知道为什么。防刷靠限流，不靠含糊其辞（登录/找回才做不可枚举）。
        """
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        kind = str(body.get("kind") or "").strip().lower()
        identifier = str(body.get("identifier") or body.get("account") or "").strip()
        password = str(body.get("password") or "")
        device_id = str(body.get("device_id") or "").strip()
        if not kind or not identifier or not password or not device_id:
            return JSONResponse({"code": "bad_request",
                                 "message": "kind / identifier / password / device_id 均必填"},
                                status_code=400)
        try:
            account = accounts.register(  # type: ignore[union-attr]
                kind=kind, identifier=identifier, password=password,
                nickname=(str(body["nickname"]) if "nickname" in body else None),
                prefs=[str(x) for x in body.get("prefs", []) if str(x).strip()],
            )
        except accounts_mod.AccountError as exc:
            status = 409 if exc.code == "identifier_taken" else 400
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status)
        token = await issue_session(store=store, settings=settings,
                                    user_id=account.user_id, device_id=device_id)
        return JSONResponse({
            "user_id": account.user_id,
            "token": token.token,
            "expires_in_seconds": settings.session_ttl_seconds,
            "idle_timeout_seconds": settings.session_idle_timeout_seconds,
            "profile": accounts.public_profile(account.user_id),  # type: ignore[union-attr]
        }, status_code=201)

    @app.post("/api/session/forgot")
    async def session_forgot(request: Request) -> JSONResponse:
        """申请重置（U68）。**无论账号是否存在，响应完全一致**。"""
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        kind = str(body.get("kind") or "").strip().lower()
        identifier = str(body.get("identifier") or "").strip()
        if not kind or not identifier:
            return JSONResponse({"code": "bad_request", "message": "kind / identifier 必填"},
                                status_code=400)
        try:
            token, masked = accounts.request_reset(  # type: ignore[union-attr]
                kind=kind, identifier=identifier)
        except accounts_mod.AccountError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        payload: dict[str, object] = {
            "status": "accepted",
            "message": f"如果 {masked} 已注册，重置方式已发送，请在 15 分钟内完成",
        }
        # 开发通道才回显令牌；生产通道拿不到（U68 的护栏在 build_channel 里）
        if token and accounts._channel.dev_token_visible():  # type: ignore[union-attr]
            payload["dev_token"] = token
        return JSONResponse(payload)

    @app.post("/api/session/reset")
    async def session_reset(request: Request) -> JSONResponse:
        """用令牌设置新密码（U68）。成功后**吊销该账号全部会话**。"""
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        token = str(body.get("token") or "")
        new_password = str(body.get("new_password") or "")
        if not token or not new_password:
            return JSONResponse({"code": "bad_request",
                                 "message": "token / new_password 必填"}, status_code=400)
        try:
            account = accounts.reset_password(  # type: ignore[union-attr]
                token=token, new_password=new_password)
        except accounts_mod.AccountError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        revoked = await force_logout(store=store, user_id=account.user_id)
        return JSONResponse({"status": "ok", "user_id": account.user_id,
                             "revoked_sessions": revoked})

    @app.get("/api/accounts/me")
    async def account_me(request: Request) -> JSONResponse:
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        try:
            profile = accounts.public_profile(uid)  # type: ignore[union-attr]
        except accounts_mod.AccountError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=404)
        return JSONResponse(profile)

    @app.post("/api/accounts/nickname")
    async def account_set_nickname(request: Request) -> JSONResponse:
        """自定义昵称（U69）。"""
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            account = accounts.set_nickname(  # type: ignore[union-attr]
                user_id=uid, nickname=str(body.get("nickname") or ""))
        except accounts_mod.AccountError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        return JSONResponse({"nickname": account.nickname})

    @app.post("/api/accounts/nickname/random")
    async def account_random_nickname(request: Request) -> JSONResponse:
        """随机刷新昵称（U69），带频控。"""
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        try:
            account = accounts.refresh_nickname(user_id=uid)  # type: ignore[union-attr]
        except accounts_mod.AccountError as exc:
            status = 429 if exc.code == "too_many_requests" else 400
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status)
        return JSONResponse({"nickname": account.nickname})

    @app.post("/api/accounts/nickname/suggest")
    async def account_nickname_suggest() -> JSONResponse:
        """注册前随机取一个昵称建议。**公开接口**：无状态、不落库、不涉及隐私。

        放在这里而不是端侧各写一份词表：词表只该有**一处**，
        否则端侧词表迟早和服务端的规则（长度、禁用字符）不一致 ——
        然后用户会遇到「界面上能填、提交却报非法」。
        """
        return JSONResponse({"nickname": accounts_mod.random_nickname()})

    @app.post("/api/accounts/prefs")
    async def account_set_prefs(request: Request) -> JSONResponse:
        """改画像标签（U70 ②）。它同时喂两处：默认模型的匹配理由、以及推荐排序。

        没有这个接口时，「按用户画像匹配」只在注册那一刻成立 —— 之后用户
        永远改不了自己的偏好，等于把画像钉死在注册表单上。
        """
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        raw = body.get("prefs")
        if not isinstance(raw, list):
            return JSONResponse({"code": "bad_request",
                                 "message": "prefs 必须是字符串数组"}, status_code=400)
        try:
            account = accounts.set_prefs(  # type: ignore[union-attr]
                user_id=uid, prefs=[str(x) for x in raw if str(x).strip()])
        except accounts_mod.AccountError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        # 顺手把「现在会匹配到哪个模型」回给端侧：用户改完偏好应该立刻看得见效果，
        # 而不是下次提问才发现换了模型。用 preview_default 而**不是** resolve ——
        # resolve 会扣额度，改标签不该消耗问答次数。
        matched = ""
        if model_access is not None:
            try:
                matched = model_access.preview_default(user_id=uid).key
            except model_mod.ModelAccessError:
                matched = ""
        return JSONResponse({"prefs": list(account.prefs), "matched_model": matched})

    @app.post("/api/accounts/password")
    async def account_change_password(request: Request) -> JSONResponse:
        """改密码（需原密码）。成功后吊销全部会话，旧设备立刻失效。"""
        unavailable = _accounts_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            accounts.change_password(  # type: ignore[union-attr]
                user_id=uid,
                old_password=str(body.get("old_password") or ""),
                new_password=str(body.get("new_password") or ""),
            )
        except accounts_mod.AccountError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        revoked = await force_logout(store=store, user_id=uid)
        return JSONResponse({"status": "ok", "revoked_sessions": revoked})

    # ================= 模型接入（U70）================= #
    def _models_or_503() -> JSONResponse | None:
        if model_access is None:
            return JSONResponse(
                {"code": "models_unavailable",
                 "message": "模型接入不可用：" + (accounts_problem or "未初始化")},
                status_code=503,
            )
        return None

    @app.get("/api/models")
    @app.post("/api/models/list")
    async def models_list(request: Request) -> JSONResponse:
        """可选模型 + 当前绑定 + 剩余额度。`api_key` 只以掩码出现。

        同时挂 GET 与 POST：端侧的 `TransportPort` 只有 `post` / `stream`
        （见 ports.dart，为的是让传输层保持最小面），所以给一个 POST 别名，
        不必为了一个列表接口去扩传输层。GET 留着供 curl 排查。
        """
        unavailable = _models_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        binding = model_access.binding(uid)  # type: ignore[union-attr]
        return JSONResponse({
            "models": model_access.available_models(user_id=uid),  # type: ignore[union-attr]
            "binding": binding.public_view() if binding else None,
            "quota": {
                "free_remaining": model_access.remaining(user_id=uid, tier=model_mod.FREE),  # type: ignore[union-attr]
                "trial_remaining": model_access.remaining(user_id=uid, tier=model_mod.TRIAL),  # type: ignore[union-attr]
            },
        })

    @app.post("/api/models/select")
    async def models_select(request: Request) -> JSONResponse:
        unavailable = _models_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            binding = model_access.bind_catalog_model(  # type: ignore[union-attr]
                user_id=uid, model_key=str(body.get("model_key") or ""))
        except model_mod.ModelAccessError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        return JSONResponse({"binding": binding.public_view()})

    @app.post("/api/models/byok")
    async def models_byok(request: Request) -> JSONResponse:
        """接入自带模型（U70）。key 加密落盘，**响应里只有掩码**。"""
        unavailable = _models_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            binding = model_access.bind_byok(  # type: ignore[union-attr]
                user_id=uid,
                base_url=str(body.get("base_url") or ""),
                model=str(body.get("model") or ""),
                api_key=str(body.get("api_key") or ""),
                display_name=str(body.get("display_name") or ""),
            )
        except model_mod.ModelAccessError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        # 注意这里返回的是 public_view：**不含密文、不含明文**
        return JSONResponse({"binding": binding.public_view()}, status_code=201)

    @app.delete("/api/models")
    @app.post("/api/models/remove")
    async def models_unbind(request: Request) -> JSONResponse:
        """解除绑定（顺带删掉 BYOK 的密文）。POST 别名同上。"""
        unavailable = _models_or_503()
        if unavailable:
            return unavailable
        uid, tier = _authed(request)
        access.require(tier, access.Capability.PROFILE_EDIT)
        removed = model_access.unbind(user_id=uid)  # type: ignore[union-attr]
        return JSONResponse({"removed": removed})

    # ================= 定位 / 推荐 / 导航（U71）================= #
    @app.post("/api/location/recommend")
    async def location_recommend(request: Request) -> JSONResponse:
        """按所在地 + 喜好推荐。**坐标先粗化再参与计算**（U71 ①）。

        没有地图 key，所以响应里**不含**任何底图或地址反查结果，
        只给坐标、距离与理由，并由 `map_available=false` 明确告知。
        """
        uid, tier = _authed(request)
        access.require(tier, access.Capability.LOCATION_SHARE)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)

        located_at: tuple[float, float] | None = None
        if body.get("lat") is not None and body.get("lon") is not None:
            try:
                located_at = geo_mod.privacy_grid(
                    body["lat"], body["lon"], decimals=settings.geo_grid_decimals)
            except geo_mod.GeoError as exc:
                return JSONResponse({"code": exc.code, "message": exc.message},
                                    status_code=400)

        try:
            places, source = load_places(accounts_root)
        except geo_mod.GeoError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=503)

        prefs = [str(x) for x in body.get("prefs", []) if str(x).strip()]
        if not prefs and accounts is not None:
            try:
                prefs = list(accounts.get(uid).prefs) if accounts.get(uid) else []  # type: ignore[union-attr]
            except accounts_mod.AccountError:
                prefs = []
        limit = int(body.get("limit") or settings.geo_recommend_limit)
        items = geo_mod.recommend(places, prefs=prefs, located_at=located_at, limit=limit)
        return JSONResponse({
            "located": located_at is not None,
            "located_at_grid": list(located_at) if located_at else None,
            "grid_decimals": settings.geo_grid_decimals,
            "prefs_used": prefs,
            "data_source": source,
            # 没有 key 就说没有 key：界面据此不画底图，而不是画一个假地图
            "map_available": False,
            "map_note": "未配置地图服务 key，仅显示坐标与距离，不提供底图与地址反查",
            "items": [item.as_dict() for item in items],
        })

    @app.post("/api/location/navigation")
    async def location_navigation(request: Request) -> JSONResponse:
        """生成三大导航 App 的跳转链接（U71 ③）。本地拼 URL，不需要 key。"""
        _uid, tier = _authed(request)
        access.require(tier, access.Capability.LOCATION_SHARE)
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"code": "bad_request", "message": "请求体不是 JSON"},
                                status_code=400)
        try:
            links = geo_mod.navigation_links(
                dest_lat=body.get("dest_lat"),
                dest_lon=body.get("dest_lon"),
                dest_name=str(body.get("dest_name") or "目的地"),
                origin_lat=body.get("origin_lat"),
                origin_lon=body.get("origin_lon"),
                source_coord=str(body.get("source_coord") or geo_mod.WGS84),
            )
        except geo_mod.GeoError as exc:
            return JSONResponse({"code": exc.code, "message": exc.message}, status_code=400)
        return JSONResponse({
            "links": [link.as_dict() for link in links],
            # 起点默认交给导航 App 自己取，我方坐标不外传（U71 ③）
            "origin_shared": body.get("origin_lat") is not None,
        })


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
    context_manager = build_chat_context_manager(files, settings)
    # 意图路由（U74）：装配完立刻**后台预热**向量层（冷启动约 9.7s，不能在请求里做）
    intent_router = intent_wiring.build_intent_router(settings, gateway=gateway)
    if intent_router is None and settings.intent_router_enable:
        logger.error("意图路由装配为空，文字查询将退化为纯正则分类", None)

    # ---- Agent-Wiki 生产装配（U115）----
    # 编译流水线此前**只在 smoke 脚本里跑过**：`wiki_compile_auto_run=true` 是空转，
    # 机器缓存与 `wiki_digest` 在生产里从来没生成过。这里补上生产者与消费者。
    wiki_store = None
    try:
        from ai_butler.agent_wiki.wiring import build_wiki_store, compile_on_startup

        wiki_store = build_wiki_store(settings)
        startup_wiki = compile_on_startup(settings, wiki_store)
        if startup_wiki.get("error"):
            logger.error("Wiki 启动编译失败（公共库机器缓存可能过期）：%s",
                         startup_wiki["error"])
        elif startup_wiki.get("skipped"):
            logger.info("Wiki 启动编译跳过：%s", startup_wiki["skipped"])
        else:
            logger.info("Wiki 启动编译完成：页面 %s / 声明 %s / digest %s 字节",
                        startup_wiki.get("page_count"), startup_wiki.get("claim_count"),
                        startup_wiki.get("digest_bytes"))
    except Exception as exc:                    # noqa: BLE001 —— 不连坐聊天
        logger.error("Wiki 存储装配失败：本次启动不提供已编译事实", exc)
    wiki_digest = build_entry_wiki_digest(settings)

    # ---- 对话入口的 function calling（U114）----
    # 开关关闭时返回 None：该路径在生产里**根本不存在**，而不是「装了但没开」。
    tool_answer = build_entry_tool_answer(settings, gateway=gateway, wiki_store=wiki_store)
    # ---- 主状态机入口（U111）----
    # 同样是「关掉就不装」：垂类链路要么有一条真入口，要么整个不存在。
    scene = build_entry_scene(settings)

    orchestrator = build_chat_orchestrator(
        gateway=gateway, search=search, settings=settings,
        model_access=model_access, context_manager=context_manager,
        intent_router=intent_router, wiki_digest=wiki_digest,
        tool_answer=tool_answer, scene=scene)

    # ---- 会话痕迹埋点（U91）：每次运行结束折一条 AgentTrace 写进事件账本 ----
    # 这是 `evaluation/journal_source.py` 声言的「线上链路每次运行结束时（埋点）」
    # 的**生产侧**。没有它，离线闭环的首轮痕迹来源（U84）永远是空的：
    # 闭环会停在 `no_traces`，「评测中台发现错误」只能靠人工喂 traces——
    # 声明有了、能力没有，正是项目铁律要抓的形态。
    #
    # 账本装配失败**不连坐聊天**（与视觉网关同一条政策）：只有痕迹读不到，
    # 回答照常给。失败要log——静默的埋点缺口比没有埋点更难发现。
    try:
        trace_journal: EventJournal | None = EventJournal(
            journal_id=CHAT_JOURNAL_ID,
            store=FileJournalStore(chat_journal_path(settings)))
    except Exception as exc:                    # noqa: BLE001
        trace_journal = None
        logger.error("会话痕迹账本装配失败：离线闭环将读不到在线会话的痕迹", exc)
    if redis is not None:
        conversation_index: conv_idx_mod.ConversationIndex = \
            conv_idx_mod.RedisConversationIndex(
                redis, ttl_seconds=settings.conversation_ttl_seconds)
    else:
        # 没有 Redis 就没有跨进程的归属表。退化为进程内表，并**说出来** ——
        # 静默退化会让多实例部署下出现「历史忽然消失」，且查不到原因。
        logger.warning("Redis 未就绪：会话索引退化为进程内内存表，多实例下历史不可共享")
        conversation_index = conv_idx_mod.InMemoryConversationIndex()

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

        # U73：会话标识**必须由服务端签发并校验归属**。
        # 不信任客户端自报的任何字段 —— 端侧此前把设备号当 session_id 传，
        # 结果是一台设备上所有对话落进同一分片并永久累积。三种情况一律重签：
        # 从没签发过、格式非法、归属不符（后两者对客户端**不作区分**，
        # 避免变成「探测他人会话是否存在」的口子）。
        requested_conversation = str((body or {}).get("conversation_id") or "").strip()
        conversation_id = ""
        conversation_note = ""
        if tier is access.AccessTier.GUEST:
            conversation_note = "访客不建立会话：对话历史不落盘"
        elif not settings.chat_history_enable:
            # 功能关掉时**连索引都不建**：建了索引却不写历史，等于留下
            # 一堆指向空会话的标识，排障时会被误读成「历史丢了」。
            conversation_note = "多轮记忆已关闭：不建立会话，历史不落盘"
        else:
            try:
                resolution = await conv_idx_mod.resolve(
                    request.app.state.conversation_index, user_id=user_id,
                    requested=requested_conversation)
            except Exception as exc:            # noqa: BLE001 —— 索引不可用退化为单轮
                conversation_note = (f"会话索引不可用（{type(exc).__name__}）："
                                     "本轮为单轮问答")
            else:
                conversation_id = resolution.conversation_id
                conversation_note = resolution.note

        def _journal_session_trace(events: list) -> None:
            """流结束：折一条 AgentTrace 写进事件账本（U91）。

            **任何异常只记日志**：埋点是手段，回答是目的。为了留痕把
            用户等着的回答弄坏，或者让一次脱敏断言炸掉整条流，
            都是把手段当成了目的。
            """
            if trace_journal is None or not events:
                return
            try:
                trace = trace_from_events(
                    trace_id=f"chat:{uuid.uuid4().hex}",
                    question=utterance, events=events)
                write_trace(trace_journal, trace, extra={
                    "user_id": user_id,
                    "conversation_id": conversation_id or "",
                    "channel": "chat",
                    "tier": tier.value,
                })
            except Exception as exc:            # noqa: BLE001
                logger.error("会话痕迹落账失败（不影响本轮回答）", exc)

        async def generator() -> AsyncIterator[bytes]:
            # 首字节尽快出去，避免 TimeoutMiddleware 的首字节截止把整条流掐掉
            yield _sse({"type": "accepted", "tier": tier.value,
                        "data": {
                            "conversation_id": conversation_id,
                            # `memory` 表示**这个账号现在有没有多轮记忆**；
                            # 本轮是否真的带上了历史，在 `done.data.history` 里报。
                            # 两个语义分开，否则首轮会把「没历史」误报成「没开店」。
                            "memory": settings.chat_history_enable and bool(conversation_id),
                            "note": conversation_note,
                        }})
            # 埋点（U91）：事件原样转给端侧的同时留一份，流结束后折成痕迹落账。
            collected: list = []
            async for event in orchestrator.stream(
                    user_id=user_id, query=utterance, tier=tier,
                    conversation_id=conversation_id or None):
                collected.append(event)
                yield _sse(event.as_dict())
            _journal_session_trace(collected)

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
    app.state.accounts = accounts
    app.state.model_access = model_access
    app.state.chat_orchestrator = orchestrator
    app.state.chat_context_manager = context_manager
    app.state.intent_router = intent_router
    app.state.conversation_index = conversation_index
    app.state.guest_budget = guest_budget
    return app
