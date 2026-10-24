"""用户自选模型与自带密钥：归属隔离、密钥不可回读、额度不静默降级（U70）。

这组测试守的是**越权与泄漏**，不是"功能能跑"：

* BYOK 的 key 不以明文落盘，对外只有掩码，`resolve` 才解密
* 绑定属于某个 user_id，别人解析不到、也用不了
* 免费/限免额度耗尽时报错，**不悄悄换成更差（或更贵）的模型**
* 目录里不出现已过期的限免模型，也不出现缺平台凭据的模型
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_butler.core import model_access as m
from ai_butler.core.security import FieldCipher

USER_A = "u_aaaaaaaaaaaaaaaa"
USER_B = "u_bbbbbbbbbbbbbbbb"
FAKE_KEY = "sk-demo-0123456789abcdef"


@pytest.fixture()
def access(tmp_path: Path) -> m.ModelAccess:
    return m.ModelAccess(
        tmp_path,
        cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={"DEEPSEEK_API_KEY": "platform-key-not-used-in-tests"},
    )


# --------------------------------------------------------------------------- #
# 目录与默认档
# --------------------------------------------------------------------------- #
def test_catalog_marks_local_model_as_vision_without_tools() -> None:
    """如实标注能力：本地 qwen2.5vl 能看图但**没有工具调用**。"""
    spec = {s.key: s for s in m.catalog()}["local-vl"]
    assert spec.tier == m.FREE
    assert spec.vision is True
    assert spec.tools is False


def test_expired_trial_model_disappears_from_catalog() -> None:
    """限免到期就不出现在目录里 —— 而不是出现了、点了才报错。"""
    future = datetime(2027, 6, 1, tzinfo=timezone.utc)
    keys = {s.key for s in m.catalog(now=future)}
    assert "platform-trial" not in keys
    assert "local-vl" in keys


def test_model_without_platform_key_is_listed_but_unusable(tmp_path: Path) -> None:
    """平台没配 key 时，模型仍在目录里但标 `usable=False` + 理由。"""
    access = m.ModelAccess(
        tmp_path, cipher=FieldCipher(FieldCipher.generate_key()), platform_keys={}
    )
    items = {i["model_key"]: i for i in access.available_models(user_id=USER_A)}
    trial = items["platform-trial"]
    assert trial["usable"] is False
    assert "DEEPSEEK_API_KEY" in trial["unusable_reason"]
    assert items["local-vl"]["usable"] is True


def test_default_is_free_tier_when_user_has_no_binding(access: m.ModelAccess) -> None:
    """用户没选过 → 默认落**免费**档（用户明确要求"默认免费"）。"""
    resolved = access.resolve(user_id=USER_A)
    assert resolved.tier == m.FREE
    assert resolved.model_key == "local-vl"
    assert "未绑定" in resolved.matched_by


def test_default_matching_is_explainable(access: m.ModelAccess) -> None:
    """画像命中要能说清是哪条规则命中的，不是黑箱。"""
    access.profile_prefs_loader = lambda uid: ("vision",)
    assert access.resolve(user_id=USER_A).model_key == "local-vl"

    access.profile_prefs_loader = lambda uid: ("long_context",)
    resolved = access.resolve(user_id=USER_A)
    assert resolved.model_key == "platform-trial"


# --------------------------------------------------------------------------- #
# BYOK：密钥不可回读
# --------------------------------------------------------------------------- #
def test_byok_key_is_encrypted_at_rest_and_masked_in_views(
    access: m.ModelAccess, tmp_path: Path
) -> None:
    binding = access.bind_byok(
        user_id=USER_A,
        base_url="https://api.example.com/v1",
        model="my-model",
        api_key=FAKE_KEY,
        display_name="我的模型",
    )

    blob = (tmp_path / "model_bindings.json").read_text(encoding="utf-8")
    assert FAKE_KEY not in blob, "自带密钥被明文写进了绑定文件"

    view = json.dumps(binding.public_view(), ensure_ascii=False)
    assert FAKE_KEY not in view, "对外视图回传了明文密钥"
    assert binding.api_key_hint.endswith(FAKE_KEY[-4:])
    assert "api_key_cipher" not in binding.public_view()


def test_byok_key_is_decrypted_only_at_resolve_time(access: m.ModelAccess) -> None:
    access.bind_byok(
        user_id=USER_A, base_url="https://api.example.com/v1",
        model="my-model", api_key=FAKE_KEY,
    )
    resolved = access.resolve(user_id=USER_A)
    assert resolved.api_key == FAKE_KEY          # 出站时才拿到
    assert resolved.tier == m.BYOK
    assert resolved.provider == "openai-compatible"


def test_byok_requires_full_credentials(access: m.ModelAccess) -> None:
    for kwargs, code in (
        ({"base_url": "ftp://x", "model": "m", "api_key": FAKE_KEY}, "invalid_base_url"),
        ({"base_url": "https://api.example.com", "model": "", "api_key": FAKE_KEY}, "invalid_model"),
        ({"base_url": "https://api.example.com", "model": "m", "api_key": "short"}, "invalid_api_key"),
    ):
        with pytest.raises(m.ModelAccessError) as err:
            access.bind_byok(user_id=USER_A, **kwargs)      # type: ignore[arg-type]
        assert err.value.code == code


def test_byok_does_not_consume_platform_quota(access: m.ModelAccess) -> None:
    access.bind_byok(
        user_id=USER_A, base_url="https://api.example.com/v1",
        model="my-model", api_key=FAKE_KEY,
    )
    for _ in range(5):
        assert access.resolve(user_id=USER_A).tier == m.BYOK
    # byok 花的是用户自己的钱，平台额度不动
    assert access.remaining(user_id=USER_A, tier=m.FREE) == m.FREE_DAILY_CALLS


# --------------------------------------------------------------------------- #
# 归属隔离：这是"不会被盗用"的落点
# --------------------------------------------------------------------------- #
def test_binding_does_not_leak_to_another_user(access: m.ModelAccess) -> None:
    """A 的模型，B 解析时**拿不到** —— B 走自己的默认档。"""
    access.bind_byok(
        user_id=USER_A, base_url="https://api.example.com/v1",
        model="a-only-model", api_key=FAKE_KEY,
    )
    resolved_b = access.resolve(user_id=USER_B)
    assert resolved_b.tier == m.FREE
    assert resolved_b.api_key == "", "B 拿到了 A 的密钥"


def test_tampered_binding_is_rejected(access: m.ModelAccess, tmp_path: Path) -> None:
    """文件里被人改了归属（把 A 的绑定挂到 B 名下），解析必须拒绝。

    这条守的是那行 `binding.user_id != uid`：现在它恒真（key 就是 user_id），
    看似多余；但一旦将来换成数据库或支持共享绑定，它就是唯一的越权拦截点。
    """
    access.bind_byok(
        user_id=USER_A, base_url="https://api.example.com/v1",
        model="a-only-model", api_key=FAKE_KEY,
    )
    path = tmp_path / "model_bindings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[USER_B] = dict(data[USER_A])           # 复制到 B 名下，但 user_id 字段仍是 A
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(m.ModelAccessError) as err:
        access.resolve(user_id=USER_B)
    assert err.value.code == "forbidden"


def test_unbind_removes_cipher_entirely(access: m.ModelAccess, tmp_path: Path) -> None:
    """删除模型 = 连密文一起删，不留可恢复的残片。"""
    access.bind_byok(
        user_id=USER_A, base_url="https://api.example.com/v1",
        model="my-model", api_key=FAKE_KEY,
    )
    assert access.unbind(user_id=USER_A) is True
    blob = (tmp_path / "model_bindings.json").read_text(encoding="utf-8")
    assert "api_key_cipher" not in blob
    assert access.resolve(user_id=USER_A).tier == m.FREE


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_no_identity_means_no_model(access: m.ModelAccess, bad: object) -> None:
    """与网关同一条原则：没有身份就没有模型。"""
    with pytest.raises(m.ModelAccessError) as err:
        access.resolve(user_id=bad)             # type: ignore[arg-type]
    assert err.value.code == "unauthenticated"


# --------------------------------------------------------------------------- #
# 额度：超额报错，不静默降级
# --------------------------------------------------------------------------- #
def test_free_quota_exhaustion_raises_instead_of_downgrading(tmp_path: Path) -> None:
    access = m.ModelAccess(
        tmp_path, cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={"DEEPSEEK_API_KEY": "k"}, free_daily_calls=2,
    )
    access.bind_catalog_model(user_id=USER_A, model_key="local-vl")
    access.resolve(user_id=USER_A)
    access.resolve(user_id=USER_A)

    with pytest.raises(m.ModelAccessError) as err:
        access.resolve(user_id=USER_A)
    assert err.value.code == "quota_exhausted"
    # 报错文案要给出路，而不是只说"不行"
    assert "自带密钥" in err.value.message


def test_quota_resets_next_day(tmp_path: Path) -> None:
    day = {"now": "2026-10-24"}
    access = m.ModelAccess(
        tmp_path, cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={"DEEPSEEK_API_KEY": "k"}, free_daily_calls=1,
        today=lambda: day["now"],
    )
    access.bind_catalog_model(user_id=USER_A, model_key="local-vl")
    access.resolve(user_id=USER_A)
    with pytest.raises(m.ModelAccessError):
        access.resolve(user_id=USER_A)

    day["now"] = "2026-10-25"
    assert access.remaining(user_id=USER_A, tier=m.FREE) == 1
    assert access.resolve(user_id=USER_A).tier == m.FREE


def test_quota_is_per_account(tmp_path: Path) -> None:
    access = m.ModelAccess(
        tmp_path, cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={"DEEPSEEK_API_KEY": "k"}, free_daily_calls=1,
    )
    access.bind_catalog_model(user_id=USER_A, model_key="local-vl")
    access.resolve(user_id=USER_A)
    with pytest.raises(m.ModelAccessError):
        access.resolve(user_id=USER_A)
    # A 用完了不影响 B —— 免费额度是按账号计的，不是全平台共享
    assert access.resolve(user_id=USER_B).tier == m.FREE


def test_binding_unavailable_model_is_refused_early(tmp_path: Path) -> None:
    """选了缺平台凭据的模型 → 绑定时就拒，别等对话里报 500。"""
    access = m.ModelAccess(
        tmp_path, cipher=FieldCipher(FieldCipher.generate_key()), platform_keys={}
    )
    with pytest.raises(m.ModelAccessError) as err:
        access.bind_catalog_model(user_id=USER_A, model_key="platform-trial")
    assert err.value.code == "model_unavailable"


def test_unknown_model_key_is_rejected(access: m.ModelAccess) -> None:
    with pytest.raises(m.ModelAccessError) as err:
        access.bind_catalog_model(user_id=USER_A, model_key="gpt-imaginary")
    assert err.value.code == "unknown_model"


# --------------------------------------------------------------------------- #
# 日志安全
# --------------------------------------------------------------------------- #
def test_resolved_model_repr_never_prints_the_key(access: m.ModelAccess) -> None:
    """`repr` 常被顺手打进日志，所以它必须自己就把 key 藏掉。"""
    access.bind_byok(
        user_id=USER_A, base_url="https://api.example.com/v1",
        model="my-model", api_key=FAKE_KEY,
    )
    resolved = access.resolve(user_id=USER_A)
    assert FAKE_KEY not in repr(resolved)
    assert "***" in repr(resolved)


# --------------------------------------------------------------------------- #
# 默认路径的额度计量（这一条曾经漏掉，直接导致"免费额度"对新用户失效）
# --------------------------------------------------------------------------- #
def test_unbound_default_also_consumes_quota(access: m.ModelAccess) -> None:
    """**没绑定也必须计量**。

    漏掉这条的后果很具体：新用户从没选过模型，每次都走 `_default_for`，
    而那条路不扣额度 —— "免费 50 次/天"对**最该被限流的人**是一句空话。
    """
    before = access.remaining(user_id=USER_A, tier=m.FREE)
    resolved = access.resolve(user_id=USER_A)
    assert resolved.tier == m.FREE
    assert access.remaining(user_id=USER_A, tier=m.FREE) == before - 1


def test_exhausted_free_quota_blocks_the_default_path(tmp_path: Path) -> None:
    """额度用完时，默认路径同样拒绝 —— 不因为"没选过模型"就放行。"""
    access = m.ModelAccess(
        tmp_path,
        cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={},
        free_daily_calls=1,
    )
    assert access.resolve(user_id=USER_A).model_key == "local-vl"
    with pytest.raises(m.ModelAccessError) as err:
        access.resolve(user_id=USER_A)
    assert err.value.code == "quota_exhausted"


def test_missing_platform_key_does_not_burn_quota(tmp_path: Path) -> None:
    """运营侧把 key 撤了，不能算用户头上：报 `missing_credential` 且额度不减。

    用同一目录重建一个"key 已被移除"的实例来模拟运维改动
    —— 用户此时还绑着 platform-trial，但平台已经调不动它了。
    """
    root = tmp_path / "store"
    with_key = m.ModelAccess(
        root,
        cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={"DEEPSEEK_API_KEY": "platform-key"},
    )
    with_key.bind_catalog_model(user_id=USER_A, model_key="platform-trial")

    without_key = m.ModelAccess(
        root,
        cipher=FieldCipher(FieldCipher.generate_key()),
        platform_keys={},
    )
    before = without_key.remaining(user_id=USER_A, tier=m.TRIAL)
    with pytest.raises(m.ModelAccessError) as err:
        without_key.resolve(user_id=USER_A)
    assert err.value.code == "missing_credential"
    assert without_key.remaining(user_id=USER_A, tier=m.TRIAL) == before

