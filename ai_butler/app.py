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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ai_butler.core.config import Settings
from ai_butler.core.log_safety import get_logger, setup_logging
from ai_butler.core.naming import ECOSYSTEM
from ai_butler.core.storage import FileStore
from pathlib import Path
from ai_butler.core.session_store import SessionStore
from ai_butler.middleware.global_middleware import RequestLogMiddleware, TimeoutMiddleware
from ai_butler.core.users import UserError, UserRegistry
from ai_butler.middleware.session_auth import SessionAuthMiddleware, login as issue_session

#: 公开路径白名单：只有基础设施探活，业务接口一律需要合法会话
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    # 登录本身不可能携带会话 —— 计划书三、P7 步骤3「账号密码登录」。
    # 它是唯一允许匿名访问的业务入口；其余业务接口一律需要合法会话（计划书七、11）。
    "/api/session/login",
})


def create_app(settings: Settings, redis) -> FastAPI:
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

    # 内 → 外添加（Starlette 后加的先执行，最终外层为 RequestLog）
    app.add_middleware(
        SessionAuthMiddleware,
        settings=settings,
        store=store,
        public_paths=PUBLIC_PATHS,
    )
    app.add_middleware(TimeoutMiddleware, timeout_seconds=settings.request_timeout_seconds)
    # 必须用 aibutler.http 通道，而不是 setup_logging 返回的根日志器：
    # 否则请求日志会混入根通道，按通道过滤/采集（日志存储）失效。
    app.add_middleware(RequestLogMiddleware, logger=get_logger("http"))

    app.state.session_store = store
    app.state.file_store = files
    app.state.settings = settings
    app.state.users = users
    return app
