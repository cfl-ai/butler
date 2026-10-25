"""离线修复闭环的本机入口（U89）。

计划书依据
----------
- 三、P10 触发时机「7×24 小时后台离线自动运行，不占用业务算力」
- 三、P10 离线链路「…人工审批后台审核 → 审批通过自动版本升级，驳回终止迭代」

这个脚本是什么
--------------
`agents/repair_entry.py` 那条链路（评测中台 → harness 改代码 → 复评）的
**人工触发入口**。§54.21 第八节登记过两处接线缺口——没有入口、coder 无人
注入——本脚本与 `build_production_repair_loop` 各补一处。

用户指令（2026-10-25）：「以本地这台机器作为有权改源码（可通过本台机器
修改转让权限等）」。所以：

- `run` 之前有两道门：**角色天花板**（只有 owner 压得住 L3 高危动作）与
  **机器授权簿**（本机授权簿里没有有效授权就拒，U88）；
- `grant` / `transfer` / `revoke` 管理授权簿：授权、转让、吊销都在这台
  机器上完成，全部留痕；
- `approve` / `reject` / `tickets` 管理撤销票：撤销一张**已提交**的补丁
  需人工确认（U81），票跨进程存活（U90），一次一焚（U59）。

为什么不是 HTTP 端点
--------------------
计划书未定义调度语义（U85），而在分布式权威就位之前，把「让系统改它自己
源码」的触发权放在一台**有人签字授权**的机器上，比放在「谁都能 POST 一下」
的端点上更接近本意。这不是永远不做，是还没到能做的时候（U88 边界）。

用法
----
    python scripts/run_repair_loop.py status
    python scripts/run_repair_loop.py grant --by owner --ttl-days 30
    python scripts/run_repair_loop.py run --user-id owner --session-id s1 \\
        --cases scripts/repair_cases.example.json
    python scripts/run_repair_loop.py approve --ticket <id> --by owner
    python scripts/run_repair_loop.py explain --round R1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

# 直接运行（python scripts/run_repair_loop.py）时 sys.path[0] 是 scripts/，
# `import ai_butler` 会 ModuleNotFoundError——与 agents/p10_harness.py 同坑。
if not __package__:  # pragma: no cover - 仅直接运行时触发
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_butler.agents.repair_entry import (                       # noqa: E402
    build_production_repair_loop,
    default_paths,
    ensure_authorized,
    require_operator_role,
)
from ai_butler.agents.repair_loop import REPAIR_USER_ID           # noqa: E402
from ai_butler.agents.repair_loop import RepairOutcome            # noqa: E402
from ai_butler.core.approval import ApprovalError, FileApprovalService  # noqa: E402
from ai_butler.core.authority import AuthorityError, AuthorityRegistry  # noqa: E402
from ai_butler.core.config import Settings, load_settings         # noqa: E402
from ai_butler.core.environments import validate_runtime          # noqa: E402
from ai_butler.core.permissions import PermissionError, UserRole  # noqa: E402
from ai_butler.harness.patch import (                             # noqa: E402
    ALLOWED_SCOPE_BY_KIND,
    FORBIDDEN_PATHS,
)
from ai_butler.harness.trail import RepairTrail                   # noqa: E402

#: 授权簿默认 TTL（天）。**限期强制**（U88）：30 天是与「日级排程」匹配的
#: 量级——超过一个月没续期的授权，通常已经不是「在用」而是「忘了」。
DEFAULT_TTL_DAYS = 30.0

EXIT_ERROR = 1


class CliError(RuntimeError):
    """用法/装配错误。**报错并退出非零**，不静默降级成打印档案。"""


# --------------------------------------------------------------------------- #
# 装配辅助
# --------------------------------------------------------------------------- #
def default_scopes() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """授权簿的默认范围：各缺陷类别可改前缀的并集 + 全局禁区。

    与 `PatchGuard` 共用同一份数据（`harness/patch.py`）：授权簿管
    「谁/哪台机器」，`PatchGuard` 管「哪些文件永远不许动」，但两者的
    **范围口径必须一致**——授权比防线宽没有意义（批了也被拦），
    比防线窄也没有意义（批了也改不了）。
    """
    allowed = tuple(sorted({p for scopes in ALLOWED_SCOPE_BY_KIND.values()
                            for p in scopes}))
    return allowed, tuple(FORBIDDEN_PATHS)


def load_cases(path: str) -> list[dict[str, Any]]:
    """读评测用例（State 形态 dict 的 JSON 数组）。金标准由调用方给。"""
    target = Path(path)
    if not target.is_file():
        raise CliError(
            f"用例文件不存在：{target}。金标准（relevant_ids / expected_points）"
            f"要对着真实知识库填写，仓库里只放格式示例："
            f"scripts/repair_cases.example.json")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CliError(f"用例文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, list) or not data:
        raise CliError("用例文件应为**非空** JSON 数组")
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise CliError(f"第 {index} 条用例不是 dict")
        if not str(item.get("case_id") or "").strip():
            raise CliError(f"第 {index} 条用例缺 case_id")
        if not str(item.get("question") or "").strip():
            raise CliError(f"第 {index} 条用例缺 question")
    return [dict(item) for item in data]


def load_traces(path: str) -> list[dict[str, Any]]:
    """读回溯重放的痕迹（可选）。给了就按调用方给的来，不读会话账本。"""
    target = Path(path)
    if not target.is_file():
        raise CliError(f"痕迹文件不存在：{target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CliError(f"痕迹文件不是合法 JSON：{exc}") from exc
    if not isinstance(data, list):
        raise CliError("痕迹文件应为 JSON 数组")
    return [dict(item) for item in data if isinstance(item, dict)]


def _load_settings(args: argparse.Namespace) -> Settings:
    injected = getattr(args, "injected_settings", None)
    if injected is not None:
        return injected                              # 测试缝：临时存储根，免环境守卫
    overrides: dict[str, Any] = {}
    if getattr(args, "storage_root", ""):
        overrides["storage_root"] = args.storage_root
    settings = load_settings(**overrides)
    # 环境守卫（与 server.py 同一条）：开发配置不许连生产的库
    validate_runtime(
        settings.environment,
        postgres_port=settings.postgres_port,
        postgres_db=settings.postgres_db,
        redis_port=settings.redis_port,
    )
    return settings


def _registry(args: argparse.Namespace, settings: Settings) -> AuthorityRegistry:
    paths = default_paths(settings, root=getattr(args, "root", None))
    return AuthorityRegistry(root=paths.authority)


def _approvals(args: argparse.Namespace, settings: Settings) -> FileApprovalService:
    paths = default_paths(settings, root=getattr(args, "root", None))
    ttl = float(getattr(args, "approval_ttl_hours", 24) or 24) * 3600
    return FileApprovalService(paths.approvals, ttl_seconds=ttl)


def _trail(args: argparse.Namespace, settings: Settings) -> RepairTrail:
    paths = default_paths(settings, root=getattr(args, "root", None))
    trail = RepairTrail(path=paths.trail)
    trail.load()
    return trail


# --------------------------------------------------------------------------- #
# 子命令：跑闭环
# --------------------------------------------------------------------------- #
def _cmd_run(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if not args.user_id or not args.session_id:
        raise CliError(
            "run 必须显式给出 --user-id 与 --session-id：离线链路没有登录用户，"
            "但审计要能追溯到这次运行是谁发起的（U48 同一立场）")
    role = UserRole[args.role.upper()]
    require_operator_role(role, action="启动离线修复闭环（改源码是 L3 高危动作）")

    paths = default_paths(settings, root=args.root)
    registry = AuthorityRegistry(root=paths.authority)
    # 两道门先问完再装配：装配要加载模型（约十秒），
    # 「不该跑」的答案没有理由让人等十秒。
    ensure_authorized(registry, action="离线闭环修改源码")
    print(f"授权有效：{registry.check(REPAIR_USER_ID).reason}"
          f"（机器 {registry.identity.machine_id[:8]}…）")

    cases = load_cases(args.cases)
    traces = load_traces(args.traces) if args.traces else []
    print(f"状态目录：{paths.base}")
    print(f"用例 {len(cases)} 条"
          + (f"，回溯痕迹 {len(traces)} 条" if traces
             else "；首轮痕迹从会话账本回放（无痕迹将停在 no_traces）"))

    loop = build_production_repair_loop(
        settings=settings, paths=paths, runs=args.runs,
        max_rounds=args.max_rounds,
        approval_ttl_seconds=float(args.approval_ttl_hours) * 3600)
    outcome = asyncio.run(loop.run(
        cases=cases, traces=traces,
        undo_approval=args.undo_approval or "",
        undo_rounds=[r.strip() for r in (args.undo_rounds or "").split(",")
                     if r.strip()]))
    _print_outcome(outcome, as_json=args.json)
    return 0 if (outcome.accepted or outcome.clean) else EXIT_ERROR


def _print_outcome(outcome: RepairOutcome, *, as_json: bool = False) -> None:
    """人读结论。**先结论后过程**：翻到最后才看到「没修」是最坏的排布。"""
    print()
    print("═" * 64)
    print(f" 结论：{outcome.verdict or '（未走到复评）'}")
    print(f" 原因：{outcome.reason}")
    print(f" 轮次 {outcome.rounds_run}，用例 {outcome.case_count} 个，"
          f"缺陷 {outcome.defect_count} 条")
    for stage in outcome.stages:
        mark = "✓" if stage.ok else "✗"
        print(f"   [{mark}] {stage.name:<10} {stage.detail}")
    if outcome.requires_approval and outcome.approval_request:
        request = outcome.approval_request
        print()
        print(" ⚠ 需要人工批准才能继续（撤销已提交补丁，U81）：")
        print(f"   票 {request.get('approval_id')}｜轮次 {request.get('round_ids')}")
        print(f"   原因：{request.get('reason')}")
        print(f"   批准：python scripts/run_repair_loop.py approve "
              f"--ticket {request.get('approval_id')} --by <你的名字>")
        print(f"   然后带回重跑：--undo-approval {request.get('approval_id')} "
              f"--undo-rounds {','.join(request.get('round_ids') or [])}")
    if outcome.undone:
        print(f" 本次撤销：{len(outcome.undone)} 份补丁已回基线")
    print("═" * 64)
    if as_json:
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# 子命令：授权簿
# --------------------------------------------------------------------------- #
def _cmd_grant(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if not args.by.strip():
        raise CliError("授权必须署名：--by <授权人>")
    registry = _registry(args, settings)
    allowed, forbidden = default_scopes()
    if args.narrow:
        # 收窄范围：只授调用方点名的前缀（仍需过禁区校验）
        allowed = tuple(p.strip() for p in args.narrow.split(",") if p.strip())
        if not allowed:
            raise CliError("--narrow 给了空范围")
    grant = registry.grant(
        args.subject, allowed_prefixes=allowed, forbidden_paths=forbidden,
        granted_by=args.by, ttl_seconds=args.ttl_days * 86400,
        note=args.note or "")
    print(f"已授权 {grant.subject}（至 {grant.expires_at}，by {grant.granted_by}）")
    print(f"  范围：{list(grant.allowed_prefixes)}")
    print(f"  禁区：{list(grant.forbidden_paths)}")
    return 0


def _cmd_transfer(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if not args.by.strip():
        raise CliError("转让必须署名：--by <授权人>")
    registry = _registry(args, settings)
    ttl = args.ttl_days * 86400 if args.ttl_days else None
    grant = registry.transfer(args.from_subject, args.to_subject, by=args.by,
                              ttl_seconds=ttl, note=args.note or "")
    print(f"已转让：{args.from_subject} → {grant.subject}"
          f"（至 {grant.expires_at}，by {grant.granted_by}）")
    print(f"  范围照搬：{list(grant.allowed_prefixes)}"
          f"{'（期限照搬剩余）' if ttl is None else ''}")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if not args.by.strip() or not args.reason.strip():
        raise CliError("吊销必须署名并给出原因：--by <授权人> --reason <原因>")
    registry = _registry(args, settings)
    grant = registry.revoke(args.subject, by=args.by, reason=args.reason)
    print(f"已吊销 {grant.subject}（by {args.by}）：{args.reason}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    registry = _registry(args, settings)
    summary = registry.status()
    print(f"机器 {summary['machine_id']}")
    print(f"授权簿 {summary['grants_file']}")
    counts = summary["counts"]
    print(f"  活跃 {counts['active']}｜已到期 {counts['expired']}｜"
          f"已吊销 {counts['revoked']}｜已转出 {counts['transferred']}")
    for grant in summary["active"]:
        print(f"  · {grant['subject']}  至 {grant['expires_at']}  "
              f"by {grant['granted_by']}")
        print(f"      范围 {grant['allowed_prefixes']}")
        if grant["supersedes"]:
            print(f"      自 {grant['supersedes']} 转让而来")
    for item in summary["tampered"]:
        print(f"  ⚠ 记录签名不符（被手改或来自别的机器）："
              f"{item['subject']} / {item['status']} / {item['granted_at']}")
    return 0


# --------------------------------------------------------------------------- #
# 子命令：审批票
# --------------------------------------------------------------------------- #
def _cmd_approve(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if not args.by.strip():
        raise CliError("批准必须署名：--by <批准人>（匿名放行不可审计，U59）")
    approvals = _approvals(args, settings)
    ticket = approvals.approve(args.ticket, by=args.by)
    print(f"已批准 {ticket.approval_id}（{ticket.tool}，by {ticket.decided_by}）")
    print(f"  一次性：消费后即焚。过期时刻 {ticket.expires_at:.0f}")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    if not args.by.strip():
        raise CliError("驳回必须署名：--by <驳回人>")
    approvals = _approvals(args, settings)
    ticket = approvals.reject(args.ticket, by=args.by)
    print(f"已驳回 {ticket.approval_id}（by {ticket.decided_by}）")
    return 0


def _cmd_tickets(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    approvals = _approvals(args, settings)
    tickets = approvals.tickets()
    if not tickets:
        print("没有审批票。撤销已提交补丁时闭环会自己申请一张。")
        return 0
    for ticket in tickets:
        print(f"  {ticket.approval_id}  {ticket.status:<9} {ticket.tool}"
              f"  by {ticket.decided_by or '—'}")
        print(f"      {ticket.reason}")
    return 0


# --------------------------------------------------------------------------- #
# 子命令：读账
# --------------------------------------------------------------------------- #
def _cmd_explain(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    trail = _trail(args, settings)
    print(trail.explain(args.round))
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    trail = _trail(args, settings)
    found = trail.trace_defect(args.defect)
    if not found:
        # 查不到不等于出错（可能还没跑过闭环），但也不能静默退出零——
        # 静默的成功和人以为「这条缺陷没人管过」是同一种骗。
        print(f"没有缺陷 {args.defect} 的记录：溯源账里没有它，"
              f"或还没跑过闭环（账本 {default_paths(settings).trail}）")
        return 0
    for item in found:
        print(f"  {item.render()}")
        print()
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_repair_loop.py",
        description="离线修复闭环的本机入口（U89）：授权簿 + 审批票 + 跑一轮闭环",
        epilog="先 grant 再 run；撤销已提交补丁需人工批准（approve → 带回重跑）。")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default="",
                       help="源码工作根（补丁应用与撤销的目标；默认仓库根）")
        p.add_argument("--storage-root", default="",
                       help="离线链路状态根（默认取 settings.storage_root/offline）")

    # run
    run = sub.add_parser("run", help="跑一轮闭环（评 → 修 → 复评）")
    common(run)
    run.add_argument("--user-id", default="", help="发起人身份（必填，进审计）")
    run.add_argument("--session-id", default="", help="会话标识（必填，进审计）")
    run.add_argument("--role", default="owner",
                     choices=["auditor", "member", "admin", "owner"],
                     help="发起人角色；改源码是 L3，只有 owner 压得住")
    run.add_argument("--cases", required=True,
                     help="评测用例 JSON（金标准；示例见 scripts/repair_cases.example.json）")
    run.add_argument("--traces", default="",
                     help="回溯重放的痕迹 JSON（可选；不给则从会话账本回放）")
    run.add_argument("--max-rounds", type=int, default=3,
                     help="最多几轮（实现参数，计划书未给刻度）")
    run.add_argument("--runs", type=int, default=2,
                     help="每个用例重跑几次（稳定性维度至少 2 次才有分母）")
    run.add_argument("--undo-approval", default="",
                     help="人工已批准的撤销票 approval_id（一次性）")
    run.add_argument("--undo-rounds", default="",
                     help="要撤销的轮次，逗号分隔（如 R1,R2）")
    run.add_argument("--approval-ttl-hours", type=float, default=24,
                     help="撤销票有效期（小时；无人应答即拒）")
    run.add_argument("--json", action="store_true", help="额外打印机器可读结论")

    # grant
    grant = sub.add_parser("grant", help="授权某主体改源码（限期强制）")
    common(grant)
    grant.add_argument("--subject", default=REPAIR_USER_ID,
                       help=f"授权主体（默认 {REPAIR_USER_ID}）")
    grant.add_argument("--by", required=True, help="授权人署名（必填）")
    grant.add_argument("--ttl-days", type=float, default=DEFAULT_TTL_DAYS,
                       help=f"有效期天数（默认 {DEFAULT_TTL_DAYS:g}；必须为正）")
    grant.add_argument("--note", default="", help="授权理由（进留痕）")
    grant.add_argument("--narrow", default="",
                       help="收窄范围：逗号分隔的路径前缀（默认各缺陷类别并集减禁区）")

    # transfer
    transfer = sub.add_parser("transfer", help="把授权转让给另一个主体")
    common(transfer)
    transfer.add_argument("--from-subject", dest="from_subject", required=True)
    transfer.add_argument("--to-subject", dest="to_subject", required=True)
    transfer.add_argument("--by", required=True, help="授权人署名（必填）")
    transfer.add_argument("--ttl-days", type=float, default=0,
                          help="延长有效期天数（默认 0 = 照搬剩余期限，不自动延长）")
    transfer.add_argument("--note", default="")

    # revoke
    revoke = sub.add_parser("revoke", help="吊销授权（记录留痕，不删）")
    common(revoke)
    revoke.add_argument("--subject", required=True)
    revoke.add_argument("--by", required=True)
    revoke.add_argument("--reason", required=True)

    # status
    status = sub.add_parser("status", help="看授权簿")
    common(status)

    # approve / reject / tickets
    approve = sub.add_parser("approve", help="批准一张撤销票")
    common(approve)
    approve.add_argument("--ticket", required=True)
    approve.add_argument("--by", required=True)

    reject = sub.add_parser("reject", help="驳回一张撤销票")
    common(reject)
    reject.add_argument("--ticket", required=True)
    reject.add_argument("--by", required=True)

    tickets = sub.add_parser("tickets", help="看全部审批票")
    common(tickets)

    # explain / trace
    explain = sub.add_parser("explain", help="读某一轮的人读说明")
    common(explain)
    explain.add_argument("--round", required=True, help="轮次号，如 R1")

    trace = sub.add_parser("trace", help="读某条缺陷在各轮的遭遇")
    common(trace)
    trace.add_argument("--defect", required=True, help="缺陷 id")

    return parser


_HANDLERS = {
    "run": _cmd_run,
    "grant": _cmd_grant,
    "transfer": _cmd_transfer,
    "revoke": _cmd_revoke,
    "status": _cmd_status,
    "approve": _cmd_approve,
    "reject": _cmd_reject,
    "tickets": _cmd_tickets,
    "explain": _cmd_explain,
    "trace": _cmd_trace,
}


def main(argv: Sequence[str] | None = None, *,
         settings: Settings | None = None) -> int:
    """命令行主函数。返回进程退出码。

    `settings` 是测试缝：注入后跳过 load_settings 与环境守卫
    （测试用临时存储根，不该被开发环境的端口配置拦下）。
    非零退出码一律伴随 stderr 上的一行原因——**不静默失败**。

    三类拒绝原样接住并转为「一行原因 + 退出 1」：授权簿
    （AuthorityError）、审批票（ApprovalError）、角色天花板
    （`core/permissions.py` 的 `PermissionError`——`raise_if_denied` 抛的
    是本项目自己的异常，不是内置的那个）。都是「该不该跑」的答案，
    答案是不跑，就把不跑的原因说清楚，而不是把栈拍到运营的人脸上。
    """
    args = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    args.injected_settings = settings
    try:
        return _HANDLERS[args.command](args)
    except (CliError, AuthorityError, ApprovalError, PermissionError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - 入口自己走，由守护测试以子进程验
    # 没有这一段，`python scripts/run_repair_loop.py ...` 会安静地成功退出——
    # 而测试全部调用 `main()`，绿得看不出来。**入口必须是可执行的那一个**。
    sys.exit(main())
