"""P0-1 应用装配测试：端到端验证中间件顺序与鉴权闭环。

计划书依据：三、P0-1 会话鉴权中间件 + 全局中间件 + 日志脱敏规则；七、11 未登录拒绝一切业务接口。
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import Request

from ai_butler.app import PUBLIC_PATHS, create_app
from ai_butler.middleware.session_auth import SESSION_EXPIRED_CODE, login


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _prepare(settings, redis_client):
    """装配应用，并把 http 日志器的输出捕获到内存。"""
    app = create_app(settings, redis_client)
    capture = _Capture()
    logging.getLogger("aibutler.http").addHandler(capture)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return app, client, capture


def test_health_is_public_and_business_paths_are_not(settings, redis_client):
    async def main():
        _app, client, _cap = _prepare(settings, redis_client)
        async with client:
            health = await client.get("/health")
            business = await client.get("/api/v1/plan")
        return health, business

    health, business = asyncio.run(main())
    assert health.status_code == 200 and health.json()["status"] == "ok"
    # 公开白名单只允许「拿凭据」与「探活」两类：
    #   /health            —— 基础设施探活
    #   /api/session/login —— 登录本身不可能携带会话（三、P7 步骤3）
    #   /api/session/guest —— 访客凭据（U64）：**签发的只是凭据，不是免检通行证**，
    #                         拿到之后仍然过同一套中间件，能力表里只有 text_query
    # 这条断言的作用是**防止**有人图省事把业务接口加进白名单：一旦加了，下面
    # 的 401 断言会立刻红。
    #   /api/session/{register,forgot,reset} —— U67/U68：这三件事必然发生在
    #       登录之前，不放行就永远拿不到会话；放行的只是动作本身，
    #       重置令牌仍由服务端签发且一次性。
    assert PUBLIC_PATHS == {
        "/health", "/api/session/login", "/api/session/guest",
        "/api/session/register", "/api/session/forgot", "/api/session/reset",
        "/api/accounts/nickname/suggest",
        # U116：常规入口的 Web 页只交出一张 HTML，不含任何数据。
        "/ui", "/ui/",
    }
    # 未登录拒绝一切业务接口（计划书七、11）
    assert business.status_code == 401
    assert business.json()["code"] == SESSION_EXPIRED_CODE


def test_valid_session_reaches_business_and_user_id_is_logged(settings, redis_client):
    """关键断言：RequestLog 必须位于 SessionAuth 之外，才能记录到 user_id。"""

    async def main():
        app, client, capture = _prepare(settings, redis_client)
        store = app.state.session_store
        st = await login(store=store, settings=settings, user_id="u-777", device_id="phone")

        @app.get("/api/v1/whoami")
        async def whoami(request: Request):
            return {"user_id": request.scope["state"]["user_id"]}

        capture.lines.clear()
        async with client:
            r = await client.get("/api/v1/whoami",
                                 headers={"Authorization": f"Bearer {st.token}"})
        return r, capture.lines

    r, lines = asyncio.run(main())
    assert r.status_code == 200 and r.json()["user_id"] == "u-777"
    assert lines, "请求日志未产生"
    assert any("user_id=u-777" in line for line in lines), (
        "请求日志未记录 user_id —— 说明 RequestLogMiddleware 不在 SessionAuthMiddleware 外层"
    )


def test_expired_or_forged_token_is_rejected_before_business(settings, redis_client):
    async def main():
        app, client, _cap = _prepare(settings, redis_client)
        reached = {"n": 0}

        @app.get("/api/v1/touch")
        async def touch():
            reached["n"] += 1
            return {"ok": True}

        async with client:
            forged = await client.get("/api/v1/touch",
                                      headers={"Authorization": "Bearer not-a-jwt"})
        return forged, reached

    forged, reached = asyncio.run(main())
    assert forged.status_code == 401
    assert reached["n"] == 0, "校验未通过的请求不得进入业务路由"


# --------------------------------------------------------------------------- #
# 常规入口 Web 页（U116）
# --------------------------------------------------------------------------- #
def test_web_ui_is_public_but_its_data_is_not(settings, redis_client):
    """页面本身公开（否则浏览器根本打不开），但它要的数据一律仍需鉴权。

    这是「放行一个动作」与「放行一个数据口子」的分界：
    `/ui` 只交出一张 HTML，页面里的每个业务请求仍然带 Bearer、走同一套会话校验。
    """
    async def main():
        _app, client, _cap = _prepare(settings, redis_client)
        async with client:
            page = await client.get("/ui")
            no_auth = await client.post("/api/chat/stream",
                                        json={"utterance": "杭州天气"})
        return page, no_auth

    page, no_auth = asyncio.run(main())
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    html = page.text
    # 页面必须真的能用：SSE 消费 + 过程面板 + 两种入口都在
    for marker in ("/api/chat/stream", "Authorization", "Bearer",
                   "落过程", "过程", "session/guest", "session/login"):
        assert marker in html or marker == "落过程", marker
    assert "api/chat/stream" in html, "页面要消费既有的 SSE 端点，不自造第二套契约"
    # 启动自检：页面必须先探 /health 才能报出「后端没连上」。
    # 少了这一步，静态打开（IDE 预览/文件直开）时只会回一句 HTTP 404，
    # 排错方向全错 —— 实测遇到过（见 MEMORY E-110）。
    assert 'fetch("/health"' in html, "页面必须自检后端是否可达"
    assert "接口不存在（404）" in html, "404 无 message 时要说清是服务没连上"
    assert no_auth.status_code == 401, "页面公开不等于数据公开"


def test_web_ui_page_exists_on_disk():
    """路由读的是磁盘文件，文件被删/改名就是 500 —— 这里提前抓住。"""
    from ai_butler.app import UI_INDEX_PATH

    assert UI_INDEX_PATH.is_file(), UI_INDEX_PATH
    assert UI_INDEX_PATH.read_text(encoding="utf-8").lstrip().startswith("<!DOCTYPE html>")
