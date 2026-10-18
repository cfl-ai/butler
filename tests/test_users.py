"""账号注册表测试。

计划书依据：三、P0-1「用户账号密码加盐哈希存储，**数据库不保存明文密码**」。
"""

from __future__ import annotations

import pytest

from ai_butler.core.users import UserError, UserRegistry


@pytest.fixture()
def registry(workdir):
    return UserRegistry(workdir / "users.json")


def test_register_then_verify(registry):
    registry.register(user_id="u-1", password="correct-horse")
    assert registry.verify(user_id="u-1", password="correct-horse") is True


def test_wrong_password_is_rejected(registry):
    registry.register(user_id="u-1", password="correct-horse")
    assert registry.verify(user_id="u-1", password="wrong-horse") is False


def test_unknown_account_is_rejected(registry):
    assert registry.verify(user_id="ghost", password="anything") is False


def test_plaintext_password_never_touches_disk(registry):
    """这是本模块存在的核心意义 —— 计划书原话「数据库不保存明文密码」。"""
    secret = "plaintext-must-not-appear"
    registry.register(user_id="u-1", password=secret)
    raw = (registry.path).read_text(encoding="utf-8")
    assert secret not in raw
    assert "password_hash" in raw
    assert raw.count("$2") >= 1, "应存 bcrypt 哈希（$2b$ 前缀）"


def test_duplicate_account_is_refused(registry):
    registry.register(user_id="u-1", password="correct-horse")
    with pytest.raises(UserError):
        registry.register(user_id="u-1", password="another-pass")


def test_blank_account_or_short_password_is_refused(registry):
    with pytest.raises(UserError):
        registry.register(user_id="   ", password="correct-horse")
    with pytest.raises(UserError):
        registry.register(user_id="u-2", password="short")


def test_two_accounts_with_same_password_get_different_hashes(registry):
    """加盐的意义：同口令不得产生相同哈希。"""
    registry.register(user_id="u-1", password="same-password")
    registry.register(user_id="u-2", password="same-password")
    import json
    data = json.loads(registry.path.read_text(encoding="utf-8"))
    assert data["u-1"]["password_hash"] != data["u-2"]["password_hash"]
