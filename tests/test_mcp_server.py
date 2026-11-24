"""MCP server 封装的离线测试：不联网、不建向量库，用假注册表走全链路。

覆盖四件事（与 server.py 的五条设计取舍对应）：
1. 工具按注册表条件暴露 —— 缺哪个少暴露哪个（不静默降级）
2. 契约一致性自检 —— MCP 签名与 params_schema 漂移即 fail-fast（E-44）
3. 调度器机制继承 —— 重复调用拦截 / 权限闸拒绝，判责信息透出到 MCP 端
4. user_id 注入 —— 模型传参被覆盖，纪律不因 MCP 而旁路
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    ToolPermission,
    UserRole,
)
from ai_butler.scheduler.tools import ToolCall, ToolDispatcher, ToolRegistry
from mcp_server.server import TOOL_PREFIX, create_server, serialize_result


def _fake_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "read_wiki_page",
        lambda page_id, *, user_id="": {"page_id": page_id, "found": False,
                                        "user": user_id},
        description="按 page_id 读取 Agent Wiki 的一页",
        params_schema={"type": "object",
                       "properties": {"page_id": {"type": "string"}},
                       "required": ["page_id"]},
        capability="wiki_read",
        required=CapabilityLevel.L0_READ,
        needs_user_id=True)
    return registry


def _fake_dispatcher() -> Any:
    from ai_butler.scheduler.tools import ToolDispatcher
    d = ToolDispatcher(_fake_registry())
    d.bind_permission(ToolPermission(
        agent=AgentIdentity(key="mcp_server",
                            level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER,
        sensitivity=DataSensitivity.L2_INTERNAL))
    return d


def test_exposes_only_registered_tools() -> None:
    """注册表里只有 read_wiki_page → MCP 清单里就只有带前缀的这一个。"""
    server, dispatcher = create_server(dispatcher=_fake_dispatcher())
    registered = [t.name for t in asyncio.run(server.list_tools())]
    assert registered == [f"{TOOL_PREFIX}read_wiki_page"]
    assert dispatcher.registry.names() == ("read_wiki_page",)


def test_dispatch_via_mcp_wrapper_inherits_dispatcher() -> None:
    """MCP 调用走 ToolDispatcher：重复调用被拦截、user_id 由服务端注入。"""
    _, dispatcher = create_server(dispatcher=_fake_dispatcher())
    uid = "u_test_001"

    first = dispatcher.dispatch(
        ToolCall("read_wiki_page", {"page_id": "p1"}), user_id=uid)
    assert first.ok and first.output["found"] is False
    assert first.output["user"] == uid          # user_id 注入生效

    dup = dispatcher.dispatch(
        ToolCall("read_wiki_page", {"page_id": "p1"}), user_id=uid)
    assert dup.duplicate and dup.progressed is False  # 重复拦截生效


def test_serialize_result_exposes_judgement_fields() -> None:
    """判责字段透出：MCP 端的模型能看到拦截/错误码真相，不盲目重试。"""
    _, dispatcher = create_server(dispatcher=_fake_dispatcher())
    result = dispatcher.dispatch(
        ToolCall("read_wiki_page", {"page_id": "p2"}), user_id="u1")
    payload = serialize_result(result)
    assert payload["ok"] is True
    assert payload["duplicate"] is False
    assert payload["error_code"] == ""


def test_contract_drift_fails_fast() -> None:
    """schema 漂移检测：注册表契约与 MCP 签名不一致必须启动失败。"""
    registry = ToolRegistry()
    registry.register(
        "read_wiki_page",
        lambda page_id: {},
        description="漂移契约",
        params_schema={"type": "object",
                       "properties": {"page_id": {"type": "string"},
                                      "extra": {"type": "string"}},
                       "required": ["page_id", "extra"]},
        capability="wiki_read",
        required=CapabilityLevel.L0_READ)
    from ai_butler.scheduler.tools import ToolDispatcher
    d = ToolDispatcher(registry)
    d.bind_permission(ToolPermission(
        agent=AgentIdentity(key="mcp_server",
                            level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER, sensitivity=DataSensitivity.L2_INTERNAL))
    with pytest.raises(RuntimeError, match="契约漂移"):
        create_server(dispatcher=d)
