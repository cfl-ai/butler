"""P0-1 会话鉴权中间件（纯 ASGI，保持 SSE 流式不被缓冲）。

计划书依据
----------
- 三、P0-1「会话鉴权中间件：校验会话 Token，校验失败直接拒绝；
  网关透传解析后的 user_id 至下游全部服务，下游禁止绕过网关接收外部请求」
- 三、P0-1「会话管理中间件：会话 Token 生成、校验、自动刷新；设置会话有效期，
  长时间无交互会话自动失效；失效后客户端提示重新登录；支持单账号多端登录、强制踢下线」
- 三、P3「开发全局状态监听机制；会话失效直接终止 Graph 流程，返回重新登录」
- 七、验收标准 11「未登录状态下所有业务接口拒绝执行；会话失效后中断 Agent 业务流程，
  客户端提示重新登录」

失败响应统一带机器可读 code，供移动端弹窗提示重新登录（计划书三、P7）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_butler.core.config import Settings
from ai_butler.core.security import (
    SessionToken,
    SessionTokenError,
    create_session_token,
    decode_session_token,
)
from ai_butler.core.session_store import SessionStore

SESSION_EXPIRED_CODE = "SESSION_EXPIRED"
SESSION_EXPIRED_MESSAGE = "会话已失效，请重新登录"
REFRESHED_TOKEN_HEADER = "x-refreshed-session-token"


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    device_id: str
    jti: str


# --------------------------------------------------------------------------- #
# 会话生命周期
# --------------------------------------------------------------------------- #
async def login(*, store: SessionStore, settings: Settings, user_id: str,
                device_id: str, now: int | None = None,
                ttl_seconds: int | None = None) -> SessionToken:
    """签发会话凭据。

    `ttl_seconds` 只有**访客凭据**会传（U64：访客期限短于成员）。
    刻意做成可选参数而不是改 `Settings`：成员会话的有效期是计划书写明的，
    不能因为访客需要短期限就把全体成员的期限一起改短。
    """
    st = create_session_token(user_id=user_id, device_id=device_id,
                              secret=settings.session_token_secret,
                              ttl_seconds=ttl_seconds or settings.session_ttl_seconds,
                              now=now)
    await store.register(jti=st.jti, user_id=st.user_id, device_id=st.device_id, now=now)
    return st


async def logout(*, store: SessionStore, jti: str) -> None:
    await store.revoke(jti)


async def force_logout(*, store: SessionStore, user_id: str) -> int:
    """强制踢下线：吊销该账号全部设备会话。"""
    return await store.revoke_all_for_user(user_id)


class SessionAuthMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings, store: SessionStore,
                 public_paths: Iterable[str] = (), public_prefixes: Iterable[str] = ()) -> None:
        self.app = app
        self.settings = settings
        self.store = store
        self.public_paths = set(public_paths)
        self.public_prefixes = tuple(public_prefixes)

    def _is_public(self, path: str) -> bool:
        return path in self.public_paths or path.startswith(self.public_prefixes)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send,
                      http_status: int, code: str, message: str) -> None:
        response = JSONResponse({"code": code, "message": message}, status_code=http_status)
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._is_public(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw = headers.get("authorization", "")
        if not raw.lower().startswith("bearer "):
            await self._reject(scope, receive, send, 401, SESSION_EXPIRED_CODE, SESSION_EXPIRED_MESSAGE)
            return

        try:
            claims = decode_session_token(raw[7:].strip(), secret=self.settings.session_token_secret)
        except SessionTokenError:
            await self._reject(scope, receive, send, 401, SESSION_EXPIRED_CODE, SESSION_EXPIRED_MESSAGE)
            return

        record = await self.store.validate(claims["jti"])
        if record is None:
            await self._reject(scope, receive, send, 401, SESSION_EXPIRED_CODE, SESSION_EXPIRED_MESSAGE)
            return

        # 自动刷新：剩余有效期不足余量时换发新会话凭证
        refreshed: SessionToken | None = None
        remaining = int(claims["exp"]) - int(time.time())
        if remaining < self.settings.session_refresh_ahead_seconds:
            refreshed = await login(store=self.store, settings=self.settings,
                                    user_id=record["user_id"], device_id=record["device_id"])
            await self.store.revoke(claims["jti"])

        # 透传 user_id 至下游全部服务
        scope.setdefault("state", {})
        scope["state"]["auth"] = AuthContext(user_id=record["user_id"],
                                             device_id=record["device_id"], jti=record["jti"])
        scope["state"]["user_id"] = record["user_id"]

        if refreshed is None:
            await self.app(scope, receive, send)
            return

        async def send_with_refresh(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(REFRESHED_TOKEN_HEADER, refreshed.token)
            await send(message)

        await self.app(scope, receive, send_with_refresh)
