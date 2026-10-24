"""用户自选大模型与自带密钥：归属隔离、加密存储、额度按账号计量（U70）。

要解决的问题
------------
用户要「可自由选择大模型、可自己输入 API Key 接入新模型、默认用免费或限免、
按个人信息匹配、且**不会被未经允许盗用**」。

最后一句是硬约束，所以它不能只写在界面提示里，必须落成**调用前的校验**：

1. 每个模型的凭据属于**一个** user_id，不存在「公共池」；
2. 解析模型时必须带 user_id，非本人绑定一律 `forbidden`；
3. BYOK 的 key 只以密文形式存在，接口只回掩码，**绝无回读路径**；
4. 免费/限免档按账号计量，超额报明确错误，不静默降级到更差的模型
   —— 「悄悄换成弱模型」和「盗用」一样，都是用户没同意的事。

为什么不用「请求体里带 api_key」这种最省事的做法
------------------------------------------------
那等于让 key 每次都穿过网络与日志。一旦某处日志没脱敏，key 就泄漏了。
落库加密 + 只在出站前一刻解密，泄漏面从"每个请求"缩到"服务端内存"。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ai_butler.core.security import FieldCipher

# --------------------------------------------------------------------------- #
# 档位
# --------------------------------------------------------------------------- #
FREE = "free"          # 平台免费额度（本地模型或平台出账）
TRIAL = "trial"        # 限时限免，带到期时间
BYOK = "byok"          # 用户自带 key / base_url

TIERS = (FREE, TRIAL, BYOK)

#: 免费档每账号每日调用上限（实现参数，可由 settings 覆盖）
FREE_DAILY_CALLS = 50
#: 限免档每账号每日调用上限
TRIAL_DAILY_CALLS = 100


class ModelAccessError(Exception):
    """模型接入相关错误。`code` 供接口层稳定映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 模型目录
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelSpec:
    """`key` 是对外的稳定标识，`model` 才是发给供应商的型号名。"""

    key: str
    label: str
    tier: str
    provider: str
    model: str
    description: str = ""
    vision: bool = False
    tools: bool = False
    #: 限免到期时间（ISO）。非 trial 档为空。
    trial_until: str = ""
    #: 是否需要平台凭据（本地 ollama 不需要）
    needs_platform_key: str = ""


#: 平台内置目录。**如实标注能力**：本地没有的模型不写进来，
#: 没有 tools 能力的模型 `tools=False`，界面上就不会给它画"可调工具"的标记。
CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="local-vl",
        label="本机视觉模型（免费）",
        tier=FREE,
        provider="ollama",
        model="qwen2.5vl:3b",
        description="本地跑，不产生费用；支持看图，不支持工具调用",
        vision=True,
        tools=False,
    ),
    ModelSpec(
        key="platform-trial",
        label="平台限时免费（DeepSeek）",
        tier=TRIAL,
        provider="deepseek",
        model="deepseek-chat",
        description="平台出账的限时免费额度，到期自动停用",
        trial_until="2026-12-31",
        needs_platform_key="DEEPSEEK_API_KEY",
    ),
)

_CATALOG_BY_KEY = {spec.key: spec for spec in CATALOG}


def catalog(*, now: datetime | None = None) -> tuple[ModelSpec, ...]:
    """当前可选的模型目录（含到期判断）。"""
    moment = now or datetime.now(timezone.utc)
    alive: list[ModelSpec] = []
    for spec in CATALOG:
        if spec.tier == TRIAL and spec.trial_until:
            if moment.date() > date.fromisoformat(spec.trial_until):
                continue        # 过期就不出现，而不是出现后点了报错
        alive.append(spec)
    return tuple(alive)


def _normalize_key_hint(raw: str) -> str:
    """掩码：只留头 3 尾 4。**这是唯一能出服务端的 form**。"""
    text = str(raw or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:3]}{'*' * 6}{text[-4:]}"


def _validate_byok_fields(*, base_url: str, model: str, api_key: str) -> None:
    if not str(base_url or "").strip().startswith(("https://", "http://")):
        raise ModelAccessError("invalid_base_url", "接入地址必须是 http(s) URL")
    if not str(model or "").strip():
        raise ModelAccessError("invalid_model", "模型名称不能为空")
    if len(str(api_key or "").strip()) < 8:
        raise ModelAccessError("invalid_api_key", "API Key 看起来不像有效的密钥（过短）")


# --------------------------------------------------------------------------- #
# 绑定
# --------------------------------------------------------------------------- #
@dataclass
class ModelBinding:
    user_id: str
    model_key: str
    tier: str
    provider: str
    model: str
    display_name: str
    base_url: str = ""
    api_key_cipher: str = ""      # 仅 byok 有值；**永远不出服务端**
    api_key_hint: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "model_key": self.model_key,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "api_key_cipher": self.api_key_cipher,
            "api_key_hint": self.api_key_hint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelBinding":
        return cls(
            user_id=str(data["user_id"]),
            model_key=str(data["model_key"]),
            tier=str(data["tier"]),
            provider=str(data["provider"]),
            model=str(data["model"]),
            display_name=str(data.get("display_name", data["model_key"])),
            base_url=str(data.get("base_url", "")),
            api_key_cipher=str(data.get("api_key_cipher", "")),
            api_key_hint=str(data.get("api_key_hint", "")),
            created_at=str(data.get("created_at", "")),
        )

    def public_view(self) -> dict[str, Any]:
        """对外视图：**没有密文**，只有掩码。"""
        return {
            "model_key": self.model_key,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "display_name": self.display_name,
            "base_url": self.base_url,
            "api_key_hint": self.api_key_hint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ResolvedModel:
    """解析结果：能直接喂给网关的东西，以及**为什么是它**。"""

    model_key: str
    tier: str
    provider: str
    model: str
    api_key: str = ""        # 已解密，仅存活于本次调用
    base_url: str = ""
    matched_by: str = ""

    def __repr__(self) -> str:                # pragma: no cover - 防手滑打日志
        return (f"ResolvedModel(key={self.model_key!r}, tier={self.tier!r}, "
                f"provider={self.provider!r}, model={self.model!r}, "
                f"api_key={'***' if self.api_key else ''}, matched_by={self.matched_by!r})")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 接入层
# --------------------------------------------------------------------------- #
class ModelAccess:
    """模型绑定 + 额度。文件即真值源（与账号存储同风格）。"""

    def __init__(
        self,
        root: Path,
        *,
        cipher: FieldCipher,
        platform_keys: dict[str, str] | None = None,
        free_daily_calls: int = FREE_DAILY_CALLS,
        trial_daily_calls: int = TRIAL_DAILY_CALLS,
        today: Callable[[], str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.bindings_path = self.root / "model_bindings.json"
        self.usage_path = self.root / "model_usage.json"
        self._cipher = cipher
        self._platform_keys = dict(platform_keys or {})
        self._limits = {FREE: free_daily_calls, TRIAL: trial_daily_calls}
        self._today = today or (lambda: date.today().isoformat())

    # ---------------- 底层 ---------------- #
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

    # ---------------- 目录 ---------------- #
    def available_models(self, *, user_id: str) -> list[dict[str, Any]]:
        """给界面的目录：每个模型都带「能不能用、为什么不能用」。"""
        bound_raw = self._bindings().get(str(user_id))
        bound = ModelBinding.from_dict(bound_raw) if bound_raw else None
        items: list[dict[str, Any]] = []
        for spec in catalog():
            if spec.needs_platform_key and not self._platform_keys.get(spec.needs_platform_key):
                usable, reason = False, f"平台未配置 {spec.needs_platform_key}"
            elif spec.tier in self._limits and self.remaining(user_id=user_id, tier=spec.tier) <= 0:
                usable, reason = False, "今日免费额度已用完"
            else:
                usable, reason = True, ""
            items.append({
                "model_key": spec.key,
                "label": spec.label,
                "tier": spec.tier,
                "provider": spec.provider,
                "model": spec.model,
                "description": spec.description,
                "vision": spec.vision,
                "tools": spec.tools,
                "trial_until": spec.trial_until,
                "usable": usable,
                "unusable_reason": reason,
                "selected": bool(bound and bound.model_key == spec.key),
            })
        # 用户自己的 byok 绑定也列出来（它不在内置目录里）
        if bound and bound.tier == BYOK:
            items.append({
                "model_key": bound.model_key,
                "label": bound.display_name,
                "tier": BYOK,
                "provider": bound.provider,
                "model": bound.model,
                "description": "由你自带密钥接入",
                "vision": False,
                "tools": False,
                "trial_until": "",
                "usable": True,
                "unusable_reason": "",
                "selected": True,
            })
        return items

    # ---------------- 绑定 ---------------- #
    def _bindings(self) -> dict[str, Any]:
        return self._read(self.bindings_path)

    def binding(self, user_id: str) -> ModelBinding | None:
        raw = self._bindings().get(str(user_id))
        return ModelBinding.from_dict(raw) if raw else None

    def bind_catalog_model(self, *, user_id: str, model_key: str) -> ModelBinding:
        uid = self._require_user(user_id)
        spec = _CATALOG_BY_KEY.get(str(model_key))
        if spec is None or spec not in catalog():
            raise ModelAccessError("unknown_model", f"没有这个模型：{model_key!r}")
        if spec.needs_platform_key and not self._platform_keys.get(spec.needs_platform_key):
            # 提前拒，而不是让用户点了"使用"之后在对话里报 500
            raise ModelAccessError(
                "model_unavailable", f"{spec.label} 暂不可用：平台未配置 {spec.needs_platform_key}"
            )
        binding = ModelBinding(
            user_id=uid,
            model_key=spec.key,
            tier=spec.tier,
            provider=spec.provider,
            model=spec.model,
            display_name=spec.label,
            created_at=_now_iso(),
        )
        self._save_binding(binding)
        return binding

    def bind_byok(
        self,
        *,
        user_id: str,
        base_url: str,
        model: str,
        api_key: str,
        display_name: str = "",
    ) -> ModelBinding:
        """接入用户自带的模型。key 加密后落盘，回给调用方的只有掩码。"""
        uid = self._require_user(user_id)
        _validate_byok_fields(base_url=base_url, model=model, api_key=api_key)
        binding = ModelBinding(
            user_id=uid,
            model_key="byok-" + secrets.token_hex(4),
            tier=BYOK,
            provider="openai-compatible",
            model=str(model).strip(),
            display_name=(display_name or str(model)).strip()[:40],
            base_url=str(base_url).strip().rstrip("/"),
            api_key_cipher=self._cipher.encrypt(str(api_key).strip()),
            api_key_hint=_normalize_key_hint(str(api_key).strip()),
            created_at=_now_iso(),
        )
        self._save_binding(binding)
        return binding

    def _save_binding(self, binding: ModelBinding) -> None:
        data = self._bindings()
        data[binding.user_id] = binding.to_dict()
        self._write(self.bindings_path, data)

    def unbind(self, *, user_id: str) -> bool:
        """删除绑定。BYOK 的密文随之消失，不做软删残留（U70 ⑦）。"""
        uid = self._require_user(user_id)
        data = self._bindings()
        if uid not in data:
            return False
        del data[uid]
        self._write(self.bindings_path, data)
        return True

    # ---------------- 解析（调用前必经） ----------------
    def resolve(self, *, user_id: str) -> ResolvedModel:
        """把「该用户现在该用哪个模型」解析成可直接调用的凭据。

        **归属校验在这里**：`binding.user_id` 必须等于 `user_id`。
        文件里理论上不会出现错配（key 就是 user_id），但这行校验留着 ——
        将来若改成数据库、或支持"共享绑定"，这行就是唯一拦住越权的地方。
        """
        uid = self._require_user(user_id)
        binding = self.binding(uid)

        if binding is None:
            return self._default_for(uid, matched_by="未绑定 → 落免费档默认模型")

        if binding.user_id != uid:
            raise ModelAccessError("forbidden", "该模型绑定不属于当前账号")

        if binding.tier == BYOK:
            if not binding.api_key_cipher:
                raise ModelAccessError("missing_credential", "自带密钥缺失，请重新接入")
            return ResolvedModel(
                model_key=binding.model_key,
                tier=binding.tier,
                provider=binding.provider,
                model=binding.model,
                api_key=self._cipher.decrypt(binding.api_key_cipher),
                base_url=binding.base_url,
                matched_by="用户自带密钥",
            )

        # 平台档位：先确认凭据可用，再扣额度。扣不动就不许调（不静默降级）
        api_key = self._platform_key_for(binding.provider, binding.model_key)
        self.consume(user_id=uid, tier=binding.tier)
        return ResolvedModel(
            model_key=binding.model_key,
            tier=binding.tier,
            provider=binding.provider,
            model=binding.model,
            api_key=api_key,
            base_url=binding.base_url,
            matched_by=f"用户已选（{binding.tier} 档）",
        )

    def _platform_key_for(self, provider: str, model_key: str) -> str:
        """取平台凭据。**没有就报错，不拿空串去撞网关**。

        顺序上放在 `consume` 之前：平台没配 key 是**运营侧**的问题，
        不该扣用户当天的额度 —— 让用户为别人的配置失误买单，
        比"少扣一次"更难解释。
        """
        if provider != "deepseek":
            return ""
        api_key = self._platform_keys.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ModelAccessError(
                "missing_credential",
                f"平台未配置 DEEPSEEK_API_KEY，{model_key} 暂不可用",
            )
        return api_key

    def _default_for(self, user_id: str, *, matched_by: str) -> ResolvedModel:
        """没绑定时按画像挑一个。挑选规则要能说清，不是黑箱。

        **未绑定也照样计量**：默认免费模型不是"无限量"。
        少了这句，「免费额度 50 次/天」对**从来没选过模型**的用户就是空话
        —— 而新用户默认就是这个状态，等于免费档对最该被限流的人敞着门。
        """
        spec = self._match_by_profile(user_id)
        api_key = ""
        if spec.needs_platform_key:
            api_key = self._platform_keys.get(spec.needs_platform_key, "")
            if not api_key:
                raise ModelAccessError(
                    "missing_credential",
                    f"平台未配置 {spec.needs_platform_key}，{spec.key} 暂不可用",
                )
        # 先扣再发：额度扣不动就不许调（与「已绑定平台档」同一口径）
        self.consume(user_id=user_id, tier=spec.tier)
        return ResolvedModel(
            model_key=spec.key, tier=spec.tier, provider=spec.provider,
            model=spec.model, api_key=api_key, matched_by=matched_by,
        )

    def preview_default(self, *, user_id: str) -> ModelSpec:
        """按当前画像会匹配到哪个模型 —— **不扣额度、不发请求**。

        给「改完偏好立刻看得见效果」用的。刻意不复用 `resolve()`：
        那个方法会 `consume()`，如果界面每次保存偏好都调它一次，
        用户光是改标签就能把自己的免费额度点光。
        """
        return self._match_by_profile(user_id)

    def _match_by_profile(self, user_id: str) -> ModelSpec:
        """U70 ②：按用户画像匹配，且**命中理由可解释**。

        画像来自账号的 prefs（如 `vision` / `long_context` / `cheap`）。
        规则很简单、顺序固定，任何人不看代码也能理解为什么给了他这个模型。
        """
        prefs = set(self._profile_prefs(user_id))
        pool = list(catalog())
        if not pool:
            raise ModelAccessError("no_model", "没有可用模型：目录为空或已全部到期")

        if "vision" in prefs:
            for spec in pool:
                if spec.vision:
                    return spec
        if "long_context" in prefs:
            for spec in pool:
                if spec.tier == TRIAL and self._platform_keys.get(spec.needs_platform_key, ""):
                    return spec
        # 默认：免费档优先（"默认免费"是用户明确要求的）
        for spec in pool:
            if spec.tier == FREE:
                if spec.needs_platform_key and not self._platform_keys.get(spec.needs_platform_key):
                    continue
                return spec
        return pool[0]

    #: 画像读取可注入：默认从同目录的 accounts.json 里取 prefs
    profile_prefs_loader: Callable[[str], Iterable[str]] | None = None

    def _profile_prefs(self, user_id: str) -> tuple[str, ...]:
        if self.profile_prefs_loader is not None:
            return tuple(self.profile_prefs_loader(user_id))
        raw = self._read(self.root / "accounts.json").get(str(user_id), {})
        return tuple(str(p) for p in raw.get("prefs", []))

    # ---------------- 额度 ----------------
    def _usage(self) -> dict[str, Any]:
        return self._read(self.usage_path)

    def consume(self, *, user_id: str, tier: str) -> int:
        """扣一次额度，返回剩余。超额抛 `quota_exhausted`。"""
        uid = self._require_user(user_id)
        limit = self._limits.get(tier)
        if limit is None:
            return -1                      # byok 不占平台额度
        data = self._usage()
        today = self._today()
        record = data.get(uid) or {}
        if record.get("date") != today:
            record = {"date": today, "counts": {}}
        counts = record.setdefault("counts", {})
        used = int(counts.get(tier, 0))
        if used >= limit:
            raise ModelAccessError(
                "quota_exhausted",
                f"{tier} 档今日额度已用完（{used}/{limit}），"
                "可在模型设置里改用自带密钥，或明天再试",
            )
        counts[tier] = used + 1
        data[uid] = record
        self._write(self.usage_path, data)
        return limit - counts[tier]

    def remaining(self, *, user_id: str, tier: str) -> int:
        limit = self._limits.get(tier)
        if limit is None:
            return -1
        record = self._usage().get(str(user_id)) or {}
        if record.get("date") != self._today():
            return limit
        return max(limit - int((record.get("counts") or {}).get(tier, 0)), 0)

    # ---------------- 校验 ---------------- #
    @staticmethod
    def _require_user(user_id: str | None) -> str:
        if not user_id or not str(user_id).strip():
            # 与 LLMGateway._require_user 同一条原则：没有身份就没有模型
            raise ModelAccessError("unauthenticated", "必须携带 user_id 才能使用模型")
        return str(user_id)
