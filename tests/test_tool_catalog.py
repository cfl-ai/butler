"""真实工具清单（`scheduler/tool_catalog.py`）的守护测试。

守三件事：
1. **清单能落到模型手里**：schema + 能力归属两道闸都过（否则接通层永远空转）；
2. **user_id 由调度器注入，模型伪造无效** —— 这是数据隔离的底线，不是风格问题；
3. **越界入参不炸链路**：空 query 得到失败结果、top_k 被夹到上限、长文截断留痕。

真实 SearchService 的端到端命中由 `scripts/smoke_p1_p4.py` 的第 9 步覆盖
（E-99：mock 看不见端点/参数错误，所以真实路径必须在非测试脚本里也走一遍）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_butler.scheduler.tool_catalog import (
    HIT_TEXT_MAX_CHARS,
    MAX_HITS,
    build_tool_registry,
)
from ai_butler.scheduler.tools import ToolCall, ToolDispatcher, ToolRegistry

_SQL = "西湖景区春季六点半开放，门票可在线预约。"


class StubSearchService:
    """记录实际收到的 user_id / top_k —— 用来看注入与夹取是否生效。"""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int | None]] = []

    def search(self, query: str, *, user_id: str, top_k: int | None = None):
        self.seen.append((query, user_id, top_k))
        hits = [SimpleNamespace(doc_id=f"doc-{i}", text=_SQL * 20, score=0.9 - i / 100,
                                evidence_ids=[f"ev-{i}"]) for i in range(top_k or 5)]
        return SimpleNamespace(query=query, user_id=user_id, hits=hits,
                               searched_scopes=["public", f"vault:{user_id}"])


def _dispatcher(registry: ToolRegistry) -> ToolDispatcher:
    from ai_butler.core.permissions import (
        AgentIdentity, CapabilityLevel, ToolPermission, UserRole)
    d = ToolDispatcher(registry)
    d.bind_permission(ToolPermission(
        agent=AgentIdentity(key="catalog_test", level=CapabilityLevel.L1_WRITE_OWN),
        role=UserRole.MEMBER))
    return d


# --------------------------------------------------------------------------- #
# 1. 清单可见性
# --------------------------------------------------------------------------- #
def test_catalog_tools_are_exposed_with_schema_and_capability():
    registry = build_tool_registry(search_service=StubSearchService(),
                                   wiki_store=None)
    exposed = registry.as_openai_tools()
    assert [t["function"]["name"] for t in exposed] == ["search_materials"]
    assert exposed[0]["function"]["parameters"]["required"] == ["query"]
    spec = registry.get("search_materials")
    assert spec.capability, "没有能力归属的工具不会被下发"


def test_missing_dependency_registers_nothing_instead_of_a_broken_shell():
    """缺依赖就少注册，不是注册一个调用必炸的空壳。"""
    assert build_tool_registry().names() == ()


# --------------------------------------------------------------------------- #
# 2. user_id 注入
# --------------------------------------------------------------------------- #
def test_user_id_is_injected_by_dispatcher_not_supplied_by_model():
    service = StubSearchService()
    dispatcher = _dispatcher(build_tool_registry(search_service=service))
    result = dispatcher.dispatch(
        # 模型若尝试自己塞 user_id，必须被 State 里的值覆盖
        ToolCall(name="search_materials",
                 payload={"query": "西湖", "user_id": "user-EVIL"}),
        user_id="user-A")
    assert result.ok, result.note
    assert service.seen[0][1] == "user-A", "模型传入的 user_id 不得生效"


def test_user_id_missing_is_a_refusal_not_an_anonymous_read():
    service = StubSearchService()
    dispatcher = _dispatcher(build_tool_registry(search_service=service))
    result = dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "西湖"}), user_id="")
    assert result.ok is False
    assert service.seen == [], "没绑定账号就不该真的去检索"


# --------------------------------------------------------------------------- #
# 3. 入参与输出边界
# --------------------------------------------------------------------------- #
def test_empty_query_fails_with_a_result_not_an_exception():
    dispatcher = _dispatcher(build_tool_registry(search_service=StubSearchService()))
    result = dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "   "}), user_id="user-A")
    assert result.ok is False


def test_top_k_is_clamped_to_the_hard_ceiling():
    service = StubSearchService()
    dispatcher = _dispatcher(build_tool_registry(search_service=service))
    dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "西湖", "top_k": 999}),
        user_id="user-A")
    assert service.seen[0][2] == MAX_HITS, "模型要 999 条也不能真给 999 条"


def test_long_hit_text_is_truncated_and_says_so():
    dispatcher = _dispatcher(build_tool_registry(search_service=StubSearchService()))
    result = dispatcher.dispatch(
        ToolCall(name="search_materials", payload={"query": "西湖", "top_k": 1}),
        user_id="user-A")
    text = result.output["hits"][0]["text"]
    assert len(text) <= HIT_TEXT_MAX_CHARS + len("…（已截断）")
    assert text.endswith("…（已截断）")
    assert result.output["truncated_text"] is True


def test_missing_wiki_page_is_a_conclusion_not_an_error():
    """「没找到」要让模型据此换路径，而不是当成故障反复重试同一个 id。"""
    class EmptyWiki:
        def read_page(self, page_id, *, user_id=None):
            return None

    dispatcher = _dispatcher(build_tool_registry(wiki_store=EmptyWiki()))
    result = dispatcher.dispatch(
        ToolCall(name="read_wiki_page", payload={"page_id": "p-missing"}),
        user_id="user-A")
    assert result.ok is True
    assert result.output == {"page_id": "p-missing", "found": False}
