"""P0-1 账号与会话安全测试。"""

from __future__ import annotations

import time

import pytest

from ai_butler.core.security import (
    FieldCipher,
    SessionTokenError,
    create_session_token,
    decode_session_token,
    hash_password,
    mask_bank_card,
    mask_location,
    mask_phone,
    verify_password,
)

SECRET = "test-secret-not-for-production-0123456789abcdef"   # >=32 字节，满足 HS256 建议


def test_password_is_salted_and_never_plaintext():
    """计划书：用户账号密码加盐哈希存储，数据库不保存明文密码。"""
    h1 = hash_password("Passw0rd!测试")
    h2 = hash_password("Passw0rd!测试")
    assert h1 != h2, "两次哈希必须不同（随机盐）"
    assert "Passw0rd" not in h1
    assert h1.startswith("$2b$")
    assert verify_password("Passw0rd!测试", h1)
    assert not verify_password("Passw0rd!错误", h1)


def test_password_longer_than_bcrypt_limit_is_supported():
    long_pwd = "a" * 200
    assert verify_password(long_pwd, hash_password(long_pwd))


def test_field_cipher_roundtrip_and_random_iv(settings):
    """计划书：手机号、位置、支付信息等隐私字段独立加密存储。"""
    c = FieldCipher(settings.field_encryption_key)
    phone = "13800001111"
    a, b = c.encrypt(phone), c.encrypt(phone)
    assert a != b, "同一明文两次密文必须不同（随机 IV）"
    assert phone not in a
    assert c.decrypt(a) == phone and c.decrypt(b) == phone


def test_log_masking_never_leaks_plaintext():
    """计划书：日志禁止打印明文手机号、账号、银行卡、完整位置。"""
    assert mask_phone("13800001111") == "138****1111"
    assert mask_bank_card("6222021234567890123").endswith("0123")
    assert "6222021234567890123" not in mask_bank_card("6222021234567890123")
    assert mask_location("北京市朝阳区建国路88号") .endswith("…")


def test_session_token_roundtrip(settings):
    st = create_session_token(user_id="u-1", device_id="dev-a",
                              secret=SECRET, ttl_seconds=60)
    claims = decode_session_token(st.token, secret=SECRET)
    assert claims["sub"] == "u-1" and claims["jti"] == st.jti
    assert claims["dev"] == "dev-a" and claims["typ"] == "session"


def test_session_token_rejects_wrong_secret_and_expiry():
    st = create_session_token(user_id="u-1", device_id="dev-a", secret=SECRET, ttl_seconds=60)
    with pytest.raises(SessionTokenError):
        decode_session_token(st.token, secret="another-secret-that-is-long-enough-0123456789")
    expired = create_session_token(user_id="u-1", device_id="dev-a",
                                   secret=SECRET, ttl_seconds=1, now=int(time.time()) - 10)
    with pytest.raises(SessionTokenError):
        decode_session_token(expired.token, secret=SECRET)


def test_configured_ttl_actually_applies(settings):
    """用 .env 中实际配置的凭证有效期验证：恰好超过即失效、未超过仍有效。"""
    ttl = settings.session_ttl_seconds
    now = int(time.time())
    expired = create_session_token(user_id="u-1", device_id="d",
                                   secret=settings.session_token_secret,
                                   ttl_seconds=ttl, now=now - ttl - 1)
    alive = create_session_token(user_id="u-1", device_id="d",
                                 secret=settings.session_token_secret,
                                 ttl_seconds=ttl, now=now - ttl + 30)
    with pytest.raises(SessionTokenError):
        decode_session_token(expired.token, secret=settings.session_token_secret)
    assert decode_session_token(alive.token, secret=settings.session_token_secret)["sub"] == "u-1"


def test_token_requires_user_and_device():
    with pytest.raises(ValueError):
        create_session_token(user_id="", device_id="dev-a", secret=SECRET, ttl_seconds=60)
    with pytest.raises(ValueError):
        create_session_token(user_id="u-1", device_id="", secret=SECRET, ttl_seconds=60)
