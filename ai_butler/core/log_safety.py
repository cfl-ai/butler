"""P0-1 日志脱敏规则。

计划书依据
----------
- 三、P0-1「日志脱敏规则：日志禁止打印明文手机号、密码、银行卡、完整位置；
  保留 user_id 用于排障，剥离原始隐私内容」
- 三、P0-1「统一文件存储、日志存储、版本备份、项目回溯机制」
- 七、验收标准 13「全链路日志、离线自进化链路全部做隐私脱敏」

设计要点
--------
- user_id 是**明确保留**的排障字段（计划书原文），脱敏时不得抹掉；
- 密码、令牌、密钥类字段一律整体抹除，不做掩码；
- 手机号、银行卡、位置做掩码而非抹除，保留排障可用的尾号/粒度。
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Mapping

from ai_butler.core.security import (
    mask_account,
    mask_bank_card,
    mask_location,
    mask_phone,
)

#: 整字段抹除：出现即只留占位符，不做掩码
ERASE_KEYS = frozenset({
    "password", "passwd", "pwd", "old_password", "new_password",
    "token", "session_token", "access_token", "refresh_token", "authorization",
    "secret", "session_token_secret", "api_key", "apikey", "private_key",
    "field_encryption_key", "jwt_secret", "credential", "cookie",
})

#: 掩码字段：按关键字匹配字段名。
#:
#: **顺序有意义**：_field_masker 返回第一个命中的规则。
#: 银行卡那条带了 "account_no"，必须排在账号那条之前 ——
#: 否则 "account_no" 会先被 "account" 命中，用错掩码。
MASK_RULES: tuple[tuple[tuple[str, ...], Any], ...] = (
    (("phone", "mobile", "tel", "手机"), mask_phone),
    (("bank", "card", "银行卡", "account_no"), mask_bank_card),
    # 登录账号（七、13「全链路日志做隐私脱敏」）。
    # 这里原先漏了：mask_account 写好了却从没进规则表，
    # 于是字段名叫 account 的日志**一点都没脱敏**。
    (("account", "账号", "账户"), mask_account),
    (("location", "address", "geo", "lat", "lng", "位置", "地址"), mask_location),
)

#: 计划书明确保留、不得脱敏的排障字段
PRESERVE_KEYS = frozenset({"user_id", "userId"})

_ERASED = "***已抹除***"

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_BANK_RE = re.compile(r"(?<!\d)(\d{6})\d{6,11}(\d{4})(?!\d)")


def _field_masker(key: str):
    lowered = key.lower()
    for keywords, masker in MASK_RULES:
        if any(k in lowered for k in keywords):
            return masker
    return None


def redact(value: Any, *, key: str | None = None) -> Any:
    """递归脱敏。user_id 原样保留（计划书要求保留用于排障）。"""
    if key is not None:
        if key in PRESERVE_KEYS:
            return value
        if key.lower() in ERASE_KEYS:
            return _ERASED
        masker = _field_masker(key)
        if masker is not None and isinstance(value, str):
            return masker(value)

    if isinstance(value, Mapping):
        return {k: redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """对自由文本做模式级脱敏，兜住没有字段名的场景。"""
    text = _PHONE_RE.sub(lambda m: m.group(1) + "****" + m.group(2), text)
    text = _BANK_RE.sub(lambda m: m.group(1) + "******" + m.group(2), text)
    return text


class RedactingFilter(logging.Filter):
    """挂在 handler 上即可全局生效：格式化前抹掉隐私。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, Mapping):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple) and record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        return True


def setup_logging(level: str = "INFO", *, log_file: str | None = None) -> logging.Logger:
    """建立带脱敏的根日志器。log_file 非空时同时落文件（日志存储）。"""
    logger = logging.getLogger("aibutler")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        logger.addHandler(h)
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"aibutler.{name}")
