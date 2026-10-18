"""P0-1 账号与会话安全原语。

计划书依据
----------
- 三、P0-1「用户账号密码加盐哈希存储，数据库不保存明文密码；
  手机号、位置、支付信息等隐私字段独立加密存储」
- 三、P0-1「会话管理中间件：会话 Token 生成、校验、自动刷新；设置会话有效期，
  长时间无交互会话自动失效；失效后客户端提示重新登录；支持单账号多端登录、强制踢下线」
- 三、P0-1「日志脱敏规则：日志禁止打印明文手机号、密码、银行卡、完整位置」
- 七、验收标准 9「登录账号密码加盐哈希存储，数据库无明文密码；会话 Token 超时自动失效」

文档未定义项（禁止 AI 自行实现）
------------------------------
【文档未定义项 #12】会话有效期与「长时间无交互」阈值未给出数值
    → 数值一律由 Settings 强制要求，本模块不设默认。
【文档未定义项 #13】「加盐哈希」未指定算法与代价因子
    → 本模块固定使用 bcrypt（标准加盐实现）；代价因子属实现参数。
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

#: bcrypt 输入上限 72 字节；先做 SHA-256 再 base64（44 字节）以支持任意长度口令。
#: 属实现细节，不改变计划书「加盐哈希、不存明文」的语义。
BCRYPT_ROUNDS = 12


# --------------------------------------------------------------------------- #
# 口令加盐哈希
# --------------------------------------------------------------------------- #
def _prehash(plain: str) -> bytes:
    return base64.b64encode(hashlib.sha256(plain.encode("utf-8")).digest())


def hash_password(plain: str, *, rounds: int = BCRYPT_ROUNDS) -> str:
    """加盐哈希。计划书：数据库不保存明文密码。"""
    if not plain:
        raise ValueError("密码不得为空")
    return bcrypt.hashpw(_prehash(plain), bcrypt.gensalt(rounds)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(_prehash(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# 隐私字段独立加密存储（三、P0-1）
# --------------------------------------------------------------------------- #
class FieldCipher:
    """对称加密隐私字段：手机号、位置、支付信息等独立加密存储。

    密钥来自 FIELD_ENCRYPTION_KEY（Fernet 32 字节 urlsafe base64）。
    密文自带随机 IV，同一明文两次加密结果不同。
    """

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("FIELD_ENCRYPTION_KEY 未配置，隐私字段无法加密存储")
        self._f = Fernet(key.encode("ascii"))

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._f.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:  # 密钥不符或密文被篡改
            raise ValueError("隐私字段解密失败：密钥不匹配或密文已被篡改") from exc


# --------------------------------------------------------------------------- #
# 日志脱敏（三、P0-1「日志脱敏规则」）
# --------------------------------------------------------------------------- #
def mask_phone(value: str) -> str:
    return value[:3] + "****" + value[-4:] if len(value) >= 7 else "****"


def mask_account(value: str) -> str:
    return value[:2] + "***" + value[-2:] if len(value) >= 5 else "***"


def mask_bank_card(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    return "**** **** **** " + digits[-4:] if len(digits) >= 4 else "****"


def mask_location(value: str) -> str:
    """完整位置禁止入日志（计划书三、P0-1）。

    仅保留到行政区划粒度：命中「市 / 省」即截断，否则退化为前 3 个字符。
    注意：中文地址不能按固定字符数截断——「北京市朝阳区建国路88号」共 11 字，
    按 12 字符截断等于完全没有脱敏。
    """
    if not value:
        return ""
    text = value.strip().split(" ")[0]
    for index, ch in enumerate(text[:6]):
        if ch in ("市", "省"):
            return text[: index + 1] + "…"
    return text[:3] + "…"


# --------------------------------------------------------------------------- #
# 会话 Token
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionToken:
    token: str
    jti: str
    user_id: str
    device_id: str
    issued_at: int
    expires_at: int


def create_session_token(
    *, user_id: str, device_id: str, secret: str, ttl_seconds: int, now: int | None = None
) -> SessionToken:
    """会话 Token 生成。exp 取会话有效期（计划书未定义该项数值 → 由配置强制提供）。"""
    if not user_id:
        raise ValueError("user_id 不得为空：全部业务请求强制携带用户身份")
    if not device_id:
        raise ValueError("device_id 不得为空：支持单账号多端登录需按设备区分")
    issued = int(now if now is not None else time.time())
    expires = issued + int(ttl_seconds)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": user_id,
        "jti": jti,
        "dev": device_id,
        "iat": issued,
        "exp": expires,
        "typ": "session",
    }
    return SessionToken(
        token=jwt.encode(payload, secret, algorithm="HS256"),
        jti=jti,
        user_id=user_id,
        device_id=device_id,
        issued_at=issued,
        expires_at=expires,
    )


class SessionTokenError(Exception):
    """会话 Token 校验失败；调用方必须直接拒绝请求。"""


def decode_session_token(token: str, *, secret: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise SessionTokenError("会话已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise SessionTokenError("会话凭证无效，请重新登录") from exc
    if claims.get("typ") != "session":
        raise SessionTokenError("会话凭证类型不合法")
    return claims
