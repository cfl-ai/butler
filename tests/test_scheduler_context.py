"""P3 会话上下文管理测试。

计划书依据：三、P3「会话上下文管理：多轮对话状态保持、上下文窗口控制、摘要压缩」
"""

from __future__ import annotations

import pytest

from ai_butler.core.storage import FileStore
from ai_butler.scheduler.context import (
    ROLE_ASSISTANT,
    ROLE_USER,
    ContextError,
    ContextManager,
    ContextPolicy,
    ConversationContext,
    ExtractiveSummarizer,
    Turn,
)


@pytest.fixture()
def manager(settings, workdir):
    return ContextManager(FileStore(workdir / "storage", settings=settings),
                          policy=ContextPolicy(max_turns=6, max_chars=300,
                                               keep_recent_turns=2,
                                               summary_max_chars=200))


# ------------------------------ 策略校验 ------------------------------ #
def test_policy_validates_bounds():
    with pytest.raises(ContextError):
        ContextPolicy(max_turns=0)
    with pytest.raises(ContextError):
        ContextPolicy(max_chars=0)
    with pytest.raises(ContextError):
        ContextPolicy(max_turns=5, keep_recent_turns=9)


def test_turn_rejects_unknown_role():
    with pytest.raises(ContextError):
        Turn(role="root", content="x")


def test_context_requires_user_id():
    with pytest.raises(ContextError):
        ConversationContext(user_id="", session_id="s")


# ------------------------------ 多轮保持 ------------------------------ #
def test_append_and_load_roundtrip(manager):
    manager.append("user-A", "s1", role=ROLE_USER, content="帮我订酒店")
    manager.append("user-A", "s1", role=ROLE_ASSISTANT, content="好的，请问城市？")
    ctx = manager.load("user-A", "s1")
    assert [t.role for t in ctx.turns] == [ROLE_USER, ROLE_ASSISTANT]
    assert ctx.turns[0].content == "帮我订酒店"
    assert ctx.user_id == "user-A" and ctx.session_id == "s1"


def test_context_is_isolated_per_account(manager):
    """计划书三、P4：禁止读取其他 user_id 数据。"""
    manager.append("user-A", "s1", role=ROLE_USER, content="A 的私密行程")
    manager.append("user-B", "s1", role=ROLE_USER, content="B 的私密行程")

    assert manager.load("user-A", "s1").turns[0].content == "A 的私密行程"
    assert manager.load("user-B", "s1").turns[0].content == "B 的私密行程"
    assert manager.load("user-C", "s1").turns == []


def test_loading_without_identity_is_refused(manager):
    with pytest.raises(ContextError):
        manager.load("", "s1")
    with pytest.raises(ContextError):
        manager.load("user-A", "")


def test_sessions_are_separated(manager):
    manager.append("user-A", "s1", role=ROLE_USER, content="会话一")
    manager.append("user-A", "s2", role=ROLE_USER, content="会话二")
    assert manager.load("user-A", "s1").turns[0].content == "会话一"
    assert manager.load("user-A", "s2").turns[0].content == "会话二"


# ------------------------------ 窗口控制 ------------------------------ #
def test_window_trims_by_turn_count(manager):
    for i in range(10):
        manager.append("user-A", "s1", role=ROLE_USER, content=f"第{i}轮")
    ctx = manager.load("user-A", "s1")
    window = ctx.window(manager.policy)
    assert len(window) <= manager.policy.max_turns
    assert window[-1].content.endswith("第9轮"), "保留的应是最新的轮次"


def test_window_trims_by_char_budget():
    policy = ContextPolicy(max_turns=50, max_chars=60, keep_recent_turns=2,
                           summary_max_chars=40)
    ctx = ConversationContext(user_id="user-A", session_id="s1")
    ctx.turns = [Turn(role=ROLE_USER, content="长" * 30) for _ in range(5)]
    window = ctx.window(policy)
    assert sum(t.length for t in window) <= policy.max_chars


# ------------------------------ 摘要压缩 ------------------------------ #
def test_compaction_moves_old_turns_into_summary(manager):
    for i in range(8):
        manager.append("user-A", "s1", role=ROLE_USER, content=f"第{i}轮的内容")
    ctx = manager.load("user-A", "s1")
    assert ctx.summary, "压缩后必须产生摘要"
    # keep_recent_turns 是**压缩当时**保留的轮数；之后继续追加仍会增长，直到再次触发压缩
    assert len(ctx.turns) <= manager.policy.max_turns
    assert "第7轮" in "".join(t.content for t in ctx.turns), "最近轮次原样保留"
    assert "第0轮" in ctx.summary, "被压缩的旧轮次内容应进入摘要"


def test_needs_compaction_detects_both_triggers(manager):
    ctx = ConversationContext(user_id="u", session_id="s")
    assert manager.needs_compaction(ctx) is False
    ctx.turns = [Turn(role=ROLE_USER, content="x") for _ in range(10)]
    assert manager.needs_compaction(ctx) is True


def test_summary_is_bounded(manager):
    for i in range(40):
        manager.append("user-A", "s1", role=ROLE_USER, content=f"第{i}轮" + "内容" * 20)
    ctx = manager.load("user-A", "s1")
    assert len(ctx.summary) <= manager.policy.summary_max_chars


def test_extractive_summarizer_does_not_invent_content():
    turns = [Turn(role=ROLE_USER, content="帮我订杭州的酒店")]
    text = ExtractiveSummarizer().summarize(turns, max_chars=100)
    assert "帮我订杭州的酒店" in text
    assert text.startswith("user:")
    assert ExtractiveSummarizer().summarize([], max_chars=100) == ""


def test_render_includes_summary_and_recent_turns(manager):
    for i in range(8):
        manager.append("user-A", "s1", role=ROLE_USER, content=f"第{i}轮")
    rendered = manager.load("user-A", "s1").render(manager.policy)
    assert "[历史摘要]" in rendered
    assert "第7轮" in rendered


def test_compaction_is_idempotent_when_nothing_to_do(manager):
    manager.append("user-A", "s1", role=ROLE_USER, content="很短")
    ctx = manager.load("user-A", "s1")
    before = (len(ctx.turns), ctx.summary)
    manager.compact(ctx)
    assert (len(ctx.turns), ctx.summary) == before
