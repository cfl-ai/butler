"""P0-1 全局中间件（ASGI 层）：接口超时 + 请求日志脱敏。

计划书依据
----------
- 三、P0-1「开发全局中间件：接口熔断、超时、降级、防雪崩机制」
  （熔断 / 降级 / 防雪崩内核见 ai_butler/core/resilience.py，本文件负责 ASGI 层的超时与观测）
- 三、P0-1「日志脱敏规则：日志禁止打印明文手机号、密码、银行卡、完整位置；
  保留 user_id 用于排障，剥离原始隐私内容」
- 三、P0-7「对接云端流式接口：逐字文字渲染 + 同步 TTS 播报」
  → 超时**不得切断流式响应**，故采用「首字节截止」语义

装配顺序（外 → 内）
------------------
RequestLogMiddleware → TimeoutMiddleware → SessionAuthMiddleware → 业务
RequestLog 必须在最外层：内层 SessionAuth 会把 user_id 写进 scope["state"]，
只有外层才能在其之后读到并记录（计划书要求保留 user_id 用于排障）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_butler.core.log_safety import get_logger, redact_text

REQUEST_TIMEOUT_CODE = "REQUEST_TIMEOUT"
REQUEST_TIMEOUT_MESSAGE = "请求超时，请稍后重试"


class TimeoutMiddleware:
    """接口超时。语义为「首字节截止」：

    - 响应尚未开始时超时 → 取消请求并返回 504；
    - 响应已开始（http.response.start 已发出）→ 解除截止，让流式转发自然结束。
      否则逐字文字渲染与同步 TTS 播报会被中途掐断（计划书三、P0-7）。
    """

    def __init__(self, app: ASGIApp, *, timeout_seconds: float) -> None:
        self.app = app
        self.timeout = float(timeout_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = asyncio.Event()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                started.set()
            await send(message)

        task = asyncio.create_task(self.app(scope, receive, send_wrapper))
        done, _pending = await asyncio.wait({task}, timeout=self.timeout)

        if task in done:
            await task                      # 正常结束；异常按原样抛出
            return

        if started.is_set():
            await task                      # 流式响应已开始 → 解除截止
            return

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):   # noqa: BLE001 — 取消即可
            pass
        response = JSONResponse(
            {"code": REQUEST_TIMEOUT_CODE, "message": REQUEST_TIMEOUT_MESSAGE}, status_code=504
        )
        await response(scope, receive, send)


class RequestLogMiddleware:
    """请求日志 + 脱敏。

    **只记录元数据**：方法、路径、状态码、耗时、user_id、客户端标识。
    不记录请求体、不记录查询串——两者都可能携带口令或令牌；
    路径本身再过一遍模式级脱敏，兜住「手机号出现在 URL 里」这类场景。
    """

    def __init__(self, app: ASGIApp, *, logger: Any = None, slow_ms: int = 2000) -> None:
        self.app = app
        self.logger = logger or get_logger("http")
        self.slow_ms = int(slow_ms)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        t0 = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            state = scope.get("state") or {}
            record = {
                "method": scope.get("method"),
                "path": redact_text(scope.get("path", "")),
                "status": status_holder.get("status"),
                "duration_ms": duration_ms,
                # 计划书：保留 user_id 用于排障
                "user_id": state.get("user_id"),
                "client": (scope.get("client") or ("unknown",))[0],
            }
            level = self.logger.warning if duration_ms >= self.slow_ms else self.logger.info
            # 单条日志同时携带可读文本与结构化字段，便于采集侧直接解析
            level("request method=%s path=%s status=%s duration_ms=%d user_id=%s",
                  record["method"], record["path"], record["status"],
                  record["duration_ms"], record["user_id"],
                  extra={"aibutler": record})
