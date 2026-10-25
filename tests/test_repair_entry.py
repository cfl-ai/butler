"""生产装配点的守护测试（U89）。

这里挡的是 E-77 的复发：**类齐全但没人装配**。`LLMCoder`、`ProcessSandbox`、
`FileLedgerStore`、`FileApprovalService`、`JournalTraceSource` 各有实现、
各有测试，但只要没有一处把它们装进闭环，生产里跑到「修」这一步就只会
得到一句「未注入 coder」——每一样都「可用」，链路却从来没通过电。

所以本文件的判据不是「能构造」，而是**每一个端口都接到了真实现上**，
以及接错时的两种失败形态都被堵住：

1. **复评跑的不是生产管道**：builder 必须经 `app.build_chat_orchestrator`
   （唯一装配点），另起一份装配迟早分叉；
2. **授权与角色门被跳过**：装配本身不替代授权——「机器没授权」与
   「角色压不住 L3」都必须先于装配（模型加载约十秒）被拒绝。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Sequence

import pytest

from ai_butler.agents.repair_entry import (
    RepairPaths,
    build_production_repair_loop,
    default_paths,
    ensure_authorized,
    require_operator_role,
)
from ai_butler.agents.repair_loop import REPAIR_USER_ID
from ai_butler.core.approval import FileApprovalService
from ai_butler.core.authority import AuthorityError, AuthorityRegistry
from ai_butler.core.config import load_settings
from ai_butler.core.event_journal import CHAT_JOURNAL_ID, chat_journal_path
from ai_butler.core.permissions import PermissionError, UserRole
from ai_butler.evaluation.producer import StreamTraceProducer
from ai_butler.harness.coder import LLMCoder
from ai_butler.harness.revision import FileLedgerStore
from ai_butler.harness.sandbox import ProcessSandbox
from ai_butler.retrieval.chat import ChatOrchestrator


class StubGateway:
    """形如 LLMGateway 的最小面：chat 可 await，返回带 text 的结果。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Result:
            text = "SKIP: 测试不真调模型"
        return _Result()


@pytest.fixture()
def work_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "ai_butler" / "retrieval").mkdir(parents=True)
    return root


@pytest.fixture()
def settings(tmp_path: Path):
    # 关掉意图路由：装配期不加载 embedding 权重（测试缝，与 app 的开关同语义）
    return load_settings(storage_root=str(tmp_path / "storage"),
                         intent_router_enable=False)


def _paths(settings: Any, work_root: Path) -> RepairPaths:
    return default_paths(settings, root=work_root)


# --------------------------------------------------------------------------- #
# 一、路径推导
# --------------------------------------------------------------------------- #
def test_default_paths_live_under_storage_offline(settings, work_root):
    paths = _paths(settings, work_root)
    assert paths.base == Path(settings.storage_root) / "offline"
    assert paths.ledger.parent == paths.base
    assert paths.trail.parent == paths.base
    assert paths.approvals.parent == paths.base
    assert paths.authority.parent == paths.base
    assert paths.root == work_root


def test_journal_path_is_shared_with_chat_instrumentation(settings, work_root):
    """埋点侧与回放侧必须指向同一个文件——两侧各推一次路径必然分叉。"""
    paths = _paths(settings, work_root)
    assert paths.journal == chat_journal_path(settings)
    assert paths.journal.name == "chat-journal.jsonl"


def test_default_root_is_the_repo(settings):
    """不显式给 root 时取仓库根（本包上两级）。"""
    paths = default_paths(settings)
    assert (paths.root / "ai_butler").is_dir()
    assert paths.root == Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 二、装配：每个端口都接真实现
# --------------------------------------------------------------------------- #
def test_every_port_is_wired_to_a_real_implementation(settings, work_root):
    paths = _paths(settings, work_root)
    gateway = StubGateway()
    loop = build_production_repair_loop(
        settings=settings, gateway=gateway, paths=paths, modules=())

    # ---- 痕迹生产端：真实对话管道（E-77 的疫苗）----
    producer = loop.evaluator.producer
    assert isinstance(producer, StreamTraceProducer)
    assert isinstance(producer.stream_factory.orchestrator, ChatOrchestrator)

    # ---- 修：coder 与沙箱都是真的 ----
    assert isinstance(loop.harness.coder, LLMCoder)
    assert loop.harness.coder.gateway is gateway
    assert isinstance(loop.harness.sandbox, ProcessSandbox)
    assert loop.harness.sandbox.root == str(work_root)
    assert loop.harness.patch_root == str(work_root)

    # ---- 账：三本账都落盘（跨进程撤销 / 编号续接 / 票的存活）----
    assert isinstance(loop.ledger.store, FileLedgerStore)
    assert Path(loop.ledger.store.path) == paths.ledger
    assert loop.ledger.root == work_root
    assert Path(loop.trail.path) == paths.trail
    assert isinstance(loop.approvals, FileApprovalService)
    assert loop.approvals.path == paths.approvals

    # ---- 首轮痕迹来源：与埋点同一文件、同一个 journal_id ----
    assert loop.source is not None
    assert loop.source.journal.journal_id == CHAT_JOURNAL_ID
    assert loop.source.journal.store.path == paths.journal
    assert loop.user_id == REPAIR_USER_ID


def test_coder_model_follows_the_online_expression(settings, work_root):
    """模型选择与在线链路同一个表达式：两处各写一次就会出现
    「评测跑 A 模型、改代码用 B 模型」。"""
    loop = build_production_repair_loop(
        settings=settings, gateway=StubGateway(),
        paths=_paths(settings, work_root), modules=())
    expected = (settings.ollama_model or settings.ollama_vision_model or None)
    assert loop.harness.coder.model == expected


def test_assembly_creates_the_state_root(settings, work_root):
    """装配期就建目录：别等第一次写入（一次会话刚结束）才炸。"""
    paths = _paths(settings, work_root)
    assert not paths.base.exists()
    build_production_repair_loop(
        settings=settings, gateway=StubGateway(), paths=paths, modules=())
    assert paths.base.is_dir()


def test_missing_work_root_fails_at_assembly(settings, tmp_path):
    with pytest.raises(FileNotFoundError) as err:
        build_production_repair_loop(
            settings=settings, gateway=StubGateway(),
            root=tmp_path / "no-such-repo", modules=())
    assert "--root" in str(err.value)


def test_root_and_paths_together_is_a_configuration_error(settings, work_root):
    """两个都描述「状态在哪」：同时给就无法判断补丁应用在哪个目录。"""
    with pytest.raises(ValueError) as err:
        build_production_repair_loop(
            settings=settings, gateway=StubGateway(), root=work_root,
            paths=_paths(settings, work_root), modules=())
    assert "只能给一个" in str(err.value)


def test_gateway_defaults_to_real_llm_gateway(settings, work_root):
    """不注入 gateway 就建真的（生产路径没有测试缝）。"""
    loop = build_production_repair_loop(
        settings=settings, paths=_paths(settings, work_root), modules=())
    from ai_butler.infra.llm_gateway import LLMGateway
    assert isinstance(loop.harness.coder.gateway, LLMGateway)


# --------------------------------------------------------------------------- #
# 三、三道门：角色与授权先于装配
# --------------------------------------------------------------------------- #
def test_operator_role_ceiling_blocks_non_owners():
    """改源码是 L3：成员/管理员压不住，只有 owner 行。"""
    assert require_operator_role(UserRole.OWNER).allowed
    for role in (UserRole.AUDITOR, UserRole.MEMBER, UserRole.ADMIN):
        with pytest.raises(PermissionError):
            require_operator_role(role)


def test_machine_grant_is_required_before_assembly(tmp_path):
    registry = AuthorityRegistry(root=tmp_path / "authority")
    with pytest.raises(AuthorityError) as err:
        ensure_authorized(registry)
    assert "grant" in str(err.value)                  # 拒绝理由能行动
    grant = registry.grant(
        REPAIR_USER_ID, allowed_prefixes=("ai_butler/retrieval/",),
        granted_by="owner", ttl_seconds=3600)
    verdict = ensure_authorized(registry)
    assert verdict.allowed and verdict.grant.subject == REPAIR_USER_ID
    assert grant.status == "active"


def test_grant_expiry_blocks_the_entry(tmp_path):
    registry = AuthorityRegistry(root=tmp_path / "authority")
    registry.grant(REPAIR_USER_ID, allowed_prefixes=("ai_butler/",),
                   granted_by="owner", ttl_seconds=3600)
    registry.revoke(REPAIR_USER_ID, by="owner", reason="收权")
    with pytest.raises(AuthorityError):
        ensure_authorized(registry)
