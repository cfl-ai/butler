"""P0-1 日志脱敏测试。

计划书依据：三、P0-1「日志脱敏规则：日志禁止打印明文手机号、密码、银行卡、完整位置；
保留 user_id 用于排障，剥离原始隐私内容」。
"""

from __future__ import annotations

import logging

from ai_butler.core.log_safety import ERASE_KEYS, RedactingFilter, redact, redact_text

ERASED = "***已抹除***"


def test_structured_payload_is_redacted():
    payload = {
        "user_id": "u-10086",
        "password": "P@ssw0rd!",
        "session_token": "eyJhbGciOi",
        "phone": "13800001111",
        "bank_card": "6222021234567890123",
        "location": "北京市朝阳区建国路88号",
        "nested": {"api_key": "sk-xxx", "note": "联系 13900002222"},
    }
    out = redact(payload)

    # 计划书明确保留 user_id 用于排障
    assert out["user_id"] == "u-10086"
    # 密钥类整体抹除，不做掩码
    assert out["password"] == ERASED
    assert out["session_token"] == ERASED
    assert out["nested"]["api_key"] == ERASED
    # 掩码类：明文不得出现在整串输出里
    blob = str(out)
    assert "13800001111" not in blob and out["phone"] == "138****1111"
    assert "6222021234567890123" not in blob
    assert "建国路88号" not in blob
    assert "13900002222" not in out["nested"]["note"]


def test_login_account_field_is_masked():
    """字段名叫 account 的日志必须脱敏。

    这里原先是个**漏的洞**：security.py 里写好了 mask_account，
    却从没被加进 log_safety 的 MASK_RULES ——
    于是 {"account": "13800001111"} 原样进了日志。
    （七、13「全链路日志…全部做隐私脱敏」。）
    """
    out = redact({"account": "13800001111", "账号": "my_login_name"})
    assert out["account"] == "13***11"
    assert "13800001111" not in str(out)
    assert "my_login_name" not in str(out)


def test_account_no_still_uses_the_bank_masker():
    """规则表的**顺序**也要守住。

    MASK_RULES 取第一个命中的规则，而 "account_no" 同时含有 "account"
    与 "account_no" 两个关键字。银行卡那条必须排在账号那条之前 ——
    调换顺序会把银行卡号按账号掩码处理，掩码强度下降。
    """
    from ai_butler.core.log_safety import _field_masker
    from ai_butler.core.security import mask_account, mask_bank_card

    assert _field_masker("account_no") is mask_bank_card
    assert _field_masker("account") is mask_account
    # 端到端确认掩码强度
    masked = redact({"account_no": "6222021234567890123"})["account_no"]
    assert "6222021234567890123" not in masked
    assert masked.endswith("0123")


def test_free_text_patterns_are_masked():
    assert redact_text("账号 13800001111 登录成功") == "账号 138****1111 登录成功"
    assert "6222021234567890123" not in redact_text("卡号 6222021234567890123 已扣款")
    assert redact_text("没有敏感内容") == "没有敏感内容"


def test_all_declared_erase_keys_are_honoured():
    for key in ERASE_KEYS:
        assert redact("secret-value", key=key) == ERASED, f"{key} 未被抹除"


def test_user_id_is_never_erased_even_if_asked():
    assert redact("u-1", key="user_id") == "u-1"
    assert redact({"user_id": "u-1", "password": "x"})["user_id"] == "u-1"


def test_redacting_filter_scrubs_log_records():
    logger = logging.getLogger("aibutler.test.redaction")
    logger.handlers.clear()
    logger.propagate = False
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = Capture()
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info("登录 %(user_id)s phone=%(phone)s password=%(password)s",
                {"user_id": "u-1", "phone": "13800001111", "password": "P@ss"})

    line = captured[-1]
    assert "13800001111" not in line
    assert "P@ss" not in line
    assert "u-1" in line, "user_id 必须保留用于排障"
