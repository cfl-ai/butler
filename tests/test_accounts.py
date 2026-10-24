"""账号体系：手机号/邮箱标识、PII 加密、昵称、密码重置（U67 / U68 / U69）。

这里的断言都对着**可泄漏点**写，而不是对着"功能能跑"写：

* 明文手机号/邮箱/口令不得出现在存储文件里（U67 红线）
* 对外视图只有脱敏值，且**本人也拿不回完整标识**
* 重置令牌只存摘要、一次性、过期即废（U68）
* 开发通道在生产环境必须拒绝启动
* 登录锁定生效，且"账号不存在"与"密码错"对外不可区分（防枚举）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_butler.core import accounts as a
from ai_butler.core.security import FieldCipher

#: 测试用标识。刻意用 138-0000-xxxx 这类**保留示例号段**，避免和真实号码撞车。
PHONE_A = "13800000001"
PHONE_B = "13800000002"
EMAIL_A = "alice@example.com"


@pytest.fixture()
def store(tmp_path: Path) -> a.AccountStore:
    cipher = FieldCipher(FieldCipher.generate_key())
    return a.AccountStore(
        tmp_path,
        cipher=cipher,
        ident_secret="test-ident-secret",
        channel=a.DevChannel(),
        nickname_chooser=lambda seq: tuple(seq)[0],
    )


# --------------------------------------------------------------------------- #
# 归一化与校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("13800000001", "+8613800000001"),
        ("+8613800000001", "+8613800000001"),
        ("+86 138 0000 0001", "+8613800000001"),
        ("138-0000-0001", "+8613800000001"),
        ("008613800000001", "+8613800000001"),
        ("8613800000001", "+8613800000001"),
    ],
)
def test_phone_is_normalized_to_e164(raw: str, expected: str) -> None:
    """同一号码的不同写法必须归一到同一个值，否则会出现"注册了却登不上"。"""
    assert a.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["12345", "12800000001", "1380000000", "138000000012", ""])
def test_invalid_phone_is_rejected(raw: str) -> None:
    with pytest.raises(a.AccountError) as err:
        a.normalize_phone(raw)
    assert err.value.code == "invalid_phone"


def test_email_is_lowercased_but_still_validated() -> None:
    assert a.normalize_email("  Alice@Example.COM ") == "alice@example.com"
    for bad in ("alice", "alice@", "@example.com", "alice@example", "a b@example.com"):
        with pytest.raises(a.AccountError):
            a.normalize_email(bad)


def test_unknown_identifier_kind_is_rejected() -> None:
    with pytest.raises(a.AccountError) as err:
        a.normalize_identifier("wechat", "whatever")
    assert err.value.code == "invalid_kind"


def test_mask_keeps_enough_to_recognize_but_not_enough_to_reuse() -> None:
    assert a.mask_identifier(a.PHONE, "+8613800000001") == "138****0001"
    assert a.mask_identifier(a.EMAIL, "alice@example.com") == "a***@example.com"


# --------------------------------------------------------------------------- #
# 注册：明文不落盘
# --------------------------------------------------------------------------- #
def test_registration_never_writes_plaintext_identifier_or_password(
    store: a.AccountStore, tmp_path: Path
) -> None:
    """U67 的核心红线：落盘只有密文与摘要。"""
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")

    blob = (tmp_path / "accounts.json").read_text(encoding="utf-8")
    assert PHONE_A not in blob, "明文手机号被写进了账号文件"
    assert "8613800000001" not in blob, "手机号只去掉了符号、仍然明文可读"
    assert "Demo-Passw0rd" not in blob, "明文口令被写进了账号文件"

    record = next(iter(json.loads(blob).values()))
    assert record["ident_cipher"] and record["ident_digest"]
    # 密文可解、摘要不可逆 —— 两者职责不同，缺一不可
    assert record["ident_cipher"] != record["ident_digest"]


def test_public_profile_exposes_only_masked_identifier(store: a.AccountStore) -> None:
    account = store.register(kind=a.EMAIL, identifier=EMAIL_A, password="Demo-Passw0rd")
    profile = store.public_profile(account.user_id)

    assert profile["ident_masked"] == "a***@example.com"
    assert EMAIL_A not in json.dumps(profile, ensure_ascii=False)
    # 三个内部字段一个都不能漏出去
    for leaked in ("ident_cipher", "ident_digest", "password_hash"):
        assert leaked not in profile


def test_duplicate_identifier_is_rejected(store: a.AccountStore) -> None:
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    with pytest.raises(a.AccountError) as err:
        # 换个写法也必须撞上
        store.register(kind=a.PHONE, identifier="+86 138-0000-0001", password="Demo-Passw0rd")
    assert err.value.code == "identifier_taken"


def test_weak_password_is_rejected(store: a.AccountStore) -> None:
    with pytest.raises(a.AccountError) as err:
        store.register(kind=a.PHONE, identifier=PHONE_A, password="short")
    assert err.value.code == "weak_password"


def test_login_accepts_equivalent_phone_spellings(store: a.AccountStore) -> None:
    account = store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    same = store.login(kind=a.PHONE, identifier="+86 138 0000 0001", password="Demo-Passw0rd")
    assert same.user_id == account.user_id


# --------------------------------------------------------------------------- #
# 登录失败：不可枚举 + 锁定
# --------------------------------------------------------------------------- #
def test_wrong_password_and_unknown_account_are_indistinguishable(
    store: a.AccountStore,
) -> None:
    """两条路径的 code 与文案必须一致，否则等于给攻击者一个账号探测接口。"""
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")

    with pytest.raises(a.AccountError) as wrong:
        store.login(kind=a.PHONE, identifier=PHONE_A, password="Not-The-Passw0rd")
    with pytest.raises(a.AccountError) as unknown:
        store.login(kind=a.PHONE, identifier=PHONE_B, password="Not-The-Passw0rd")

    assert wrong.value.code == unknown.value.code == "invalid_credentials"
    assert wrong.value.message == unknown.value.message


def test_account_locks_after_repeated_failures(store: a.AccountStore) -> None:
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    for _ in range(a.MAX_FAILED_ATTEMPTS):
        with pytest.raises(a.AccountError):
            store.login(kind=a.PHONE, identifier=PHONE_A, password="Not-The-Passw0rd")

    with pytest.raises(a.AccountError) as err:
        # 连正确口令也进不去 —— 锁定必须优先于口令校验
        store.login(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    assert err.value.code == "account_locked"


def test_lock_expires(tmp_path: Path) -> None:
    """时钟可注入，才能测"锁 15 分钟后自动解开"而不是靠 sleep。"""
    moment = {"now": datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)}
    cipher = FieldCipher(FieldCipher.generate_key())
    store = a.AccountStore(
        tmp_path, cipher=cipher, ident_secret="s", clock=lambda: moment["now"]
    )
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    for _ in range(a.MAX_FAILED_ATTEMPTS):
        with pytest.raises(a.AccountError):
            store.login(kind=a.PHONE, identifier=PHONE_A, password="Not-The-Passw0rd")

    moment["now"] += timedelta(minutes=a.LOCK_MINUTES + 1)
    assert store.login(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd").user_id


# --------------------------------------------------------------------------- #
# 密码重置（U68）
# --------------------------------------------------------------------------- #
def test_reset_token_is_stored_only_as_digest(store: a.AccountStore, tmp_path: Path) -> None:
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    token, masked = store.request_reset(kind=a.PHONE, identifier=PHONE_A)

    assert token and masked == "138****0001"
    blob = (tmp_path / "resets.json").read_text(encoding="utf-8")
    assert token not in blob, "重置令牌被明文存进了文件 —— 拿到文件就等于能改任何人的密码"


def test_reset_changes_password_and_burns_token(store: a.AccountStore) -> None:
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    token, _ = store.request_reset(kind=a.PHONE, identifier=PHONE_A)
    assert token

    store.reset_password(token=token, new_password="Brand-New-Passw0rd")
    assert store.login(kind=a.PHONE, identifier=PHONE_A, password="Brand-New-Passw0rd")

    with pytest.raises(a.AccountError) as err:
        store.reset_password(token=token, new_password="Yet-Another-Passw0rd")
    assert err.value.code == "invalid_token", "同一令牌必须只能用一次"


def test_reset_rejects_unknown_account_but_response_is_unchanged(
    store: a.AccountStore,
) -> None:
    token, masked = store.request_reset(kind=a.PHONE, identifier=PHONE_B)
    # 令牌为 None（没这个人），但脱敏值是照常算出来的 —— 调用方据此返回同一句话
    assert token is None
    assert masked == "138****0002"


def test_reset_token_expires(tmp_path: Path) -> None:
    moment = {"now": datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)}
    cipher = FieldCipher(FieldCipher.generate_key())
    store = a.AccountStore(
        tmp_path, cipher=cipher, ident_secret="s", clock=lambda: moment["now"]
    )
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    token, _ = store.request_reset(kind=a.PHONE, identifier=PHONE_A)
    moment["now"] += timedelta(minutes=a.RESET_TTL_MINUTES + 1)
    with pytest.raises(a.AccountError) as err:
        store.reset_password(token=token or "", new_password="Brand-New-Passw0rd")
    assert err.value.code == "expired_token"


def test_reset_rejects_same_password(store: a.AccountStore) -> None:
    """「重置了个寂寞」：新密码与旧密码相同时必须拒绝，否则用户以为改过了。"""
    store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    token, _ = store.request_reset(kind=a.PHONE, identifier=PHONE_A)
    with pytest.raises(a.AccountError) as err:
        store.reset_password(token=token or "", new_password="Demo-Passw0rd")
    assert err.value.code == "password_unchanged"


def test_change_password_requires_old_password(store: a.AccountStore) -> None:
    account = store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    with pytest.raises(a.AccountError) as err:
        store.change_password(
            user_id=account.user_id, old_password="wrong-old", new_password="Brand-New-Passw0rd"
        )
    assert err.value.code == "invalid_credentials"
    store.change_password(
        user_id=account.user_id, old_password="Demo-Passw0rd", new_password="Brand-New-Passw0rd"
    )
    assert store.login(kind=a.PHONE, identifier=PHONE_A, password="Brand-New-Passw0rd")


# --------------------------------------------------------------------------- #
# 通道安全边界
# --------------------------------------------------------------------------- #
def test_dev_channel_is_refused_in_production() -> None:
    """这条是整个 U68 的护栏：开发通道上线 = 任何人输入手机号就能拿重置令牌。"""
    with pytest.raises(a.AccountError) as err:
        a.build_channel("dev", environment="production")
    assert err.value.code == "unsafe_reset_channel"

    with pytest.raises(a.AccountError):
        a.build_channel("console", environment="production")


def test_smtp_channel_without_configuration_says_which_key_is_missing() -> None:
    """缺配置不许"半开"：报出**缺的是哪一个键**，运维才能一次配完。"""
    with pytest.raises(a.AccountError) as err:
        a.build_channel("smtp", environment="production")
    assert err.value.code == "channel_not_configured"
    assert "host" in err.value.message and "port" in err.value.message

    with pytest.raises(a.AccountError):
        # 只补一半（有 host 没 port/sender）同样不许建起来
        a.build_channel("smtp", environment="production",
                        smtp={"host": "smtp.example.com"})


def test_smtp_channel_is_prod_safe_and_carries_config() -> None:
    channel = a.build_channel(
        "smtp", environment="production",
        smtp={"host": "smtp.example.com", "port": 465,
              "sender": "noreply@example.com", "username": "u", "password": "p"})
    assert channel.name == "smtp" and channel.prod_safe is True
    assert channel.dev_token_visible() is False


class _FakeSmtp:
    """假的 SMTP 客户端：记录发出去的信，不碰网络。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, list[str], str]] = []
        self.logged_in = False
        self.quit_called = False

    def login(self, user: str, password: str) -> None:  # noqa: ARG002
        self.logged_in = True

    def sendmail(self, sender: str, to: list[str], body: str) -> None:
        if self.fail:
            raise OSError("smtp down")
        self.sent.append((sender, to, body))

    def quit(self) -> None:
        self.quit_called = True


def test_smtp_delivers_to_the_real_address_but_only_logs_the_mask() -> None:
    fake = _FakeSmtp()
    channel = a.SmtpChannel(host="h", port=465, sender="noreply@example.com",
                            username="u", password="p",
                            smtp_factory=lambda *args, **kw: fake)
    channel.deliver(kind="email", masked="a***@example.com",
                    token="TOKEN-123", destination=EMAIL_A)
    assert fake.logged_in and fake.quit_called
    sender, to, body = fake.sent[0]
    assert sender == "noreply@example.com"
    assert to == [EMAIL_A]                       # 收件人必须是真地址，否则发不出去
    assert "TOKEN-123" in body and "a***@example.com" in body
    assert "Subject:" in body                     # 主题编码过，客户端才不会乱码
    assert channel.sent == ["a***@example.com"]   # 记录里只有掩码


def test_smtp_does_not_pretend_to_send_to_a_phone_account() -> None:
    """手机号账号没有收件地址：记一条 skip，**不许**假装发过。"""
    fake = _FakeSmtp()
    channel = a.SmtpChannel(host="h", port=465, sender="s@example.com",
                            smtp_factory=lambda *args, **kw: fake)
    channel.deliver(kind="phone", masked="138****0001", token="T",
                    destination="+8613800000001")
    assert fake.sent == []
    assert channel.failures and "没有可用邮箱" in channel.failures[0]


def test_smtp_delivery_failure_does_not_raise() -> None:
    """投递失败**不抛给调用方** —— 抛了就成了枚举口子（存在的号才可能投递失败）。"""
    channel = a.SmtpChannel(host="h", port=465, sender="s@example.com",
                            smtp_factory=lambda *args, **kw: _FakeSmtp(fail=True))
    channel.deliver(kind="email", masked="a***@example.com", token="T",
                    destination=EMAIL_A)          # 不抛
    assert channel.failures and "投递失败" in channel.failures[0]


def test_reset_request_looks_identical_whether_or_not_the_mail_was_sent(
    tmp_path: Path) -> None:
    """换了 smtp 通道之后，不可枚举的性质不能丢。"""
    fake = _FakeSmtp(fail=True)                   # 故意让它失败
    store = a.AccountStore(
        tmp_path,
        cipher=FieldCipher(FieldCipher.generate_key()),
        ident_secret="test-ident-secret",
        channel=a.SmtpChannel(host="h", port=465, sender="s@example.com",
                              smtp_factory=lambda *args, **kw: fake),
    )
    store.register(kind="email", identifier=EMAIL_A, password="Str0ng-Pass-1")
    token, masked = store.request_reset(kind="email", identifier=EMAIL_A)
    missing_token, missing_masked = store.request_reset(
        kind="email", identifier="nobody@example.com")
    assert token and missing_token is None          # 令牌只对存在的账号签发
    assert masked == a.mask_identifier("email", EMAIL_A)
    assert missing_masked == a.mask_identifier("email", "nobody@example.com")
    assert fake.sent == []                          # 一封都没出去
    assert store._channel.failures                  # 但运维能看见


def test_dev_channel_exposes_token_only_for_development(store: a.AccountStore) -> None:
    channel = a.DevChannel()
    assert channel.dev_token_visible() is True
    assert a.ConsoleChannel(print_fn=lambda _t: None).dev_token_visible() is False


# --------------------------------------------------------------------------- #
# 昵称（U69）
# --------------------------------------------------------------------------- #
def test_nickname_can_be_customized_and_randomized(store: a.AccountStore) -> None:
    account = store.register(
        kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd", nickname="老张"
    )
    assert account.nickname == "老张"

    refreshed = store.refresh_nickname(user_id=account.user_id)
    assert refreshed.nickname and refreshed.nickname != "老张"
    # 历史值留着，供申诉排查
    assert "老张" in refreshed.nickname_history


def test_nickname_duplicates_are_allowed(store: a.AccountStore) -> None:
    """昵称不做唯一：唯一性由 user_id 保证，否则「小明」会被抢注。"""
    one = store.register(
        kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd", nickname="小明"
    )
    two = store.register(
        kind=a.EMAIL, identifier=EMAIL_A, password="Demo-Passw0rd", nickname="小明"
    )
    assert one.nickname == two.nickname == "小明"
    assert one.user_id != two.user_id


@pytest.mark.parametrize(
    "bad,why",
    [
        ("", "空"),
        ("   ", "纯空白"),
        ("a", "太短"),
        ("x" * 17, "太长"),
        ("老\u200b张", "含零宽字符（可做视觉冒充）"),
        ("alice@example.com", "含 @ 会被当成邮箱"),
        ("我的号13800000001", "含疑似手机号，等于变相泄漏 PII"),
    ],
)
def test_invalid_nicknames_are_rejected(store: a.AccountStore, bad: str, why: str) -> None:
    with pytest.raises(a.AccountError) as err:
        store.register(
            kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd", nickname=bad
        )
    assert err.value.code == "invalid_nickname", why


def test_nickname_length_counts_graphemes_not_code_points() -> None:
    """`len("👨‍👩‍👧")` 是 5，用户看到的是 1 个 —— 按码点算会把正常 emoji 判成超长。"""
    length, exact = a.grapheme_length("👨‍👩‍👧")
    assert length <= 2, f"字素计数明显偏大：{length}"
    assert exact is False, "含 ZWJ 的串必须如实标注为近似值"
    assert a.grapheme_length("张三") == (2, True)


def test_nickname_refresh_is_rate_limited(store: a.AccountStore) -> None:
    account = store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
    for _ in range(3):
        store.refresh_nickname(user_id=account.user_id)
    with pytest.raises(a.AccountError) as err:
        store.refresh_nickname(user_id=account.user_id)
    assert err.value.code == "too_many_requests"


def test_reveal_identifier_is_server_side_only() -> None:
    """`reveal_identifier` 存在是为了申诉核对，但**接口层不得调用**。

    这条测试守的是"它存在且能解出原文"，另一侧由接口测试守"响应里没有原文"。
    两边一起，才能证明"能解"不等于"会泄漏"。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cipher = FieldCipher(FieldCipher.generate_key())
        store = a.AccountStore(Path(tmp), cipher=cipher, ident_secret="s")
        account = store.register(kind=a.PHONE, identifier=PHONE_A, password="Demo-Passw0rd")
        assert store.reveal_identifier(account.user_id) == "+8613800000001"
