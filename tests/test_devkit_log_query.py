"""P0-2 日志检索 / 问题标注 / 缺陷统计测试。

计划书依据：三、P0-2「开发日志检索、问题标注、缺陷自动统计工具；
业务接口禁止读取其他用户日志」
"""

from __future__ import annotations

import json

import pytest

from ai_butler.core.storage import FileStore
from ai_butler.devkit.log_query import CASE_CATEGORIES, AuditLog, LogAccessError


@pytest.fixture()
def audit(settings, workdir):
    return AuditLog(FileStore(workdir / "storage", settings=settings))


def test_append_requires_user_id(audit):
    with pytest.raises(LogAccessError):
        audit.append(event="login", level="INFO", user_id="")
    with pytest.raises(ValueError):
        audit.append(event="login", level="TRACE", user_id="u-1")


def test_detail_is_redacted_before_it_hits_disk(audit, settings, workdir):
    """计划书三、P0-1：日志禁止打印明文手机号、密码、银行卡、完整位置。"""
    record = audit.append(
        event="pay", level="INFO", user_id="u-1",
        detail={"password": "P@ss", "phone": "13800001111",
                "bank_card": "6222021234567890123", "location": "北京市朝阳区建国路88号"})
    blob = json.dumps(record.as_dict(), ensure_ascii=False)
    assert "P@ss" not in blob and "13800001111" not in blob
    assert "6222021234567890123" not in blob and "建国路88号" not in blob
    assert record.user_id == "u-1", "user_id 必须保留用于排障"

    on_disk = "".join(p.read_text(encoding="utf-8")
                      for p in (workdir / "storage" / "logs" / "audit").glob("*.jsonl"))
    assert "13800001111" not in on_disk


def test_query_requires_user_id(audit):
    """计划书三、P0-2：业务接口禁止读取其他用户日志。"""
    with pytest.raises(LogAccessError) as ei:
        audit.query(user_id="")
    assert "禁止读取其他用户日志" in str(ei.value)


def test_query_returns_only_that_users_records(audit):
    audit.append(event="plan", level="INFO", user_id="user-A", detail={"note": "A 的任务"})
    audit.append(event="plan", level="INFO", user_id="user-B", detail={"note": "B 的任务"})

    a = audit.query(user_id="user-A")
    b = audit.query(user_id="user-B")
    assert [r.detail["note"] for r in a] == ["A 的任务"]
    assert [r.detail["note"] for r in b] == ["B 的任务"]


def test_query_filters_by_level_and_event(audit):
    audit.append(event="plan", level="INFO", user_id="u-1")
    audit.append(event="plan", level="ERROR", user_id="u-1")
    audit.append(event="pay", level="ERROR", user_id="u-1")

    assert len(audit.query(user_id="u-1", level="ERROR")) == 2
    assert len(audit.query(user_id="u-1", event="pay")) == 1
    assert len(audit.query(user_id="u-1", level="ERROR", event="plan")) == 1


def test_annotate_uses_the_four_plan_categories(audit):
    """计划书三、P0-9：优质 / 错误案例分类；Jev 错误、Wiki 冲突案例独立分类。"""
    assert CASE_CATEGORIES == ("优质案例", "错误案例", "Jev错误", "Wiki冲突")

    rec = audit.append(event="jev", level="ERROR", user_id="u-1")
    for category in CASE_CATEGORIES:
        audit.annotate(rec.record_id, category=category, note="标注", by="tester")
    assert len(audit.annotations()) == 4

    with pytest.raises(ValueError):
        audit.annotate(rec.record_id, category="随便分类")


def test_defect_stats_returns_aggregates_without_any_log_body(audit):
    audit.append(event="plan", level="ERROR", user_id="user-A", detail={"note": "A 的私密细节"})
    audit.append(event="plan", level="INFO", user_id="user-B")
    audit.annotate("r1", category="Jev错误")

    stats = audit.defect_stats()
    blob = json.dumps(stats, ensure_ascii=False)
    assert stats["total"] == 2 and stats["defect_count"] == 1
    assert stats["annotation_counts"] == {"Jev错误": 1}
    assert "私密细节" not in blob, "统计不得夹带日志正文"
    assert "user-A" not in blob and "user-B" not in blob, "统计只暴露不可逆摘要"
    assert stats["unique_users"] == 2


def test_no_cross_user_content_entry_point_exists():
    """结构性断言：AuditLog 不得提供不带 user_id 的正文检索入口。"""
    import inspect
    sig = inspect.signature(AuditLog.query)
    assert "user_id" in sig.parameters
    assert sig.parameters["user_id"].default is inspect.Parameter.empty, "user_id 必须必填"
    public = [n for n in dir(AuditLog) if not n.startswith("_")]
    assert not ({"all", "query_all", "iter_all", "dump"} & set(public))
