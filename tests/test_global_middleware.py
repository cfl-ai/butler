"""P0-1 全局 ASGI 中间件测试：接口超时 + 请求日志脱敏。"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
from starlette.responses import JSONResponse

from ai_butler.middleware.global_middleware import REQUEST_TIMEOUT_CODE, RequestLogMiddleware, TimeoutMiddleware


async def _fast_app(scope, receive, send):
    await JSONResponse({"ok": True})(scope, receive, send)


async def _slow_app(scope, receive, send):
    await asyncio.sleep(1.0)
    await JSONResponse({"ok": True})(scope, receive, send)


async def _stream_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    for _ in range(3):
        await asyncio.sleep(0.15)
        await send({"type": "http.response.body", "body": "块".encode(), "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_slow_request_is_cut_with_504():
    async def main():
        app = TimeoutMiddleware(_slow_app, timeout_seconds=0.1)
        async with _client(app) as c:
            return await c.get("/slow")

    r = asyncio.run(main())
    assert r.status_code == 504
    assert r.json()["code"] == REQUEST_TIMEOUT_CODE


def test_fast_request_passes_untouched():
    async def main():
        app = TimeoutMiddleware(_fast_app, timeout_seconds=5)
        async with _client(app) as c:
            return await c.get("/fast")

    r = asyncio.run(main())
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_streaming_response_is_not_cut_by_timeout():
    """计划书三、P0-7 要求逐字流式播报 → 首字节之后必须解除截止。"""

    async def main():
        app = TimeoutMiddleware(_stream_app, timeout_seconds=0.1)   # 总时长 0.45s > 0.1s
        async with _client(app) as c:
            return await c.get("/stream")

    r = asyncio.run(main())
    assert r.status_code == 200
    assert r.text == "块块块", "流式响应被超时切断"


def test_request_log_carries_user_id_and_no_plaintext_privacy():
    logger = logging.getLogger("aibutler.test.http")
    logger.handlers.clear()
    logger.propagate = False
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    async def inner_app(scope, receive, send):
        scope.setdefault("state", {})["user_id"] = "u-42"     # 内层鉴权中间件写入
        await JSONResponse({"ok": True})(scope, receive, send)

    async def main():
        app = RequestLogMiddleware(inner_app, logger=logger)
        async with _client(app) as c:
            return await c.get("/v1/users/13800001111/profile?token=should-not-be-logged")

    r = asyncio.run(main())
    assert r.status_code == 200
    line = captured[-1]
    assert "u-42" in line, "user_id 必须保留用于排障"
    assert "13800001111" not in line, "URL 中的手机号必须脱敏"
    assert "should-not-be-logged" not in line, "查询串不得入日志"


def test_slow_request_is_logged_at_warning():
    logger = logging.getLogger("aibutler.test.http.slow")
    logger.handlers.clear()
    logger.propagate = False
    levels: list[int] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            levels.append(record.levelno)

    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    async def main():
        app = RequestLogMiddleware(_slow_app, logger=logger, slow_ms=50)
        async with _client(app) as c:
            return await c.get("/slow")

    asyncio.run(main())
    assert logging.WARNING in levels
