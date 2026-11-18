"""AiButler MCP Server —— 把 butler 工具调度层挂到 MCP 标准协议上。

设计取舍（继承 tool_catalog.py / tools.py 的既有纪律，不另起炉灶）
----------------------------------------------------------------
1. **不重写工具**：复用 `build_tool_registry` 唯一装配点，工具清单永远以
   butler 注册表为真值源；缺依赖的工具（如无高德 key 的 search_poi）
   butler 不注册，MCP 清单里就没有 —— 不静默降级。
2. **不绕过调度器**：每次 MCP 工具调用一律经 `ToolDispatcher.dispatch` ——
   三层权限闸（U43-45）、重复调用拦截、无增量熔断、user_id 注入、
   错误码判责（U60）全部自动继承，MCP 层零旁路。
3. **user_id 纪律不变**：needs_user_id 工具的 user_id 依然**不进参数契约**，
   由 MCP 层从环境变量 `BUTLER_USER_ID` 注入（调用方传了也会被覆盖）。
   让模型填 user_id 等于把「读谁的数据」交给概率决定 —— MCP 不是旁口。
4. **契约单一真值源**（E-44 家族）：MCP 端函数签名在启动时与
   `ToolSpec.params_schema` 做一致性自检，不一致 fail-fast ——
   模型看到的 schema 与执行侧的解释必须是同一份。
5. **懒装配**：Wiki 存储首次调用才构建（与 `_resolve_service` 同一立场），
   依赖缺失只影响那一次工具调用，不升级成「服务起不来」。

运行：`python -m mcp_server.server`（stdio 传输，配置进任意 MCP 客户端）。
"""

from __future__ import annotations

import inspect
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from ai_butler.core.config import Settings, load_settings
from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    ToolPermission,
    UserRole,
)
from ai_butler.retrieval.wiring import SearchWiring
from ai_butler.scheduler.tool_catalog import build_tool_registry
from ai_butler.scheduler.tools import ToolCall, ToolDispatcher, ToolResult

SERVER_NAME = "ai-butler-tools"
SERVER_VERSION = "0.1.0"
#: MCP 端工具名前缀：客户端工具列表里一眼可辨来源，也避免与其他 server 撞名
TOOL_PREFIX = "butler_"


class LazyWikiStore:
    """惰性 Wiki 存储代理：首次 read_page 才真正 build_wiki_store。

    启动时强建会把「向量模型未下载」升级成「MCP 服务起不来」；
    懒建后构建失败只影响那一次工具调用，由调度器按错误码记账。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store: Any = None

    def _resolve(self) -> Any:
        if self._store is None:
            from ai_butler.agent_wiki.wiring import build_wiki_store
            self._store = build_wiki_store(self._settings)
        return self._store

    def read_page(self, page_id: str, *, user_id: str = "") -> Any:
        return self._resolve().read_page(page_id, user_id=user_id)


def build_dispatcher(settings: Settings | None = None) -> ToolDispatcher:
    """装配带权限上下文的调度器（MCP 侧唯一入口）。

    权限取 MEMBER + L2 + L2_INTERNAL：本清单只有 L0_READ 工具，
    闸门以 registry 为真值源 —— 身份再高也调不到未注册的写工具。
    """
    settings = settings or load_settings()
    registry = build_tool_registry(
        search_service=SearchWiring(settings).service,
        wiki_store=LazyWikiStore(settings),
        settings=settings,
    )
    dispatcher = ToolDispatcher(registry)
    dispatcher.bind_permission(ToolPermission(
        agent=AgentIdentity(key="mcp_server",
                            level=CapabilityLevel.L2_SIDE_EFFECT),
        role=UserRole.MEMBER,
        sensitivity=DataSensitivity.L2_INTERNAL))
    return dispatcher


def mcp_user_id() -> str:
    """MCP 会话的 user_id。纪律与调度器一致：**不来自模型参数**。

    未设置 `BUTLER_USER_ID` 时落到 `mcp_local`（本地单用户默认档），
    保证 needs_user_id 工具可调用且数据边界明确。
    """
    return os.environ.get("BUTLER_USER_ID", "").strip() or "mcp_local"


def serialize_result(result: ToolResult) -> dict[str, Any]:
    """ToolResult → MCP 端结构。判责信息（note/error_code/duplicate）原样透出，
    让 MCP 客户端的模型也能看到「这次调用被拦截/被拒/超时」的真相。"""
    return {
        "ok": result.ok,
        "output": result.output,
        "note": result.note,
        "duplicate": result.duplicate,
        "error_code": result.error_code,
        "approval_tier": result.approval_tier,
        "approval_id": result.approval_id,
    }


def _check_schema_consistency(wrapper_by_name: dict[str, callable],
                              dispatcher: ToolDispatcher) -> None:
    """启动自检：MCP 函数签名必须与 ToolSpec.params_schema 同源一致。

    规则：① 参数集合一致（user_id 由调度器注入，不在任何一侧的契约里）；
    ② schema.required == 无默认值的 MCP 参数。不一致即抛错（fail-fast）。
    """
    for mcp_name, fn in wrapper_by_name.items():
        spec = dispatcher.registry.get(mcp_name)
        schema = spec.params_schema or {}
        sig = inspect.signature(fn)
        mcp_params = {k: (p.default is not inspect.Parameter.empty)
                      for k, p in sig.parameters.items()}
        schema_params = set((schema.get("properties") or {}).keys())
        schema_required = set(schema.get("required") or [])
        if set(mcp_params) != schema_params:
            raise RuntimeError(
                f"契约漂移（E-44）：{TOOL_PREFIX}{mcp_name} 参数 {sorted(mcp_params)}"
                f" != params_schema {sorted(schema_params)}")
        mcp_required = {k for k, has_default in mcp_params.items() if not has_default}
        if mcp_required != schema_required:
            raise RuntimeError(
                f"契约漂移（E-44）：{TOOL_PREFIX}{mcp_name} 必填项 {sorted(mcp_required)}"
                f" != params_schema.required {sorted(schema_required)}")


def create_server(settings: Settings | None = None,
                  dispatcher: ToolDispatcher | None = None
                  ) -> tuple[MCPServer, ToolDispatcher]:
    """构建 MCP server。`dispatcher` 可注入（离线测试用假注册表）。"""
    settings = settings or load_settings()
    if dispatcher is None:
        dispatcher = build_dispatcher(settings)
    registry = dispatcher.registry
    names = registry.names()

    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "AiButler 工具网关：暴露 AI 管家生态的只读工具"
            "（素材检索 / 知识库读取 / 天气 / POI 检索）。"
            "所有调用经 butler ToolDispatcher：三层权限闸、重复调用拦截、"
            "无增量熔断自动生效；user_id 由服务端注入，模型无需也无法指定。"
            "返回里的 ok=False 与 error_code 是判责结论，请据此调整策略，"
            "不要盲目重试。"),
    )

    wrappers: dict[str, callable] = {}

    if "search_materials" in names:
        desc = registry.get("search_materials").description

        @server.tool(name=f"{TOOL_PREFIX}search_materials", description=desc)
        def butler_search_materials(query: str, top_k: int = 5) -> dict[str, Any]:
            """在素材中心检索权威资料片段（规划/问答前的事实来源）。"""
            return serialize_result(dispatcher.dispatch(
                ToolCall("search_materials", {"query": query, "top_k": top_k}),
                user_id=mcp_user_id()))

        wrappers["search_materials"] = butler_search_materials

    if "read_wiki_page" in names:
        desc = registry.get("read_wiki_page").description

        @server.tool(name=f"{TOOL_PREFIX}read_wiki_page", description=desc)
        def butler_read_wiki_page(page_id: str) -> dict[str, Any]:
            """按 page_id 读取 Agent Wiki 的一页（公共或本人私有）。"""
            return serialize_result(dispatcher.dispatch(
                ToolCall("read_wiki_page", {"page_id": page_id}),
                user_id=mcp_user_id()))

        wrappers["read_wiki_page"] = butler_read_wiki_page

    if "get_weather" in names:
        desc = registry.get("get_weather").description

        @server.tool(name=f"{TOOL_PREFIX}get_weather", description=desc)
        def butler_get_weather(city: str, days: int = 3) -> dict[str, Any]:
            """查某个城市或地点的天气：当前实况以及未来几天预报（Open-Meteo）。"""
            return serialize_result(dispatcher.dispatch(
                ToolCall("get_weather", {"city": city, "days": days})))

        wrappers["get_weather"] = butler_get_weather

    if "search_poi" in names:
        desc = registry.get("search_poi").description

        @server.tool(name=f"{TOOL_PREFIX}search_poi", description=desc)
        def butler_search_poi(keyword: str, city: str = "", limit: int = 5) -> dict[str, Any]:
            """按关键字搜索地点/商户/景区（高德官方 POI 检索，返回地址与坐标）。"""
            return serialize_result(dispatcher.dispatch(
                ToolCall("search_poi", {"keyword": keyword, "city": city,
                                        "limit": limit})))

        wrappers["search_poi"] = butler_search_poi

    _check_schema_consistency(wrappers, dispatcher)
    return server, dispatcher


def main() -> None:
    server, _ = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
