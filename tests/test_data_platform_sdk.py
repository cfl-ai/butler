"""P1 全局统一数据 SDK 测试。

计划书依据：三、P1「所有 Agent 只读，禁止直写库」+ 表注「入参强制传入 user_id；
查询自动拼接行级过滤；公共知识库不受 user_id 限制；SDK 同时封装 Agent-Wiki 读写 API」
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from ai_butler.agent_wiki.ontology import CLAIM, ENTITY, new_page
from ai_butler.agent_wiki.workspace import WikiStore
from ai_butler.core.db import Base, Database
from ai_butler.data_platform.models import (
    DeviceStateRecord,
    KnowledgeGraphRecord,
    MaterialSourceRecord,
    UserProfileRecord,
)
from ai_butler.data_platform.sdk import (
    ALLOWED_METHODS,
    DB_READ_METHODS,
    WIKI_METHODS,
    DataAccessError,
    DataSDK,
)


@pytest.fixture(scope="module")
def loop():
    """模块级事件循环。

    必须 set_event_loop：同进程内其他用例调用 asyncio.run() 后会把当前事件循环置空，
    若这里只 new_event_loop() 而不设置，asyncpg 在某些代码路径上会去取「当前」循环，
    产生只在整包连跑时才冒头的偶发失败（Event loop is closed / 连接挂在别的循环上）。
    """
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()
    asyncio.set_event_loop(None)


@pytest.fixture(scope="module")
def seeded(settings, loop):
    database = Database(settings)

    async def setup():
        # 与其它测试模块共用 test 库，这里重建四张表以获得确定性的初始数据
        async with database.write_engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.drop_all(c, tables=[
                UserProfileRecord.__table__, KnowledgeGraphRecord.__table__,
                MaterialSourceRecord.__table__, DeviceStateRecord.__table__,
            ]))
            await conn.run_sync(Base.metadata.create_all)
        async with database.session_scope(user_id="user-A") as s:
            s.add(UserProfileRecord(user_id="user-A", attribute="偏好出行", value="高铁",
                                    category="travel", confidence=0.9))
            s.add(DeviceStateRecord(user_id="user-A", device_id="dev-1", metric="battery",
                                    value="80", confidence=0.99))
            s.add(KnowledgeGraphRecord(user_id="user-A", subject="我的偏好", predicate="是",
                                      object="高铁", statement="本人偏好高铁", confidence=0.8))
        async with database.session_scope(user_id="user-B") as s:
            s.add(UserProfileRecord(user_id="user-B", attribute="偏好出行", value="自驾",
                                    category="travel", confidence=0.9))
        async with database.session_scope(user_id=None) as s:
            s.add(KnowledgeGraphRecord(user_id=None, subject="西湖", predicate="位于",
                                      object="杭州", statement="西湖位于杭州", confidence=0.95))
            s.add(MaterialSourceRecord(source_key="sdk-src-1", url="https://e.com/s",
                                       title="公开资讯"))

    loop.run_until_complete(setup())
    yield database
    loop.run_until_complete(database.dispose())


@pytest.fixture()
def sdk(settings, workdir, seeded):
    return DataSDK(database=seeded, wiki_store=WikiStore(workdir / "wiki", settings=settings))


def _run(loop, coro):
    return loop.run_until_complete(coro)


# ------------------------------ 契约 ------------------------------ #
def test_sdk_surface_is_exactly_the_plan_contract():
    """SDK 只能有「数据库只读」与「Wiki 读写」两类方法，不得出现写库入口。"""
    public = {n for n in dir(DataSDK) if not n.startswith("_") and callable(getattr(DataSDK, n))}
    assert public == set(ALLOWED_METHODS), f"SDK 公开面发生变化：{public}"
    assert not any(n.startswith(("write_", "insert", "update", "delete", "save"))
                   for n in DB_READ_METHODS)
    assert not any(n.startswith("read_") for n in WIKI_METHODS)


def test_sdk_never_exposes_the_raw_session_or_engine():
    """所有数据库访问必须经 SDK，业务不得自行开 session。"""
    public = {n for n in dir(DataSDK) if not n.startswith("_")}
    assert not ({"session_scope", "db", "engine", "session", "session_factory"} & public)


def test_private_reads_require_user_id(loop, sdk):
    """计划书表注：SDK 入参强制传入 user_id。

    两层强制：参数本身是必填关键字参数（漏传直接 TypeError），
    传空值则由 SDK 抛出带计划书出处的 DataAccessError。
    """
    for method in (sdk.read_profile, sdk.read_device_state):
        sig = inspect.signature(method)
        assert sig.parameters["user_id"].default is inspect.Parameter.empty, \
            "user_id 必须是必填参数"
        with pytest.raises(TypeError):
            _run(loop, method())                       # 漏传

    for blank in ("", "   "):
        with pytest.raises(DataAccessError) as ei:
            _run(loop, sdk.read_profile(user_id=blank))
        assert "必须携带 user_id" in str(ei.value)
    with pytest.raises(DataAccessError):
        sdk.wiki_retrieve(user_id="")


def test_profile_and_device_are_row_filtered(loop, sdk):
    """查询自动拼接行级过滤：A 只拿到 A 的。"""
    profile = _run(loop, sdk.read_profile(user_id="user-A"))
    assert [r["attribute"] for r in profile] == ["偏好出行"]
    assert all(r["user_id"] == "user-A" for r in profile)

    device = _run(loop, sdk.read_device_state(user_id="user-A"))
    assert [d["metric"] for d in device] == ["battery"]

    other = _run(loop, sdk.read_profile(user_id="user-B"))
    assert other and all(r["user_id"] == "user-B" for r in other)


def test_knowledge_dual_domain_through_sdk(loop, sdk):
    bound = _run(loop, sdk.read_knowledge(user_id="user-A"))
    assert {r["subject"] for r in bound} == {"西湖", "我的偏好"}
    assert all(r["user_id"] in (None, "user-A") for r in bound)

    unbound = _run(loop, sdk.read_knowledge())
    assert {r["subject"] for r in unbound} == {"西湖"}, "未绑定账号只能拿到公共域"
    assert all(r["user_id"] is None for r in unbound)


def test_material_sources_are_public_and_take_no_user_id(loop, sdk):
    """计划书表注：公共知识库不受 user_id 限制。"""
    sig = inspect.signature(DataSDK.read_material_sources)
    assert "user_id" not in sig.parameters, "公共知识库方法不应接收 user_id"

    rows = _run(loop, sdk.read_material_sources())
    assert "sdk-src-1" in [r["source_key"] for r in rows]


# ------------------------------ Wiki 通道 ------------------------------ #
def test_wiki_is_read_write_through_the_sdk(sdk):
    """计划书 P1 表注「SDK 同时封装 Agent-Wiki 读写 API」+ P4 子 Agent 写私有 Vault。"""
    page = new_page(ENTITY, "sdk-ent-1", title="偏好高铁", user_id="user-A",
                    name="偏好高铁", entity_type="用户偏好")
    sdk.wiki_write(page, user_id="user-A")

    got = sdk.wiki_get("sdk-ent-1", user_id="user-A")
    assert got is not None and got.extra["entity_type"] == "用户偏好"
    assert sdk.wiki_get("sdk-ent-1", user_id="user-B") is None


def test_wiki_retrieve_merges_public_and_own_only(sdk):
    sdk.wiki_write(new_page(CLAIM, "pub-claim", confidence=0.9, claim_type="fact",
                            description="公共事实", subject_id="e:西湖", object_id="e:杭州",
                            evidence_ids=["ev-1"]))
    sdk.wiki_write(new_page(CLAIM, "a-claim", confidence=0.9, claim_type="preference",
                            description="A 的偏好", subject_id="u:A", object_id="s:高铁",
                            evidence_ids=["ev-1"], user_id="user-A"), user_id="user-A")
    sdk.wiki_write(new_page(CLAIM, "b-claim", confidence=0.9, claim_type="preference",
                            description="B 的偏好", subject_id="u:B", object_id="s:自驾",
                            evidence_ids=["ev-1"], user_id="user-B"), user_id="user-B")

    ids = {p.id for p in sdk.wiki_retrieve(user_id="user-A")}
    assert ids == {"pub-claim", "a-claim"}, "不得包含 user-B 的私有页面"


def test_wiki_write_still_enforces_isolation(sdk):
    """SDK 是通道不是后门：隔离规则依旧生效。"""
    from ai_butler.agent_wiki.workspace import VaultIsolationError

    leaky = new_page(CLAIM, "leak", confidence=0.5, claim_type="fact", description="x",
                     subject_id="a", object_id="b", evidence_ids=["ev-1"], user_id="user-A")
    with pytest.raises(VaultIsolationError):
        sdk.wiki_write(leaky)                      # 把带 user_id 的页面写进公共库
