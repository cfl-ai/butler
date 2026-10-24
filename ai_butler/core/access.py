"""访问层级与能力门控 —— 「未登录只能文字查询，登录后才有语音与声纹」。

计划书依据
----------
- 七、验收标准 11「**未登录状态下所有业务接口拒绝执行**」
- 三、P7 触发时机「后台常驻监听，唤醒即启动全业务链路；唤醒后先校验本地登录会话状态」
- 三、P7 步骤3「账号密码登录」/ 步骤5「请求带上会话 Token；会话失效弹窗重登」
- 一、项目概述「移动端（仅感知 + 交互）」

裁定
----
U64（人工授权，2026-10-24）：「未登录时两端都只能文字查询，登录后可以语音唤起」。
这与验收标准 11 的字面「拒绝执行」存在张力，处理方式如下 —— **不放宽鉴权，只放宽能力面**：

1. 验收标准 11 约束的是**身份**：未登录不得访问**私有**数据与业务副作用。
   这一条**原样保留**（私有 Vault 检索、设备上报、支付、记忆写入一律拒绝）。
2. 新增的是**访客层（guest）**：由 `/api/session/guest` 签发**有期限的访客凭据**，
   与成员凭据走同一套中间件校验（不是「免鉴权白名单」）。
   访客凭据只解锁 `TEXT_QUERY` 一项能力 —— 公开知识的文字问答。
3. 语音相关能力（语音输入 / 播报 / 离线唤醒 / 声纹）**一律要求成员层级**。
   理由不是省算力，而是**声纹与唤醒本身就是身份凭据**：
   允许访客采集声纹等于允许匿名方往声纹库里写样本（U63 的 1:N 校验会被污染）。

因此本模块把「谁能用哪项能力」集中成**一张表**，所有入口只查表，不再各自写 if。
表是唯一真值源；新增能力必须同时在 `CAPABILITIES` 与 `TIER_CAPABILITIES` 登记，
否则 `authorize()` 直接抛错而不是默认放行（fail-closed）。

与 `core/permissions.py` 的关系
------------------------------
`permissions.py` 管的是**已登录之后**的三层权限（Agent 能力级 × 数据密级 × 用户角色）。
本模块管的是**登录之前**的层级判定（访客 vs 成员），并在进入 permissions 之前
先把能力面削到该层级允许的范围。两者是串联关系，不是替代关系：
本模块说「他没资格用语音」，permissions 说「他有资格但不能读 L4」。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

#: 访客 user_id 前缀。**身份层级从会话身份里派生**，不采信请求体 ——
#: 否则客户端把 body 里的 tier 改成 member 就能自授权。
GUEST_ID_PREFIX = "guest:"

#: 访客凭据有效期（实现参数，短于成员：访客只是「先试试」的通道）。
GUEST_SESSION_TTL_SECONDS = 3600

#: 访客凭据的问答额度（实现参数）。**这是成本闸，不是权限闸**：
#: 语音与私有数据已由能力表挡住，这里只防「匿名方无限刷大模型」。
GUEST_TEXT_QUERY_BUDGET = 30


class AccessTier(str, Enum):
    """身份层级。**只有两级**：再细分是 `permissions.UserRole` 的事。"""

    GUEST = "guest"      # 未登录访客：仅文字查询
    MEMBER = "member"    # 已登录成员：全部能力（再受 permissions 三层约束）

    @classmethod
    def from_user_id(cls, user_id: str | None) -> "AccessTier":
        """从会话身份派生层级。空身份按**最严**处理（guest，而非 member）。"""
        if user_id and str(user_id).startswith(GUEST_ID_PREFIX):
            return cls.GUEST
        if not user_id or not str(user_id).strip():
            return cls.GUEST
        return cls.MEMBER


class Capability(str, Enum):
    """一项「用户可以做的事」。命名按**用户动作**，不按内部模块名。"""

    TEXT_QUERY = "text_query"                # 文字查询（公开知识问答）
    VOICE_INPUT = "voice_input"              # 语音输入（VAD + ASR）
    VOICE_OUTPUT = "voice_output"            # 语音播报（TTS）
    WAKE_WORD = "wake_word"                  # 离线唤醒常驻监听
    SPEAKERPRINT = "speakerprint"            # 声纹登记 / 校验（U63）
    VISION_INPUT = "vision_input"            # 图文识别（U61）
    PRIVATE_RETRIEVAL = "private_retrieval"  # 私有 Vault 检索
    MEMORY_WRITE = "memory_write"            # 长期记忆写入
    DEVICE_REPORT = "device_report"          # 设备状态上报（三、P7 步骤7）
    SENSITIVE_ACTION = "sensitive_action"    # 支付 / 下单等副作用（P8）
    PROFILE_EDIT = "profile_edit"            # 改昵称 / 偏好 / 选择模型（U67~U70）
    LOCATION_SHARE = "location_share"        # 定位上报 / 推荐 / 导航（U71）


#: 各层级允许的能力集合。**未登记的能力 = 谁都不许**（fail-closed）。
TIER_CAPABILITIES: Mapping[AccessTier, frozenset[Capability]] = {
    AccessTier.GUEST: frozenset({Capability.TEXT_QUERY}),
    AccessTier.MEMBER: frozenset(Capability),
}

#: 层级被拒时的**可执行提示**。空字符串表示「不给提示」——
#: 越权时究竟提示什么，必须是明确决定，不能靠前端随手拼。
UPGRADE_HINT: Mapping[Capability, str] = {
    Capability.VOICE_INPUT: "语音输入需要登录后使用（需绑定账号才能做声纹校验）",
    Capability.VOICE_OUTPUT: "语音播报需要登录后使用",
    Capability.WAKE_WORD: "语音唤起需要登录后使用",
    Capability.SPEAKERPRINT: "声纹登记与校验需要登录后使用",
    Capability.VISION_INPUT: "图文识别需要登录后使用",
    Capability.PRIVATE_RETRIEVAL: "检索你自己的私有资料需要登录",
    Capability.MEMORY_WRITE: "写入长期记忆需要登录",
    Capability.DEVICE_REPORT: "设备上报需要登录",
    Capability.SENSITIVE_ACTION: "该操作需要登录并通过声纹校验",
}


class AccessDenied(PermissionError):
    """能力不足。带上机器可读 code，供端侧区分「去登录」与「真的坏了」。"""

    code = "capability_required"

    def __init__(self, capability: Capability, tier: AccessTier,
                 hint: str = "") -> None:
        self.capability = capability
        self.tier = tier
        self.hint = hint or UPGRADE_HINT.get(capability, "")
        super().__init__(f"{tier.value} 层级无 {capability.value} 能力"
                         + (f"：{self.hint}" if self.hint else ""))

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "capability": self.capability.value,
                "tier": self.tier.value, "message": self.hint or str(self),
                "need_tier": AccessTier.MEMBER.value}


@dataclass(frozen=True)
class AccessVerdict:
    allowed: bool
    tier: AccessTier
    capability: Capability
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "tier": self.tier.value,
                "capability": self.capability.value, "reason": self.reason}


def authorize(tier: AccessTier, capability: Capability) -> AccessVerdict:
    """查表判定。**只读表，不做例外** —— 例外一旦散落在调用点就再也查不清。"""
    allowed_set = TIER_CAPABILITIES.get(tier)
    if allowed_set is None:
        raise KeyError(f"未登记的访问层级 {tier!r}：能力表不允许默认放行")
    # 能力本身没在任何表里出现过 —— 说明是新增能力忘了登记，直接报错而不是放行
    if capability not in set().union(*TIER_CAPABILITIES.values()):
        raise KeyError(f"未登记的能力 {capability!r}：请先在 TIER_CAPABILITIES 登记")
    if capability in allowed_set:
        return AccessVerdict(True, tier, capability, "层级允许")
    return AccessVerdict(False, tier, capability,
                         UPGRADE_HINT.get(capability, "层级不足"))


def require(tier: AccessTier, capability: Capability) -> None:
    """不适格即抛 `AccessDenied`。接口入口统一走这里，不各自拼 401/403 文案。"""
    verdict = authorize(tier, capability)
    if not verdict.allowed:
        raise AccessDenied(capability, tier, verdict.reason)


def capabilities_of(tier: AccessTier) -> tuple[Capability, ...]:
    """该层级的能力全集（供 `/api/access/profile` 下发给端侧画按钮状态）。"""
    return tuple(sorted(TIER_CAPABILITIES.get(tier, frozenset()), key=lambda c: c.value))


def is_guest(user_id: str | None) -> bool:
    return AccessTier.from_user_id(user_id) is AccessTier.GUEST


def new_guest_id() -> str:
    """签发访客身份。**带前缀是唯一的层级标记**：中间件只透传 user_id，
    下游若还要额外记住「这个人是访客」就会有两处真值源，迟早不一致。"""
    return GUEST_ID_PREFIX + uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# 访客额度（成本闸，不是权限闸）
# --------------------------------------------------------------------------- #
@dataclass
class GuestBudget:
    """按访客身份计的问答次数预算。

    **纯内存实现，进程重启即清零** —— 这是如实标注的局限：多进程部署下
    每个进程各记一份。真要做准，应落到 Redis（与限流同一载体）；
    当前形态覆盖「单实例开发/预发布」，不声称能防分布式刷量。
    """

    limit: int = GUEST_TEXT_QUERY_BUDGET
    clock: Callable[[], float] = field(default=time.monotonic)
    ttl_seconds: float = 3600.0
    _used: dict[str, tuple[float, int]] = field(default_factory=dict, repr=False)

    def _prune(self, now: float) -> None:
        expired = [k for k, (start, _) in self._used.items()
                   if now - start > self.ttl_seconds]
        for key in expired:
            self._used.pop(key, None)

    def consume(self, user_id: str, *, cost: int = 1) -> tuple[bool, int]:
        """消耗额度，返回 (是否允许, 剩余)。超额即拒绝而不是静默截断。"""
        if cost <= 0:
            raise ValueError("额度消耗必须为正整数")
        now = self.clock()
        self._prune(now)
        start, used = self._used.get(user_id, (now, 0))
        if used + cost > self.limit:
            self._used[user_id] = (start, used)
            return False, max(0, self.limit - used)
        self._used[user_id] = (start, used + cost)
        return True, self.limit - used - cost

    def remaining(self, user_id: str) -> int:
        self._prune(self.clock())
        _, used = self._used.get(user_id, (self.clock(), 0))
        return max(0, self.limit - used)

    def reset(self, user_id: str | None = None) -> None:
        """登录成功后清零（访客额度转成员额度，不该把试用的余额带进正式身份）。"""
        if user_id is None:
            self._used.clear()
        else:
            self._used.pop(user_id, None)


# --------------------------------------------------------------------------- #
# 声纹门控（U63）
# --------------------------------------------------------------------------- #
#: 声纹校验的默认阈值（实现参数）。CAM++ 在中文 16k 上的经验区间是 0.3~0.5；
#: 取 0.35 偏严：**宁可让本人重说一次，也不要放错人**（放错人不可回滚）。
VOICEPRINT_THRESHOLD = 0.35


@dataclass(frozen=True)
class VoiceprintVerdict:
    """一次声纹校验的结论。**永远带分数**，不给「大概是他」这种结论。"""

    matched: bool
    score: float
    threshold: float = VOICEPRINT_THRESHOLD
    speaker_id: str | None = None
    reason: str = ""

    @property
    def is_self(self) -> bool:
        return self.matched and self.speaker_id is not None

    def as_dict(self) -> dict[str, object]:
        return {"matched": self.matched, "score": round(self.score, 4),
                "threshold": self.threshold, "speaker_id": self.speaker_id,
                "reason": self.reason}


def judge_voiceprint(score: float | None, *, threshold: float = VOICEPRINT_THRESHOLD,
                     speaker_id: str | None = None) -> VoiceprintVerdict:
    """把「分数」判成「结论」。**分数缺失一律判不匹配**，不允许 None 当通过。

    这条是 U63 的红线：声纹是身份凭据，任何「取不到分数就放行」的实现
    都等于没有声纹。
    """
    if score is None:
        return VoiceprintVerdict(False, 0.0, threshold, None, "未取得声纹分数，判为不匹配")
    if score < threshold:
        return VoiceprintVerdict(False, float(score), threshold, speaker_id,
                                 f"声纹相似度 {score:.3f} 低于阈值 {threshold:.3f}")
    return VoiceprintVerdict(True, float(score), threshold, speaker_id, "声纹校验通过")


def describe_matrix() -> dict[str, list[str]]:
    """能力表的人读形式，供自检与端侧渲染（端侧不得自己写一份矩阵）。"""
    return {tier.value: sorted(c.value for c in caps)
            for tier, caps in TIER_CAPABILITIES.items()}
