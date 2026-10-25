"""本机 CLI 入口的守护测试（U89）。

挡的是「门装了但没人过」：授权簿、角色天花板、审批票各有实现，
但如果 CLI 不按固定顺序问这两道门、不把结论说清，运营的人就只能
靠试错发现「为什么跑不起来」——而试错的方向通常是绕过它。

本文件钉住四件事：

1. **顺序**：角色门 → 授权门 → 装配。任一不过，**不装配**（不等十秒模型加载）；
2. **拒绝能行动**：每种拒绝都给出下一步命令（grant / approve / 补参数）；
3. **授权簿全生命周期**：grant → status → transfer → revoke 在 CLI 上走得通；
4. **撤销票跨进程**：run 申请 → approve 署名 → 票可消费（一次性）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from ai_butler.agents.repair_loop import REPAIR_USER_ID, RepairOutcome
from ai_butler.core.approval import FileApprovalService
from ai_butler.core.authority import AuthorityRegistry
from ai_butler.core.config import load_settings

REPO = Path(__file__).resolve().parents[1]


def _load_cli():
    """按文件路径载入 CLI 模块（scripts/ 不是包，与 p10_harness 同坑）。"""
    spec = importlib.util.spec_from_file_location(
        "run_repair_loop_cli", REPO / "scripts" / "run_repair_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return _load_cli()


@pytest.fixture()
def settings(tmp_path):
    return load_settings(storage_root=str(tmp_path / "storage"),
                         intent_router_enable=False)


def _outcome(**kw) -> RepairOutcome:
    base = dict(user_id=REPAIR_USER_ID, verdict="accepted", accepted=True,
                clean=False, reason="修好了", case_count=2, defect_count=3,
                rounds_run=1)
    base.update(kw)
    return RepairOutcome(**base)


# --------------------------------------------------------------------------- #
# 一、三道门的顺序与拒绝质量
# --------------------------------------------------------------------------- #
def test_run_requires_identity(cli, settings, capsys):
    code = cli.main(["run", "--cases", "x.json"], settings=settings)
    assert code != 0
    assert "--user-id" in capsys.readouterr().err


def test_run_refuses_non_owner_role_before_assembly(cli, settings, capsys):
    """角色天花板先于授权与装配：成员连授权簿都不该被问到。"""
    registry = AuthorityRegistry(root=_authority_root(settings))
    registry.grant(REPAIR_USER_ID, allowed_prefixes=("ai_butler/",),
                   granted_by="owner", ttl_seconds=3600)
    code = cli.main(["run", "--user-id", "someone", "--session-id", "s1",
                     "--role", "member", "--cases", "x.json"], settings=settings)
    assert code != 0
    err = capsys.readouterr().err
    assert "L3" in err or "拒绝" in err


def test_run_refuses_without_machine_grant(cli, settings, capsys):
    code = cli.main(["run", "--user-id", "owner", "--session-id", "s1",
                     "--cases", "x.json"], settings=settings)
    assert code != 0
    err = capsys.readouterr().err
    assert "授权" in err and "grant" in err          # 拒绝理由能行动


def test_run_gate_order_is_role_then_authority(cli, settings, capsys):
    """成员 + 无授权：报的是角色（先问的那道门），不是授权。"""
    code = cli.main(["run", "--user-id", "u", "--session-id", "s",
                     "--role", "member", "--cases", "x.json"], settings=settings)
    assert code != 0
    assert "L3" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 二、run 的 happy path（替身装配，不加载模型）
# --------------------------------------------------------------------------- #
def _authority_root(settings) -> Path:
    return Path(settings.storage_root) / "offline" / "authority"


def _grant(settings, **kw):
    registry = AuthorityRegistry(root=_authority_root(settings))
    base = dict(allowed_prefixes=("ai_butler/retrieval/",),
                granted_by="owner", ttl_seconds=3600)
    base.update(kw)
    return registry.grant(REPAIR_USER_ID, **base)


def test_run_happy_path_with_stub_loop(cli, settings, tmp_path, monkeypatch,
                                       capsys):
    _grant(settings)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(
        [{"case_id": "c1", "question": "年假几天"}]), encoding="utf-8")
    seen: dict = {}

    class _StubLoop:
        async def run(self, **kwargs):
            seen.update(kwargs)
            return _outcome()

    monkeypatch.setattr(cli, "build_production_repair_loop",
                        lambda **kw: _StubLoop())
    code = cli.main(["run", "--user-id", "owner", "--session-id", "s1",
                     "--cases", str(cases), "--json"], settings=settings)
    assert code == 0
    out = capsys.readouterr().out
    assert "授权有效" in out and "accepted" in out
    assert seen["cases"][0]["case_id"] == "c1"


def test_run_stopped_outcome_exits_nonzero(cli, settings, tmp_path,
                                           monkeypatch, capsys):
    _grant(settings)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(
        [{"case_id": "c1", "question": "年假几天"}]), encoding="utf-8")

    class _StubLoop:
        async def run(self, **kwargs):
            return _outcome(verdict="rejected_no_effect", accepted=False,
                            requires_manual=True, reason="一个都没修好")

    monkeypatch.setattr(cli, "build_production_repair_loop",
                        lambda **kw: _StubLoop())
    code = cli.main(["run", "--user-id", "owner", "--session-id", "s1",
                     "--cases", str(cases)], settings=settings)
    assert code != 0
    assert "一个都没修好" in capsys.readouterr().out


def test_run_prints_approval_resume_hint(cli, settings, tmp_path,
                                         monkeypatch, capsys):
    _grant(settings)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(
        [{"case_id": "c1", "question": "年假几天"}]), encoding="utf-8")

    class _StubLoop:
        async def run(self, **kwargs):
            return _outcome(
                verdict="", accepted=False, requires_approval=True,
                approval_request={"approval_id": "ticket-1",
                                  "round_ids": ["R1"], "reason": "要修必须先撤销"},
                reason="[undo_needs_approval] 冲突文件已被 R1 改动过")

    monkeypatch.setattr(cli, "build_production_repair_loop",
                        lambda **kw: _StubLoop())
    code = cli.main(["run", "--user-id", "owner", "--session-id", "s1",
                     "--cases", str(cases)], settings=settings)
    assert code != 0
    out = capsys.readouterr().out
    assert "approve --ticket ticket-1" in out
    assert "--undo-approval ticket-1" in out


def test_missing_cases_file_says_where_the_example_is(cli, settings, capsys):
    _grant(settings)
    code = cli.main(["run", "--user-id", "owner", "--session-id", "s1",
                     "--cases", str(settings.storage_root + "/nope.json")],
                    settings=settings)
    assert code != 0
    assert "repair_cases.example.json" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 三、授权簿全生命周期
# --------------------------------------------------------------------------- #
def test_grant_status_transfer_revoke_flow(cli, settings, capsys):
    assert cli.main(["grant", "--by", "owner", "--ttl-days", "30",
                     "--note", "首授"], settings=settings) == 0
    out = capsys.readouterr().out
    assert REPAIR_USER_ID in out and "ai_butler/retrieval/" in out
    # status 看得见
    assert cli.main(["status"], settings=settings) == 0
    assert "活跃 1" in capsys.readouterr().out
    # 转让：范围与剩余期限照搬
    assert cli.main(["transfer", "--from-subject", REPAIR_USER_ID,
                     "--to-subject", "system:p10-repair-2", "--by", "owner"],
                    settings=settings) == 0
    out = capsys.readouterr().out
    assert "system:p10-repair-2" in out and "照搬" in out
    # 旧主体失效、新主体有效
    assert cli.main(["revoke", "--subject", "system:p10-repair-2",
                     "--by", "owner", "--reason", "演示结束"],
                    settings=settings) == 0
    assert cli.main(["status"], settings=settings) == 0
    summary_out = capsys.readouterr().out
    assert "活跃 0" in summary_out and "已吊销 1" in summary_out


def test_grant_requires_signature(cli, settings, capsys):
    # argparse 拦不住空串（--by 给了就是给了），由业务层拦：
    for empty in ("", "   "):
        code = cli.main(["grant", "--by", empty], settings=settings)
        assert code != 0
        assert "署名" in capsys.readouterr().err


def test_duplicate_grant_is_refused_with_next_step(cli, settings, capsys):
    assert cli.main(["grant", "--by", "owner"], settings=settings) == 0
    capsys.readouterr()
    code = cli.main(["grant", "--by", "owner"], settings=settings)
    assert code != 0
    assert "revoke" in capsys.readouterr().err


def test_narrow_scope_grant(cli, settings, capsys):
    code = cli.main(["grant", "--by", "owner", "--narrow",
                     "ai_butler/retrieval/planner.py"], settings=settings)
    assert code == 0
    assert "ai_butler/retrieval/planner.py" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 四、审批票跨进程：run 申请 → approve 署名 → 可消费
# --------------------------------------------------------------------------- #
def test_approve_reject_and_tickets_flow(cli, settings, capsys):
    approvals = FileApprovalService(
        Path(settings.storage_root) / "offline" / "approvals.json",
        ttl_seconds=3600)
    ticket = approvals.request(tool="harness.revision.undo", signature="sig-1",
                               agent=REPAIR_USER_ID, role="p10-harness",
                               tier=2, reason="撤销 R1")
    capsys.readouterr()
    assert cli.main(["tickets"], settings=settings) == 0
    assert ticket.approval_id in capsys.readouterr().out
    assert cli.main(["approve", "--ticket", ticket.approval_id, "--by", "owner"],
                    settings=settings) == 0
    out = capsys.readouterr().out
    assert "已批准" in out and "一次性" in out
    # 票在新实例里可用（跨进程）
    fresh = FileApprovalService(
        Path(settings.storage_root) / "offline" / "approvals.json",
        ttl_seconds=3600)
    consumed = fresh.consume(ticket.approval_id, tool="harness.revision.undo",
                             signature="sig-1", agent=REPAIR_USER_ID,
                             role="p10-harness")
    assert consumed.consumed is True


def test_anonymous_approval_is_refused(cli, settings, capsys):
    approvals = FileApprovalService(
        Path(settings.storage_root) / "offline" / "approvals.json",
        ttl_seconds=3600)
    ticket = approvals.request(tool="t", signature="s", agent="a", role="r",
                               tier=2, reason="x")
    capsys.readouterr()
    code = cli.main(["approve", "--ticket", ticket.approval_id, "--by", "  "],
                    settings=settings)
    assert code != 0
    assert "署名" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 五、读账
# --------------------------------------------------------------------------- #
def test_explain_and_trace_read_the_trail(cli, settings, tmp_path, capsys):
    """读账两个子命令都从同一本溯源账来，且轮次账是唯一入口。"""
    from ai_butler.harness.trail import RepairTrail, RoundRecord
    trail_path = Path(settings.storage_root) / "offline" / "repair-trail.jsonl"
    trail_path.parent.mkdir(parents=True, exist_ok=True)
    trail = RepairTrail(path=trail_path)
    trail.append(RoundRecord(
        round_id="R1", started_at="2026-10-25T00:00:00+00:00",
        defects=({"defect_id": "d1", "severity": "major", "kind": "recall_gap",
                  "evidence": "问年假天数只召回了考勤制度",
                  "target_hint": "ai_butler/retrieval/planner.py"},),
        fingerprint="fp", rationale="把召回阈值下调了一档",
        patch={"applied": True, "paths": ["ai_butler/retrieval/planner.py"],
               "touched": 2}))
    capsys.readouterr()
    assert cli.main(["explain", "--round", "R1"], settings=settings) == 0
    out = capsys.readouterr().out
    assert "R1" in out and "d1" in out and "召回阈值" in out
    assert cli.main(["trace", "--defect", "d1"], settings=settings) == 0
    assert "planner.py" in capsys.readouterr().out
    # 查不到的轮次与缺陷也要说清，不是静默退出零
    assert cli.main(["explain", "--round", "R9"], settings=settings) == 0
    assert "没有 R9" in capsys.readouterr().out
    assert cli.main(["trace", "--defect", "nope"], settings=settings) == 0
    assert "没有缺陷 nope" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 六、脚本本身可执行（子进程级）
# --------------------------------------------------------------------------- #
def test_script_is_directly_runnable(tmp_path):
    """`python scripts/run_repair_loop.py` 必须真的跑起来。

    这条守的是一个**曾经真实发生过**的缺陷：`main()` 写得齐全、
    全部测试直接调用 `cli.main(...)` 全绿，但文件里没有
    `if __name__ == "__main__"`——直接执行脚本安静地成功退出，
    什么都发生。测试绕过入口，入口就没被测试保护过。
    """
    import subprocess
    import sys

    storage = tmp_path / "storage"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_repair_loop.py"), "status",
         "--storage-root", str(storage)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    assert "机器" in proc.stdout and "授权簿" in proc.stdout
    # 空簿也要把文件建出来：下一句 grant 要有地方写
    assert (storage / "offline" / "authority" / "machine.key").is_file()


def test_script_run_without_user_id_fails_as_subprocess(tmp_path):
    """直接执行时身份缺失也是「一行原因 + 退出 1」，不是栈。"""
    import subprocess
    import sys

    storage = tmp_path / "storage"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_repair_loop.py"), "run",
         "--storage-root", str(storage), "--cases", "x.json"],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    assert proc.returncode == 1
    assert "--user-id" in proc.stderr
    assert "Traceback" not in proc.stderr
