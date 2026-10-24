"""账号体系：手机号/邮箱标识、PII 加密、昵称、密码重置。

计划书依据与裁定
----------------
* 三、P0-1「账号密码加盐哈希存储」「隐私字段独立加密」「日志脱敏规则」
* **U67**：手机号与邮箱二选一作账号；对外只暴露 opaque `user_id`；
  手机号/邮箱加密落盘 + HMAC 摘要列用于查重与登录查找；接口只回脱敏值
* **U68**：忘记密码走一次性令牌（只存摘要、TTL、用后即焚）+ 可插拔投递通道，
  开发通道**在生产环境拒绝启动**
* **U69**：昵称可自定义也可随机刷新，不做全局唯一

为什么"加密列 + 摘要列"两列都要
--------------------------------
只用加密：密文每次不同（随机 IV），没法 `WHERE ident = ?` 查人 —— 登录直接不可用。
只用摘要：HMAC 虽然不可逆，但没有密钥就不可解密，运维申诉时无法核对。
两者并存，**查询走摘要、还原走密文**，各自只承担一件事。

为什么明文一律不入库、也不回传
--------------------------------
U67 的红线。落盘只有密文与摘要；接口只有 `138****8000`。
即使本人来问，也拿不回完整号码 —— 那是"再发一条短信验证"该做的事，
不是"把库里的明文吐回来"。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ai_butler.core.security import (
    FieldCipher,
    hash_password,
    mask_phone,
    verify_password,
)

# --------------------------------------------------------------------------- #
# 常量（U67 / U68 / U69 的实现参数）
# --------------------------------------------------------------------------- #

#: 连续失败多少次锁定
MAX_FAILED_ATTEMPTS = 5
#: 锁定时长
LOCK_MINUTES = 15
#: 重置令牌有效期
RESET_TTL_MINUTES = 15
#: 密码最短长度（与既有 UserRegistry 一致）
MIN_PASSWORD_LENGTH = 8
#: 昵称长度下限 / 上限（按**近似字素**计，见 grapheme_length）
NICKNAME_MIN = 2
NICKNAME_MAX = 16

PHONE = "phone"
EMAIL = "email"
IDENT_KINDS = (PHONE, EMAIL)

#: 中国大陆手机号：1 开头，第二位 3-9，共 11 位
_PHONE_CN = re.compile(r"^1[3-9]\d{9}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
#: 控制字符、零宽字符、方向控制符 —— 用它们能做出"看起来一样"的昵称（与 U69 的防空子同源）
_INVISIBLE = re.compile(r"[\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


class AccountError(ValueError):
    """账号相关错误。`code` 供接口层稳定映射状态码与文案。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 标识归一化：同一手机号/邮箱只允许一种写法进库
# --------------------------------------------------------------------------- #
def normalize_phone(raw: str) -> str:
    """归一化为 E.164（默认 +86）。只接受中国大陆号码。

    不接受 `+1` 等其他区号：本项目没有国际短信通道，
    收下也发不出验证码 —— 那只会把"注册成功但永远收不到码"变成静默故障。
    """
    text = str(raw or "").strip().replace(" ", "").replace("-", "")
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("0086"):
        text = text[4:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    if not _PHONE_CN.match(text):
        raise AccountError("invalid_phone", "手机号格式不正确（需中国大陆 11 位号码）")
    return "+86" + text


def normalize_email(raw: str) -> str:
    text = str(raw or "").strip()
    if not _EMAIL.match(text):
        raise AccountError("invalid_email", "邮箱格式不正确")
    # 域名大小写不敏感；本地部分按 RFC 5321 大小写敏感，但现实中几乎没人依赖，
    # 这里统一小写并在文档中写明，避免"注册时大写、登录时小写"这种必然踩的坑
    return text.lower()


def normalize_identifier(kind: str, raw: str) -> str:
    if kind == PHONE:
        return normalize_phone(raw)
    if kind == EMAIL:
        return normalize_email(raw)
    raise AccountError("invalid_kind", f"不支持的账号类型：{kind!r}（只支持 phone / email）")


def mask_identifier(kind: str, normalized: str) -> str:
    """脱敏展示。手机号 138****8000；邮箱 a***@example.com（保域名，便于本人确认）。"""
    if kind == PHONE:
        digits = normalized.replace("+86", "")
        return mask_phone(digits)
    if kind == EMAIL:
        local, _, domain = normalized.partition("@")
        head = local[:1] if local else ""
        return f"{head}***@{domain}"
    return "***"


# --------------------------------------------------------------------------- #
# 昵称
# --------------------------------------------------------------------------- #
#: 随机昵称词表（U69：自拟，无外部来源）
_ADJECTIVES = (
    "安静的", "明亮的", "早起的", "慢吞吞的", "会打盹的", "爱走神的",
    "胆小的", "好奇心重的", "讲道理的", "不认路的", "爱整理的", "吃得很少的",
)
_NOUNS = (
    "松鼠", "鲸鱼", "布谷鸟", "水獭", "刺猬", "猫头鹰",
    "苔藓", "海雾", "旧书页", "雨后的土", "半夜的灯", "第二杯茶",
)


def grapheme_length(text: str) -> tuple[int, bool]:
    """近似字素簇计数，返回 `(长度, 是否精确)`。

    为什么不用 `len()`：`len("👨‍👩‍👧")` 是 5，用户看到的是 1 个。
    为什么不用 `regex` 的 `\\X`：那要多装一个依赖，只为数个数不值得。
    折中做法是把组合符号、变体选择符、ZWJ 连接序列并入前一个字素 ——
    对常见 emoji 与带声调拉丁字母是准的；**对复杂的肤色/家族组合仍可能多算**，
    所以第二个返回值如实告诉调用方"这次是近似值"。

    不做"估个大概就算精确"的假精确：长度校验失败时，宁可提示按近似值算的。
    """
    if not text:
        return 0, True
    count = 0
    previous_was_joiner = False
    for ch in text:
        if ch == "\u200d":                      # ZWJ：连接符本身不占位
            previous_was_joiner = True
            continue
        is_combining = unicodedata.combining(ch) != 0
        is_variation = ch in ("\ufe0e", "\ufe0f")
        if is_combining or is_variation or previous_was_joiner:
            previous_was_joiner = False         # 并入前一个字素
            continue
        count += 1
        previous_was_joiner = False
    # 含 ZWJ 或组合符的串，计数可能与真实字素数有出入
    approximate = ("\u200d" in text) or any(unicodedata.combining(c) for c in text)
    return max(count, 1), not approximate


def validate_nickname(raw: str) -> str:
    """校验并返回去空白后的昵称。

    拒绝的几类都有具体理由，不是"看着不顺眼"：
    * 空白/空串：展示时是空的，列表里会出现无主行；
    * 控制符与零宽字符：能做出视觉相同、字节不同的昵称，用于冒充（U69）；
    * 含 `@` 或连续 6 位以上数字：昵称会被当成账号标识，等于变相泄漏 PII。
    """
    text = str(raw or "").strip()
    if not text:
        raise AccountError("invalid_nickname", "昵称不能为空")
    if _INVISIBLE.search(text):
        raise AccountError("invalid_nickname", "昵称不能包含不可见字符")
    if "@" in text:
        raise AccountError("invalid_nickname", "昵称不能包含 @（避免被误认为邮箱）")
    if re.search(r"\d{6,}", text):
        raise AccountError("invalid_nickname", "昵称不能包含连续 6 位以上数字（避免被误认为手机号）")
    length, _exact = grapheme_length(text)
    if length < NICKNAME_MIN or length > NICKNAME_MAX:
        raise AccountError(
            "invalid_nickname", f"昵称长度需 {NICKNAME_MIN}–{NICKNAME_MAX} 个字"
        )
    return text


def random_nickname(*, chooser: Callable[[Iterable[str]], str] | None = None) -> str:
    """随机拼一个昵称。`chooser` 可注入，测试里就能给出确定结果。"""
    pick = chooser or (lambda seq: secrets.choice(tuple(seq)))
    return validate_nickname(f"{pick(_ADJECTIVES)}{pick(_NOUNS)}")


# --------------------------------------------------------------------------- #
# 账号实体
# --------------------------------------------------------------------------- #
@dataclass
class Account:
    user_id: str
    ident_kind: str
    ident_digest: str          # HMAC-SHA256，可查不可逆
    ident_cipher: str          # Fernet 密文，可还原但不可查
    password_hash: str
    nickname: str
    created_at: str
    updated_at: str
    failed_attempts: int = 0
    locked_until: str = ""
    prefs: list[str] = field(default_factory=list)
    nickname_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "ident_kind": self.ident_kind,
            "ident_digest": self.ident_digest,
            "ident_cipher": self.ident_cipher,
            "password_hash": self.password_hash,
            "nickname": self.nickname,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failed_attempts": self.failed_attempts,
            "locked_until": self.locked_until,
            "prefs": list(self.prefs),
            "nickname_history": list(self.nickname_history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return cls(
            user_id=str(data["user_id"]),
            ident_kind=str(data["ident_kind"]),
            ident_digest=str(data["ident_digest"]),
            ident_cipher=str(data["ident_cipher"]),
            password_hash=str(data["password_hash"]),
            nickname=str(data.get("nickname", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            failed_attempts=int(data.get("failed_attempts", 0)),
            locked_until=str(data.get("locked_until", "")),
            prefs=[str(x) for x in data.get("prefs", [])],
            nickname_history=[str(x) for x in data.get("nickname_history", [])],
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 投递通道（U68）
# --------------------------------------------------------------------------- #
class ResetChannel:
    """重置令牌的投递通道端口。真实实现应是短信 / 邮件服务。"""

    name = "abstract"
    prod_safe = False          # 默认不安全：子类必须显式声明

    def deliver(self, *, kind: str, masked: str, token: str,
                destination: str = "") -> None:
        """投递。`destination` 是**真实收件标识**（邮箱地址），只有通道自己看。

        `masked` 只用于日志与文案：通道实现**不许**把 `destination` 打进日志，
        否则前面那套「落盘只有密文」的努力会从日志漏出去。
        """
        raise NotImplementedError

    def dev_token_visible(self) -> bool:
        """该通道是否允许把令牌直接放进接口响应。仅开发通道为 True。"""
        return False


class DevChannel(ResetChannel):
    """开发通道：把令牌写进服务端日志，并允许响应回显。

    **生产环境禁止使用**：`accounts.build_channel()` 会直接拒绝启动。
    这条硬边界的意义在于 —— "本地收不到短信所以先回显一下"这种便利，
    一旦跟着代码上线，就等于任何人输入任意手机号都能拿到重置令牌。
    """

    name = "dev"
    prod_safe = False

    def __init__(self, sink: Callable[[str], None] | None = None) -> None:
        self._lines: list[str] = []
        self._sink = sink

    def deliver(self, *, kind: str, masked: str, token: str,
                destination: str = "") -> None:
        line = f"[reset-channel:dev] 给 {masked} 的重置令牌：{token}"
        self._lines.append(line)
        if self._sink:
            self._sink(line)

    def dev_token_visible(self) -> bool:
        return True

    @property
    def outbox(self) -> tuple[str, ...]:
        return tuple(self._lines)


class ConsoleChannel(ResetChannel):
    """把令牌打印到 stderr 的通道，供自建部署时人工转发。

    与 DevChannel 的区别：**不把令牌放进接口响应**，因此即便被误用，
    攻击者也拿不到令牌（他读不到服务端 stdout）。但仍不是生产级方案 ——
    没有审计、没有重试、没有送达确认，所以 `prod_safe` 依旧为 False。
    """

    name = "console"
    prod_safe = False

    def __init__(self, print_fn: Callable[[str], None] | None = None) -> None:
        self._print = print_fn or (lambda text: print(text, flush=True))

    def deliver(self, *, kind: str, masked: str, token: str,
                destination: str = "") -> None:
        self._print(f"[reset-channel:console] {masked} -> {token}")


#: `build_channel` 认的 smtp 配置键。缺任何一个都建不起来（fail-closed）。
SMTP_REQUIRED_KEYS: tuple[str, ...] = ("host", "port", "sender")


class SmtpChannel(ResetChannel):
    """真投递：走 SMTP 把重置令牌发到注册邮箱（U68 的生产通道）。

    这条通道是**唯一**允许在 production 使用的通道 —— 它不把令牌写进接口响应，
    也不写日志，只发到账号本人的邮箱。

    三条刻意为之的行为
    ------------------
    1. **发送失败不抛给调用方**：抛了的话，「账号存在 + 投递失败」与
       「账号不存在」的响应就不一样了，等于把不可枚举撕开一个口子。
       失败记进 `failures` 并由运维从日志里看。
    2. **手机号账号不硬发**：没邮箱就发不了，记一条 skip，不静默假装发过。
    3. **日志里只有掩码**：`destination` 只出现在收件人字段，不进日志。

    依赖是标准库 `smtplib` —— 不引第三方 SDK，是因为发一封重置邮件
    不该把整个项目绑到某家邮件服务商上。
    """

    name = "smtp"
    prod_safe = True

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        use_ssl: bool = True,
        timeout: float = 15.0,
        smtp_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._host = str(host).strip()
        self._port = int(port)
        self._sender = str(sender).strip()
        self._username = str(username).strip()
        self._password = password
        self._use_ssl = bool(use_ssl)
        self._timeout = timeout
        self._smtp_factory = smtp_factory
        #: 投递失败/跳过的记录（供运维排查；接口层拿不到，避免形成枚举口子）
        self.failures: list[str] = []
        self.sent: list[str] = []

    # ---------- 内部 ---------- #
    def _connect(self):
        import smtplib

        factory = self._smtp_factory
        if factory is not None:
            return factory(self._host, self._port, timeout=self._timeout)
        if self._use_ssl:
            return smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout)
        client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        client.starttls()
        return client

    def _compose(self, masked: str, token: str) -> str:
        minutes = RESET_TTL_MINUTES
        return (
            "Subject: =?utf-8?b?"  # noqa: E501 —— 中文主题必须编码，否则某些客户端显示乱码
            + _b64("AI 管家：重置密码")
            + "?=\r\n"
            "From: " + self._sender + "\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "MIME-Version: 1.0\r\n"
            "\r\n"
            f"你正在重置 {masked} 的登录密码。\r\n"
            f"重置码：{token}\r\n"
            f"{minutes} 分钟内有效，只能使用一次。\r\n"
            "不是你本人操作？忽略本邮件即可，密码不会被改动。\r\n"
        )

    # ---------- 端口 ---------- #
    def deliver(self, *, kind: str, masked: str, token: str,
                destination: str = "") -> None:
        if kind != "email" or "@" not in str(destination):
            # 手机号账号没有收件地址：如实记一笔，不假装发过
            self.failures.append(f"{masked} 没有可用邮箱，smtp 通道无法投递")
            return
        try:
            client = self._connect()
            try:
                if self._username:
                    client.login(self._username, self._password)
                client.sendmail(self._sender, [destination], self._compose(masked, token))
            finally:
                try:
                    client.quit()
                except Exception:      # noqa: BLE001 —— 关连接失败不影响投递结论
                    pass
        except Exception as exc:       # noqa: BLE001 —— 见类注释第 1 条
            self.failures.append(f"{masked} 投递失败：{type(exc).__name__}")
            return
        self.sent.append(masked)


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def build_channel(name: str, *, environment: str,
                  smtp: dict[str, Any] | None = None) -> ResetChannel:
    """按名字建通道，并在生产环境下拒绝不安全的通道（fail-closed）。"""
    key = str(name or "dev").strip().lower()
    if environment == "production" and key != "smtp":
        raise AccountError(
            "unsafe_reset_channel",
            f"生产环境不允许使用 {key!r} 重置通道：它会把令牌暴露给调用方或日志。"
            "请配置真实短信/邮件通道后再上线。",
        )
    if key == "console":
        return ConsoleChannel()
    if key == "smtp":
        cfg = dict(smtp or {})
        missing = [k for k in SMTP_REQUIRED_KEYS
                   if not str(cfg.get(k) or "").strip()]
        if missing:
            raise AccountError(
                "channel_not_configured",
                "smtp 通道缺少配置：" + "、".join(missing)
                + "（需要 SMTP_HOST / SMTP_PORT / SMTP_SENDER，"
                "多数服务商还要 SMTP_USER / SMTP_PASSWORD）",
            )
        return SmtpChannel(
            host=str(cfg["host"]), port=int(cfg["port"]), sender=str(cfg["sender"]),
            username=str(cfg.get("username") or ""),
            password=str(cfg.get("password") or ""),
            use_ssl=bool(cfg.get("use_ssl", True)),
        )
    return DevChannel()


# --------------------------------------------------------------------------- #
# 账号存储
# --------------------------------------------------------------------------- #
class AccountStore:
    """账号存储。文件即真值源（与 users.py 的 U42 选择一致，便于将来换库）。"""

    def __init__(
        self,
        root: Path,
        *,
        cipher: FieldCipher,
        ident_secret: str,
        channel: ResetChannel | None = None,
        clock: Callable[[], datetime] = _now,
        nickname_chooser: Callable[[Iterable[str]], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.accounts_path = self.root / "accounts.json"
        self.resets_path = self.root / "resets.json"
        self._cipher = cipher
        if not ident_secret:
            raise AccountError("missing_secret", "账号摘要密钥未配置，无法安全存储账号标识")
        self._secret = ident_secret.encode("utf-8")
        self._channel = channel or DevChannel()
        self._clock = clock
        self._chooser = nickname_chooser
        self._throttle: dict[str, list[float]] = {}
        # 时序对齐用的假哈希：**构造时算一次**，不要在每次登录失败时现算。
        # bcrypt 12 轮约 250ms，若按次计算，未登录攻击者只要乱填手机号
        # 就能用每条请求换来 250ms 的 CPU —— 那等于自己给自己造了个 DoS 入口。
        self._dummy_hash = hash_password("timing-alignment-only")

    # ---------------- 底层读写 ---------------- #
    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _all(self) -> dict[str, Any]:
        return self._read(self.accounts_path)

    def _save_all(self, data: dict[str, Any]) -> None:
        self._write(self.accounts_path, data)

    # ---------------- 摘要 ---------------- #
    def digest(self, kind: str, normalized: str) -> str:
        """HMAC-SHA256。用 HMAC 而非裸 SHA256：裸哈希对 11 位手机号是可穷举的。"""
        message = f"{kind}:{normalized}".encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    # ---------------- 注册 / 查询 ---------------- #
    def register(
        self,
        *,
        kind: str,
        identifier: str,
        password: str,
        nickname: str | None = None,
        prefs: Iterable[str] | None = None,
    ) -> Account:
        """注册账号。

        `nickname` 的 **None 与 "" 不是一回事**：`None` 表示"没提供"，自动随机生成；
        `""` 表示"提供了但为空"，属于非法输入，直接报错。
        二者混同过一次：传空串被静默替换成随机昵称，用户以为自己设了个空名。
        """
        normalized = normalize_identifier(kind, identifier)
        self._check_password_strength(password)
        digest = self.digest(kind, normalized)
        data = self._all()
        if self._find_by_digest(data, digest) is not None:
            # 注册时**必须**告诉用户"已被占用"，否则他永远登不上也查不出原因。
            # 这与登录/重置的"不可枚举"不冲突：注册接口本来就暴露"该号是否已注册"，
            # 所以注册侧改用限流（见 app.py）而不是含糊其辞。
            raise AccountError("identifier_taken", "该手机号/邮箱已注册，请直接登录或找回密码")

        now = self._clock()
        user_id = "u_" + secrets.token_hex(8)
        account = Account(
            user_id=user_id,
            ident_kind=kind,
            ident_digest=digest,
            ident_cipher=self._cipher.encrypt(normalized),
            password_hash=hash_password(password),
            nickname=validate_nickname(nickname) if nickname is not None else self._new_nickname(),
            created_at=_iso(now),
            updated_at=_iso(now),
            prefs=[str(p).strip() for p in (prefs or []) if str(p).strip()],
        )
        data[user_id] = account.to_dict()
        self._save_all(data)
        return account

    def _find_by_digest(self, data: dict[str, Any], digest: str) -> Account | None:
        for raw in data.values():
            if raw.get("ident_digest") == digest:
                return Account.from_dict(raw)
        return None

    def find(self, *, kind: str, identifier: str) -> Account | None:
        normalized = normalize_identifier(kind, identifier)
        return self._find_by_digest(self._all(), self.digest(kind, normalized))

    def get(self, user_id: str) -> Account | None:
        raw = self._all().get(str(user_id))
        return Account.from_dict(raw) if raw else None

    def reveal_identifier(self, user_id: str) -> str:
        """**仅供服务端内部核对**（如申诉）。接口层绝不调用它。"""
        account = self.get(user_id)
        if account is None:
            raise AccountError("not_found", "账号不存在")
        return self._cipher.decrypt(account.ident_cipher)

    # ---------------- 口令 ---------------- #
    def _check_password_strength(self, password: str) -> None:
        if len(str(password or "")) < MIN_PASSWORD_LENGTH:
            raise AccountError(
                "weak_password", f"密码至少 {MIN_PASSWORD_LENGTH} 位"
            )

    def login(self, *, kind: str, identifier: str, password: str) -> Account:
        normalized = normalize_identifier(kind, identifier)
        digest = self.digest(kind, normalized)
        data = self._all()
        account = self._find_by_digest(data, digest)
        now = self._clock()

        if account is None:
            # 不存在也要走一次口令校验，避免"查得到才慢、查不到立刻返回"的时序侧信道。
            # 用**预生成**的假哈希，不在这里现算 bcrypt（见 __init__ 的说明）。
            verify_password(password, self._dummy_hash)
            raise AccountError("invalid_credentials", "账号或密码不正确")

        if account.locked_until:
            locked_until = datetime.fromisoformat(account.locked_until)
            if now < locked_until:
                raise AccountError(
                    "account_locked",
                    f"连续失败次数过多，请 {LOCK_MINUTES} 分钟后再试",
                )
            account.locked_until = ""
            account.failed_attempts = 0

        if not verify_password(password, account.password_hash):
            account.failed_attempts += 1
            if account.failed_attempts >= MAX_FAILED_ATTEMPTS:
                account.locked_until = _iso(now + timedelta(minutes=LOCK_MINUTES))
                account.failed_attempts = 0
            data[account.user_id] = account.to_dict()
            self._save_all(data)
            # 锁定与"密码错"用同一个 code 之外的文案区分是可以的（用户需要知道被锁），
            # 但**不区分账号是否存在** —— 那是给攻击者的信息
            raise AccountError("invalid_credentials", "账号或密码不正确")

        account.failed_attempts = 0
        account.locked_until = ""
        account.updated_at = _iso(now)
        data[account.user_id] = account.to_dict()
        self._save_all(data)
        return account

    def change_password(self, *, user_id: str, old_password: str, new_password: str) -> None:
        data = self._all()
        raw = data.get(str(user_id))
        if raw is None:
            raise AccountError("not_found", "账号不存在")
        account = Account.from_dict(raw)
        if not verify_password(old_password, account.password_hash):
            raise AccountError("invalid_credentials", "原密码不正确")
        self._check_password_strength(new_password)
        if verify_password(new_password, account.password_hash):
            raise AccountError("password_unchanged", "新密码不能与原密码相同")
        account.password_hash = hash_password(new_password)
        account.updated_at = _iso(self._clock())
        data[account.user_id] = account.to_dict()
        self._save_all(data)

    # ---------------- 昵称 ---------------- #
    def _new_nickname(self) -> str:
        return random_nickname(chooser=self._chooser)

    def set_nickname(self, *, user_id: str, nickname: str) -> Account:
        text = validate_nickname(nickname)
        data = self._all()
        raw = data.get(str(user_id))
        if raw is None:
            raise AccountError("not_found", "账号不存在")
        account = Account.from_dict(raw)
        if account.nickname == text:
            return account
        if account.nickname:
            account.nickname_history = (account.nickname_history + [account.nickname])[-10:]
        account.nickname = text
        account.updated_at = _iso(self._clock())
        data[account.user_id] = account.to_dict()
        self._save_all(data)
        return account

    def refresh_nickname(self, *, user_id: str) -> Account:
        """随机刷新昵称。带频控，防止把昵称当娱乐按钮刷爆审计。"""
        now = time.monotonic()
        recent = [t for t in self._throttle.get(f"nick:{user_id}", []) if now - t < 60.0]
        if len(recent) >= 3:
            raise AccountError("too_many_requests", "昵称刷新太频繁，请一分钟后再试")
        recent.append(now)
        self._throttle[f"nick:{user_id}"] = recent
        return self.set_nickname(user_id=user_id, nickname=self._new_nickname())

    def set_prefs(self, *, user_id: str, prefs: Iterable[str]) -> Account:
        data = self._all()
        raw = data.get(str(user_id))
        if raw is None:
            raise AccountError("not_found", "账号不存在")
        account = Account.from_dict(raw)
        account.prefs = [str(p).strip() for p in prefs if str(p).strip()][:20]
        account.updated_at = _iso(self._clock())
        data[account.user_id] = account.to_dict()
        self._save_all(data)
        return account

    # ---------------- 重置 ---------------- #
    def request_reset(self, *, kind: str, identifier: str) -> tuple[str | None, str]:
        """申请重置。返回 `(原始令牌 或 None, 脱敏标识)`。

        **无论账号是否存在，调用方拿到的响应必须一致** —— 令牌为 None 时
        也必须假装发过。这里连摘要计算都照做，让两条路径的耗时接近。
        """
        normalized = normalize_identifier(kind, identifier)
        digest = self.digest(kind, normalized)
        masked = mask_identifier(kind, normalized)
        account = self._find_by_digest(self._all(), digest)

        if account is None:
            # 仍然算一次摘要，避免"查不到就不算"带来的时序差
            hashlib.sha256(digest.encode("ascii")).hexdigest()
            return None, masked

        raw_token = secrets.token_urlsafe(32)
        token_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = self._clock()
        resets = self._read(self.resets_path)
        # 清掉过期项，避免文件无限增长（也顺手让"重放旧令牌"必然失败）
        resets = {
            k: v for k, v in resets.items()
            if datetime.fromisoformat(v["expires_at"]) > now
        }
        resets[token_digest] = {
            "user_id": account.user_id,
            "expires_at": _iso(now + timedelta(minutes=RESET_TTL_MINUTES)),
            "used_at": "",
        }
        self._write(self.resets_path, resets)
        # destination 是**还原出来的真实标识**，只有通道自己会用；
        # 日志与接口一律只走 masked（U67 红线）。
        self._channel.deliver(kind=kind, masked=masked, token=raw_token,
                              destination=normalized)
        return raw_token, masked

    def reset_password(self, *, token: str, new_password: str) -> Account:
        """用令牌设置新密码。令牌用后即焚，且一次性。"""
        token_digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        resets = self._read(self.resets_path)
        record = resets.get(token_digest)
        now = self._clock()
        if record is None:
            raise AccountError("invalid_token", "重置链接无效或已使用")
        if record.get("used_at"):
            raise AccountError("invalid_token", "重置链接无效或已使用")
        if datetime.fromisoformat(record["expires_at"]) <= now:
            raise AccountError("expired_token", "重置链接已过期，请重新申请")

        self._check_password_strength(new_password)
        data = self._all()
        raw = data.get(record["user_id"])
        if raw is None:
            raise AccountError("not_found", "账号不存在")
        account = Account.from_dict(raw)
        if verify_password(new_password, account.password_hash):
            raise AccountError("password_unchanged", "新密码不能与原密码相同")
        account.password_hash = hash_password(new_password)
        account.failed_attempts = 0
        account.locked_until = ""
        account.updated_at = _iso(now)
        data[account.user_id] = account.to_dict()
        self._save_all(data)

        record["used_at"] = _iso(now)
        resets[token_digest] = record
        self._write(self.resets_path, resets)
        return account

    # ---------------- 展示 ---------------- #
    def public_profile(self, user_id: str, *, dev_token: str | None = None) -> dict[str, Any]:
        """对外视图：**只有脱敏标识**，没有密文、没有摘要、没有口令哈希。"""
        account = self.get(user_id)
        if account is None:
            raise AccountError("not_found", "账号不存在")
        identifier = self._cipher.decrypt(account.ident_cipher)
        payload: dict[str, Any] = {
            "user_id": account.user_id,
            "ident_kind": account.ident_kind,
            "ident_masked": mask_identifier(account.ident_kind, identifier),
            "nickname": account.nickname,
            "prefs": list(account.prefs),
            "created_at": account.created_at,
        }
        if dev_token:
            payload["dev_token"] = dev_token
        return payload
