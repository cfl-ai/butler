"""P0-1 配置、参数授权台账与文档忠实度测试。"""

from __future__ import annotations

import pytest

from ai_butler.core.config import (
    PARAM_PROVENANCE,
    SESSION_PARAM_INVARIANTS,
    MissingAuthorizedParamError,
    load_settings,
)


def test_missing_authorized_params_are_named(monkeypatch):
    """人工授权参数缺失时必须报错并点名环境变量，不得静默兜底。"""
    monkeypatch.setenv("SESSION_TTL_SECONDS", "")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_SECONDS", "")
    with pytest.raises(MissingAuthorizedParamError) as ei:
        load_settings(_env_file=None, session_token_secret="x")
    msg = str(ei.value)
    assert "SESSION_TTL_SECONDS" in msg
    assert "SESSION_IDLE_TIMEOUT_SECONDS" in msg
    assert "人工授权" in msg and "PARAM_PROVENANCE" in msg


def test_provenance_records_are_complete():
    """每个非计划书参数都必须能回溯到：原文引用、授权来源、取值理由。"""
    for field, meta in PARAM_PROVENANCE.items():
        for key in ("env", "plan_quote", "status", "authorized_at", "value", "rationale"):
            assert meta.get(key), f"{field} 缺少 {key}"
        assert meta["status"].startswith("人工授权") or meta["status"] == "实现参数"


def test_session_params_present_and_satisfy_invariants(settings):
    """不变式：空闲阈值必须严于凭证有效期，否则空闲控制形同虚设。

    本用例只断言关系与合理性，不断言具体数值——数值可按实际情况调整。
    """
    assert settings.session_ttl_seconds > 0
    assert settings.session_idle_timeout_seconds > 0
    assert settings.session_refresh_ahead_seconds > 0
    assert settings.session_idle_timeout_seconds < settings.session_ttl_seconds
    assert settings.session_refresh_ahead_seconds < settings.session_ttl_seconds
    assert len(SESSION_PARAM_INVARIANTS) == 2


def test_jev_frozen_params_match_plan_section_eight(settings):
    """计划书【八】Jev 配置 4 项：数值必须与文档完全一致，不得改动。"""
    assert settings.jev_enable is True                     # jev_enable = true
    assert settings.jev_confidence_threshold == 0.82       # jev_confidence_threshold = 0.82
    assert settings.jev_fallback_enable is True            # jev_fallback_enable = true
    assert settings.jev_shadow_mode is True                # jev_shadow_mode = true


def test_agent_wiki_frozen_params_match_plan_section_eight(settings):
    """计划书【八】Agent-Wiki v2 配置 6 项：数值必须与文档完全一致，不得改动。"""
    assert settings.wiki_public_workspace_enable is True     # wiki_public_workspace_enable = true
    assert settings.wiki_user_vault_per_user is True         # wiki_user_vault_per_user = true
    assert settings.wiki_compile_auto_run is True            # wiki_compile_auto_run = true
    assert settings.wiki_auto_report_contradiction is True   # wiki_auto_report_contradiction = true
    assert settings.wiki_auto_report_low_confidence is True  # wiki_auto_report_low_confidence = true
    assert settings.wiki_max_digest_size == 12000            # wiki_max_digest_size = 12000


def test_read_write_split_defaults_to_primary(settings):
    """未配置只读库时读写分离复用主库。"""
    assert settings.read_dsn == settings.write_dsn
