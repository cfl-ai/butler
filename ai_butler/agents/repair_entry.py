"""离线修复闭环的生产装配与本机入口（U89）。

计划书依据
----------
- 三、P10 触发时机「7×24 小时后台离线自动运行，不占用业务算力」
- 三、P10 离线链路「代码迭代 Agent 分析问题、生成优化方案 → Hook + 质检风险校验
  → 沙箱执行代码修改与自测」

补的是哪一段（§54.21 第八节登记的两处接线缺口）
--------------------------------------------
机制全部落地并有守护，但**端到端没接通**：

1. **没有入口**：`app.py` 只暴露 session/accounts/retrieval 端点，没有任何一处
   触发 `RepairLoop`；CLI 也没有。它能跑的全部途径是测试与 `.tmp/` 演示。
2. **coder 无人注入**：`LLMCoder` 只出现在测试里，生产默认 None，
   `HarnessAgent` 按 U78「不给就不改代码」如实停下——行为正确，
   但闭环在生产里永远停在「修」这一步。

本模块把这两处接上，并按用户指令（2026-10-25）划定权威：
**以本地这台机器作为有权改源码**（可通过本台机器修改、转让权限）。
落到实现上是三样东西：

| 缺口 | 接法 |
|---|---|
| 入口 | `scripts/run_repair_loop.py`（本机 CLI，人工触发，一次运行跑一轮闭环） |
| coder | `build_production_repair_loop` 装配真 `LLMGateway → LLMCoder` |
| 权威 | `core/authority.py` 授权簿：机器绑定签名 + 强制限期 + 转让/吊销 |

为什么触发方式是「本机 CLI 人工触发」
----------------------------------
计划书只写到「7×24 后台离线自动运行」，**没有定义调度语义**（U85：只提供
`RepairJob` 调度单元，不起线程、不发明定时器）。在分布式权威就位之前，
把触发权放在一台**有名有姓、有人签字授权**的机器上，比放在一个
「谁都能 POST 一下」的 HTTP 端点上更接近计划书的本意——
后者意味着任何能连上服务的人都能让系统修改它自己的源码。
HTTP 触发不是永远不做，而是要到它有同等强度的身份与授权闸为止
（服务端持有机器凭据属分布式权威，不在本模块能力内，见 U88 的边界）。

三道门（顺序固定，缺一个都不跑）
------------------------------
1. **角色天花板**（U43–U45/U51）：调用方角色压不住 L3 高危动作就拒。
2. **机器授权簿**（U88）：本机授权簿里没有该主体的有效授权就拒。
3. **装配**：coder / sandbox / 账本 / 痕迹来源全部就位；缺任何一样，
   都在装配期或首轮如实停下（U78/U84 的既有纪律），不静默降级。

顺序的理由：1 和 2 问的是「该不该跑」，必须赶在 3（装配期要加载模型，
约十秒）之前问完——先跑十秒再拒绝，浪费的是人的时间。

状态放哪
--------
全部离线链路状态在 `<storage_root>/offline/` 下（`data/` 已入 .gitignore）：

    chat-journal.jsonl    在线会话痕迹账本（与 app.py 埋点同一文件）
    revision-ledger.jsonl 已提交补丁的版本账（撤销靠它）
    repair-trail.jsonl    轮次溯源账（每轮改了什么、为什么）
    approvals.json        人工审批票（撤销已提交补丁要人签字，U59/U81）
    authority/            源码修改授权簿（machine.key + grants.json，U88）

放一起的理由：「哪次会话、改过什么、谁批准的」是一个问题，
拆开存放等于逼着排障的人在四个目录之间对时间戳。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_butler.agents.repair_loop import (
    CHAT_PIPELINE_MODULES,
    DEFAULT_MAX_ROUNDS,
    REPAIR_USER_ID,
    RepairLoop,
    build_repair_loop,
)
from ai_butler.core.approval import FileApprovalService
from ai_butler.core.authority import AuthorityRegistry, GrantVerdict
from ai_butler.core.event_journal import (
    CHAT_JOURNAL_ID,
    EventJournal,
    FileJournalStore,
    chat_journal_path,
)
from ai_butler.core.permissions import (
    AgentIdentity,
    CapabilityLevel,
    DataSensitivity,
    PermissionRequest,
    PermissionVerdict,
    UserRole,
    evaluate,
)
from ai_butler.harness.coder import LLMCoder
from ai_butler.harness.sandbox import ProcessSandbox

__all__ = [
    "RepairPaths",
    "build_production_repair_loop",
    "default_paths",
    "ensure_authorized",
    "require_operator_role",
]


@dataclass(frozen=True)
class RepairPaths:
    """离线链路全部状态的路径。**一处推导，两侧共用**（E-44）。"""

    #: 源码工作根：补丁应用与撤销的目标（= 仓库根）
    root: Path
    #: 状态根：`<storage_root>/offline`
    base: Path
    #: 在线会话痕迹账本（app.py 埋点与闭环回放**同一个文件**）
    journal: Path
    #: 已提交补丁的版本账
    ledger: Path
    #: 轮次溯源账
    trail: Path
    #: 人工审批票
    approvals: Path
    #: 源码修改授权簿目录
    authority: Path

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items()}


def default_paths(settings: Any, *, root: Path | str | None = None) -> RepairPaths:
    """按 settings 推导离线链路的状态路径。

    `root` 是源码工作根，缺省取本包所在的仓库根
    （`ai_butler/agents/` → 上两级）。测试与「仓库不在默认位置」的部署
    显式传入。
    """
    base = Path(getattr(settings, "storage_root", "") or "data/dev/storage") / "offline"
    if root is None:
        root = Path(__file__).resolve().parents[2]
    return RepairPaths(
        root=Path(root),
        base=base,
        journal=chat_journal_path(settings),
        ledger=base / "revision-ledger.jsonl",
        trail=base / "repair-trail.jsonl",
        approvals=base / "approvals.json",
        authority=base / "authority",
    )


# --------------------------------------------------------------------------- #
# 三道门的第一道：角色天花板                                             #
# --------------------------------------------------------------------------- #
def require_operator_role(
        role: UserRole, *, action: str = "启动离线修复闭环") -> PermissionVerdict:
    """改源码是 L3 高危动作：**按动作定级，不按 Agent 定级**（U45）。

    角色压不住就拒——访客/成员/管理员都不行，只有账号主体（owner）行。
    这不是「信任某个人」，是「这个动作的后果只有账号主体承担得起」。
    """
    verdict = evaluate(PermissionRequest(
        agent=AgentIdentity(key=REPAIR_USER_ID, level=CapabilityLevel.L3_CRITICAL),
        role=role,
        sensitivity=DataSensitivity.L2_INTERNAL,
        required=CapabilityLevel.L3_CRITICAL))
    verdict.raise_if_denied(action=action)
    return verdict


# --------------------------------------------------------------------------- #
# 三道门的第二道：机器授权簿                                             #
# --------------------------------------------------------------------------- #
def ensure_authorized(registry: AuthorityRegistry, *,
                      subject: str = REPAIR_USER_ID,
                      paths: tuple[str, ...] = (),
                      action: str = "") -> GrantVerdict:
    """本机授权簿里没有有效授权就拒（U88）。

    `paths` 给空 = 只问「这个主体在本机有没有权」；路径级的上限由
    `PatchGuard` 在每份补丁应用前判（两层独立，见 `core/authority.py`）。
    """
    verdict = registry.check(subject, paths=paths)
    verdict.raise_if_denied(
        action=action or f"离线闭环修改源码（{subject}）")
    return verdict


# --------------------------------------------------------------------------- #
# 三道门的第三道：装配                                                     #
# --------------------------------------------------------------------------- #
def build_production_repair_loop(
        *, settings: Any, redis: Any = None, root: Path | str | None = None,
        paths: RepairPaths | None = None, gateway: Any = None,
        runs: int = 2, max_rounds: int = DEFAULT_MAX_ROUNDS,
        approval_ttl_seconds: float = 24 * 3600,
        modules: tuple[str, ...] | None = None) -> RepairLoop:
    """装配**生产形态**的闭环。**唯一生产装配点**（E-77 的疫苗）。

    与测试装配（`build_repair_loop` 直接传替身）的区别只有一件事：
    这里每一个端口都给**真实现**——

    - coder：`LLMCoder(gateway)`，模型取 `ollama_model` 回退
      `ollama_vision_model`（与在线链路同一个表达式；两者都空则交给
      供应商，而 Ollama 空模型名是装配缺陷，会在调用时暴露）；
    - sandbox：`ProcessSandbox(root)`（进程级受限适配器，无容器隔离——
      U34 已如实登记，这里不假装有）；
    - 版本账 / 溯源账：落盘（跨进程撤销与编号续接的前提，U82/U83）；
    - 审批票：`FileApprovalService`（撤销票要跨进程申请→批准→消费，U90）；
    - 首轮痕迹来源：会话事件账本（U84），与 `app.py` 埋点同一文件（U91）。

    `gateway` 可注入（测试缝）；不注入就现建 `LLMGateway(settings, redis)`。
    装配期失败一律抛出：对话管道、模型权重、账本文件的问题，
    要在启动时就说清楚，而不是等复评跑到一半才炸——那时它会被记成
    「复评失败」，很难指向真正的原因。

    `root` 与 `paths` 都描述「状态在哪、源码在哪」：**只给一个**。
    同时给就无法判断「补丁应用在哪个目录、账本记在哪个目录」——
    而这两个目录不一致时，看起来一切正常，只是撤错地方。
    """
    from ai_butler.infra.llm_gateway import LLMGateway

    if paths is not None and root is not None:
        raise ValueError(
            "root 与 paths 只能给一个：前者只重新指定源码工作根，"
            "后者是整套状态路径（其中已含 root）。同时给无法判断"
            "「补丁应用在哪个目录、账本记在哪个目录」")
    paths = paths or default_paths(settings, root=root)
    work_root = Path(paths.root)
    if not work_root.is_dir():
        raise FileNotFoundError(
            f"源码工作根不存在：{work_root}。补丁要应用与撤销的目标目录"
            f"必须存在（可用 --root 指定仓库根）")
    # 状态根就位：四本账与授权簿都在它下面。装配期建好，别等第一次写入
    # （那时一次会话刚结束、session_end 正等着落账）。
    paths.base.mkdir(parents=True, exist_ok=True)
    gateway = gateway if gateway is not None else LLMGateway(settings, redis)
    # 模型选择与在线链路同一个表达式（app.build_chat_orchestrator）：
    # 两处各写一次，迟早出现「评测跑 A 模型、改代码用 B 模型」。
    model = (getattr(settings, "ollama_model", "") or
             getattr(settings, "ollama_vision_model", "") or None)
    coder = LLMCoder(gateway=gateway, model=model)
    sandbox = ProcessSandbox(root=str(work_root))
    approvals = FileApprovalService(paths.approvals,
                                    ttl_seconds=approval_ttl_seconds)
    journal = EventJournal(journal_id=CHAT_JOURNAL_ID,
                           store=FileJournalStore(paths.journal))

    def builder() -> Any:
        """重建被测对话管道。**延迟导入**：顶部导入会把旧类名绑进本模块，
        补丁落盘后重建出来的还是旧实例（E-83）。

        管道的唯一装配点在 `app.py`——这里调它，不另写一份：
        两份装配迟早分叉，而分叉的方向恰好是「复评跑的不是生产那条管道」。
        """
        from ai_butler.app import build_chat_orchestrator
        from ai_butler.retrieval.wiring import build_search_fn
        from ai_butler.scheduler import intent_wiring

        router = intent_wiring.build_intent_router(settings, gateway=gateway)
        return build_chat_orchestrator(
            gateway=gateway, search=build_search_fn(settings), settings=settings,
            intent_router=router)

    return build_repair_loop(
        builder=builder,
        runs=runs,
        modules=(modules if modules is not None else CHAT_PIPELINE_MODULES),
        sandbox=sandbox,
        coder=coder,
        patch_root=str(work_root),
        max_rounds=max_rounds,
        ledger_path=str(paths.ledger),
        trail_path=str(paths.trail),
        approvals=approvals,
        journal=journal,
        user_id=REPAIR_USER_ID,
    )
