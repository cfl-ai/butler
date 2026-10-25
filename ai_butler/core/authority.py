"""源码修改授权：哪台机器、哪个主体有权让离线闭环改源码（U88）。

计划书依据
----------
- 三、P10 离线链路「代码迭代 Agent 分析问题、生成优化方案 → Hook + 质检风险校验
  → 沙箱执行代码修改与自测」——链路能改源码，但**计划书没有定义「谁有权让它改」**
- 三、P10 触发时机「7×24 小时后台离线自动运行，不占用业务算力」

人工授权（2026-10-25，用户指令）
--------------------------------
「以本地这台机器作为有权改源码（可通过本台机器修改转让权限等）」。

据此裁定 **U88**：源码修改权是一项**显式授予、限期有效、可吊销、可转让**的授权，
登记在本机授权簿里；授权簿与一台机器绑定，复制文件到别的机器不生效。

这个模块管什么、不管什么
------------------------
| 管（本模块） | 不管（别的模块） |
|---|---|
| 哪个**主体**有权改源码 | 改哪些文件永远不许（`harness/patch.py` 的 `FORBIDDEN_PATHS`） |
| 哪台**机器**上的授权有效 | 本轮补丁是否越界（`PatchGuard`，每次应用前判） |
| 授权**何时到期**、被谁吊销、转让给谁 | 改完有没有修好（评测中台复评） |

两层是**独立**的：授权簿回答「该不该让这台机器上的这个主体启动闭环」，
`PatchGuard` 回答「这一份 diff 能不能应用」。少了前者，任何能摸到键盘的人
都能启动闭环；少了后者，授权内的补丁照样能改测试文件。

四条纪律
--------
1. **机器绑定靠密钥，不靠「在本机」这三个字。** 授权根目录下有一把
   `machine.key`（随机生成，只在本机）。每条授权用它对内容签名；
   签名不符 = 授权簿被手改过，或整份文件是从别的机器拷来的——
   两种都拒，且**不尝试修复**（修不好：不知道原文是什么）。
2. **限期是强制的**，没有「永久授权」。到期即拒，理由里写明怎么续期。
   无人应答即拒与审批票 TTL（U59）是同一立场：权限放得越久，
   越没人记得它为什么被放。
3. **fail-closed，且拒绝理由要能行动。** 没有授权 / 过期 / 吊销 / 签名不符 /
   范围不含——每种都给出对应的下一步命令，不是一句「无权」。
4. **状态迁移由本模块重签。** active → expired / revoked / transferred
   都由授权簿自己完成并重新签名：签名证明的是「这条记录出自这台机器的授权簿」，
   不是「这条记录自从写下就没变过」——后者做不到，也不该假装。

诚实的边界
----------
- **不是硬件 attestation**：机器绑定=本机的一把随机密钥。拿走 `machine.key`
  就等于拿走这台机器的授权能力（和拿走 SSH 私钥同性质）。Windows 上文件权限位
  （0600）只是尽力而为，真正的边界是目录 ACL 与「谁能登录这台机器」。
- **不跨机器**：授权簿可以转让给另一个**主体**，但转让后的授权仍然只在本机有效
  （签名用的是本机密钥）。多机共用一份授权需要分布式协调，不在本模块能力内
  （同 `core/filelock.py` 的边界一节）。
- **不决定补丁内容**：授权范围是路径前缀的**上限**，不是任务清单。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ai_butler.core.filelock import FileLock

__all__ = [
    "ACTIVE",
    "EXPIRED",
    "GRANTS_NAME",
    "MACHINE_KEY_NAME",
    "REVOKED",
    "TRANSFERRED",
    "AuthorityError",
    "AuthorityRegistry",
    "GrantVerdict",
    "MachineIdentity",
    "SourceGrant",
]

#: 授权簿状态机（单向，同 U59 的立场）
ACTIVE = "active"
EXPIRED = "expired"          # 到期自动转入（授权时清扫）
REVOKED = "revoked"          # 人工吊销
TRANSFERRED = "transferred"  # 已转让给另一主体（链上留痕）

MACHINE_KEY_NAME = "machine.key"
GRANTS_NAME = "grants.json"
GRANTS_LOCK_NAME = "grants.lock"


class AuthorityError(RuntimeError):
    """授权缺失 / 不符 / 簿损坏。**一律抛出**，不静默降级为「放行但记一笔」。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm(path: str) -> str:
    """归一成 POSIX 相对路径：反斜杠、`./` 前缀、尾部斜杠、首尾空白都统一掉。

    不归一的后果是同一份授权在 Windows 与 Linux 下覆盖范围不同——
    而「范围」正是这个模块唯一要判准的东西。
    前导 `/` **不剥**：绝对路径本来就不是仓库相对路径，归一了反而会
    悄悄匹配上前缀，那是把越界路径洗成合法。
    """
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _parse_iso(value: str, *, what: str) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise AuthorityError(f"{what} 不是合法 ISO 时间：{value!r}") from exc
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# ================================================================== #
# 一、机器身份                                                        #
# ================================================================== #
@dataclass(frozen=True)
class MachineIdentity:
    """一台机器的身份：`machine_id` 给人看，`secret` 给签名用。

    `load_or_create` 用 `O_EXCL` 建密钥文件：两个进程同时首跑时，
    后到者加载先到者写好那份，而不是各自生成一把——
    各自生成的后果是两份授权互不认得，而两边看起来都「成功了」。
    """

    root: Path
    machine_id: str
    secret: str

    @classmethod
    def load_or_create(cls, root: Path | str) -> "MachineIdentity":
        base = Path(root)
        base.mkdir(parents=True, exist_ok=True)
        path = base / MACHINE_KEY_NAME
        if path.exists():
            return cls._load(path, base)
        payload = json.dumps(
            {"machine_id": uuid.uuid4().hex, "secret": secrets.token_hex(32)},
            ensure_ascii=False)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:                      # 别人抢先建好了
            return cls._load(path, base)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        data = json.loads(payload)
        return cls(root=base, machine_id=str(data["machine_id"]),
                   secret=str(data["secret"]))

    @classmethod
    def _load(cls, path: Path, base: Path) -> "MachineIdentity":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            machine_id, secret = str(data["machine_id"]), str(data["secret"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AuthorityError(
                f"{path} 损坏或不是本模块的密钥文件（{type(exc).__name__}）。"
                f"**不要手改它**：改坏意味着本机所有授权都验不过。"
                f"若确认要作废全部授权重新开始，删掉该文件会生成新的机器身份，"
                f"既有授权簿随之全部失效——那是一次故意的重置，不是修复。") from exc
        if not machine_id.strip() or not secret.strip():
            raise AuthorityError(f"{path} 缺少 machine_id 或 secret")
        return cls(root=base, machine_id=machine_id, secret=secret)

    # ------------------------------------------------------------------ #
    def sign(self, payload: Mapping[str, Any]) -> str:
        """对一份可 JSON 化的载荷签名。同一载荷永远得到同一签名。"""
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
        return hmac.new(self.secret.encode("utf-8"), body.encode("utf-8"),
                        sha256).hexdigest()

    def verify(self, payload: Mapping[str, Any], signature: str) -> bool:
        if not signature:
            return False
        return hmac.compare_digest(self.sign(payload), signature)


# ================================================================== #
# 二、一条授权                                                        #
# ================================================================== #
@dataclass(frozen=True)
class SourceGrant:
    """一条源码修改授权。frozen——状态迁移经 `replace()` 产生新实例并重签。"""

    subject: str
    allowed_prefixes: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    granted_by: str
    granted_at: str
    expires_at: str
    note: str = ""
    status: str = ACTIVE
    signature: str = ""
    #: 转让链：这条授权是从哪个主体转来的；转出去时记 `transferred_to`
    supersedes: str = ""
    transferred_to: str = ""

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise AuthorityError("授权主体（subject）不得为空——无主体的授权无从判定")
        if not self.granted_by.strip():
            raise AuthorityError(
                "授权必须署名（granted_by 不得为空）——匿名放行不可审计（U59 同一立场）")
        if not self.allowed_prefixes:
            raise AuthorityError(
                "授权范围不得为空：没有范围的授权等于全部放开，那不是授权是弃权")
        object.__setattr__(self, "allowed_prefixes",
                           tuple(_norm(p) for p in self.allowed_prefixes))
        object.__setattr__(self, "forbidden_paths",
                           tuple(_norm(p) for p in self.forbidden_paths))
        # 解析结果缓存进非字段属性：frozen 的 __setattr__ 连属性 setter 都拦，
        # 只能走 object.__setattr__；读走普通 property，不涉及赋值。
        object.__setattr__(self, "_expires_at",
                           _parse_iso(self.expires_at, what="expires_at"))
        if self.status not in (ACTIVE, EXPIRED, REVOKED, TRANSFERRED):
            raise AuthorityError(f"未知授权状态 {self.status!r}")

    @property
    def expires_at_dt(self) -> datetime:
        """`expires_at` 的解析结果（UTC）。构造时已校验，这里不会抛。"""
        return self._expires_at                                  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    def covers(self, path: str) -> bool:
        """这个路径在授权范围内吗。**禁区优先于范围**（deny overrides）。"""
        target = _norm(path)
        if not target:
            return False
        for forbidden in self.forbidden_paths:
            if target == forbidden or target.startswith(forbidden + "/"):
                return False
        for allowed in self.allowed_prefixes:
            if target == allowed or target.startswith(allowed + "/"):
                return True
        return False

    def signable(self) -> dict[str, Any]:
        """参与签名的载荷：除 `signature` 外的全部字段。"""
        return {k: v for k, v in self.as_dict().items() if k != "signature"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "allowed_prefixes": list(self.allowed_prefixes),
            "forbidden_paths": list(self.forbidden_paths),
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "note": self.note,
            "status": self.status,
            "signature": self.signature,
            "supersedes": self.supersedes,
            "transferred_to": self.transferred_to,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceGrant":
        if not isinstance(payload, Mapping):
            raise AuthorityError(
                f"授权记录应为 dict，实际 {type(payload).__name__}")
        required = ("subject", "allowed_prefixes", "granted_by",
                    "granted_at", "expires_at")
        missing = [k for k in required if k not in payload]
        if missing:
            raise AuthorityError(f"授权记录缺字段 {missing}：{dict(payload)!r}")
        return cls(
            subject=str(payload["subject"]),
            allowed_prefixes=tuple(str(p) for p in (payload["allowed_prefixes"] or ())),
            forbidden_paths=tuple(str(p) for p in (payload.get("forbidden_paths") or ())),
            granted_by=str(payload["granted_by"]),
            granted_at=str(payload["granted_at"]),
            expires_at=str(payload["expires_at"]),
            note=str(payload.get("note") or ""),
            status=str(payload.get("status") or ACTIVE),
            signature=str(payload.get("signature") or ""),
            supersedes=str(payload.get("supersedes") or ""),
            transferred_to=str(payload.get("transferred_to") or ""))


@dataclass(frozen=True)
class GrantVerdict:
    """一次授权核查的结论。拒绝时也要说清**下一步做什么**。"""

    allowed: bool
    reason: str
    grant: SourceGrant | None = None

    def raise_if_denied(self, *, action: str) -> None:
        if self.allowed:
            return
        raise AuthorityError(f"拒绝「{action}」：{self.reason}")


# ================================================================== #
# 三、授权簿                                                          #
# ================================================================== #
@dataclass
class AuthorityRegistry:
    """本机的源码修改授权簿。**唯一的授权真值源。**

    文件布局（授权根目录下）：

        machine.key    机器密钥（随机，只在本机；**严禁入库存分享**）
        grants.json    授权簿（数组；状态迁移留痕，不删记录）
        grants.lock    写互斥（U86：锁跨越「读-判-写」整段）

    读不持锁：保存是「临时文件 + os.replace」的原子替换，读者只会看到
    改之前或改之后的完整文件，看不到半份。
    """

    root: Path
    identity: MachineIdentity | None = None
    clock: Callable[[], datetime] = _utcnow
    lock_timeout: float = 5.0
    _grants_path: Path = field(init=False, repr=False)
    _lock: FileLock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.identity = self.identity or MachineIdentity.load_or_create(self.root)
        self._grants_path = self.root / GRANTS_NAME
        self._lock = FileLock(self.root / GRANTS_LOCK_NAME,
                              timeout=self.lock_timeout, purpose="源码修改授权簿")

    # ------------------------------------------------------------------ #
    # 读                                                                  #
    # ------------------------------------------------------------------ #
    def grants(self) -> tuple[SourceGrant, ...]:
        """授权簿全部记录（含历史）。**坏一条拒全部**（fail-closed）。"""
        return tuple(self._load())

    def _load(self) -> list[SourceGrant]:
        if not self._grants_path.exists():
            return []
        try:
            raw = json.loads(self._grants_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuthorityError(
                f"{self._grants_path} 不是合法 JSON（{type(exc).__name__}）："
                f"授权簿损坏，拒绝加载。手改出来的损坏无法猜回原文，"
                f"请从备份恢复或删除后重新授权。") from exc
        if not isinstance(raw, list):
            raise AuthorityError(
                f"{self._grants_path} 顶层应为数组，实际 {type(raw).__name__}")
        return [SourceGrant.from_dict(item) for item in raw]

    def _save(self, grants: Sequence[SourceGrant]) -> None:
        payload = json.dumps([g.as_dict() for g in grants], ensure_ascii=False,
                             indent=2)
        tmp = self._grants_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._grants_path)

    # ------------------------------------------------------------------ #
    # 核查                                                                #
    # ------------------------------------------------------------------ #
    def check(self, subject: str, *, paths: Sequence[str] = (),
              now: datetime | None = None) -> GrantVerdict:
        """这个主体在本机有权改源码吗。**读路径，不写状态**。"""
        moment = (now or self.clock())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        candidates: list[SourceGrant] = []
        for grant in self.grants():
            if grant.subject != subject or grant.status != ACTIVE:
                continue
            if not self.identity.verify(grant.signable(), grant.signature):
                return GrantVerdict(
                    False,
                    "授权签名不符：授权簿被改过，或这份授权簿是从别的机器拷来的"
                    "（机器密钥不匹配）。两种都不放行——不尝试猜测原文")
            candidates.append(grant)
        if not candidates:
            return GrantVerdict(
                False,
                f"主体 {subject!r} 没有有效授权。本机未授权它修改源码；"
                f"授权：scripts/run_repair_loop.py grant --subject {subject}")
        if len(candidates) > 1:
            # 不该发生（grant 拒绝重复授权）；真出现了就是「撤哪份」说不清
            return GrantVerdict(
                False,
                f"主体 {subject!r} 有 {len(candidates)} 条活跃授权，状态不可判定。"
                f"请检查 {self._grants_path} 并吊销多余的那条")
        grant = candidates[0]
        if moment >= grant.expires_at_dt:
            return GrantVerdict(
                False,
                f"授权已于 {grant.expires_at} 到期（不存在永久授权，同 U59 的 TTL 立场）。"
                f"续期：重新执行 grant --subject {subject}")
        missing = [p for p in paths if not grant.covers(p)]
        if missing:
            return GrantVerdict(
                False,
                f"授权范围不含 {missing}：授权覆盖 {list(grant.allowed_prefixes)}，"
                f"禁区 {list(grant.forbidden_paths)}")
        return GrantVerdict(True, "授权有效", grant)

    # ------------------------------------------------------------------ #
    # 写：授权 / 转让 / 吊销                                              #
    # ------------------------------------------------------------------ #
    def grant(self, subject: str, *, allowed_prefixes: Sequence[str],
              forbidden_paths: Sequence[str] = (), granted_by: str,
              ttl_seconds: float, note: str = "",
              now: datetime | None = None) -> SourceGrant:
        """授予一份源码修改授权。**限期强制**，重复授权需先吊销。"""
        if ttl_seconds <= 0:
            raise AuthorityError(
                f"ttl_seconds 必须为正，实际 {ttl_seconds}：没有限期的授权"
                f"会被永久遗忘，而没人记得它为什么被放")
        moment = (now or self.clock())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        with self._lock:
            grants = self._load()
            grants = self._sweep(grants, moment)
            live = [g for g in grants
                    if g.subject == subject and g.status == ACTIVE]
            if live:
                raise AuthorityError(
                    f"主体 {subject!r} 已有未过期授权（至 {live[0].expires_at}，"
                    f"by {live[0].granted_by}）。重复授权会得到两份范围可能不同的"
                    f"授权，吊销时「撤哪份」说不清——先 revoke，或等它到期")
            grant = SourceGrant(
                subject=subject,
                allowed_prefixes=tuple(allowed_prefixes),
                forbidden_paths=tuple(forbidden_paths),
                granted_by=granted_by,
                granted_at=moment.isoformat(),
                expires_at=(moment + timedelta(seconds=float(ttl_seconds))).isoformat(),
                note=note)
            signed = replace(grant, signature=self.identity.sign(grant.signable()))
            grants.append(signed)
            self._save(grants)
            return signed

    def transfer(self, from_subject: str, to_subject: str, *, by: str,
                 ttl_seconds: float | None = None, note: str = "",
                 now: datetime | None = None) -> SourceGrant:
        """把授权转让给另一个主体。**范围照搬，剩余期限照搬**（不自动延期）。

        旧授权记 `transferred` 并留下 `transferred_to`：授权链在簿里看得见，
        「这个主体手上的权是从谁那来的」是一个可以回答的问题。
        """
        moment = (now or self.clock())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        with self._lock:
            grants = self._load()
            grants = self._sweep(grants, moment)
            source = next((g for g in grants
                           if g.subject == from_subject and g.status == ACTIVE), None)
            if source is None:
                # 区分「到期」与「从来没有/已吊销/已转出」：两者的下一步不同
                stale = next((g for g in grants
                              if g.subject == from_subject
                              and g.status == EXPIRED), None)
                if stale is not None:
                    raise AuthorityError(
                        f"主体 {from_subject!r} 的授权已于 {stale.expires_at} 到期，"
                        f"不能转让。续期请重新 grant")
                raise AuthorityError(
                    f"主体 {from_subject!r} 没有可转让的活跃授权（无授权 / "
                    f"已吊销 / 已转出）。转让不是续期：过期的授权请重新 grant")
            if source.expires_at_dt <= moment:
                raise AuthorityError(
                    f"主体 {from_subject!r} 的授权已于 {source.expires_at} 到期，"
                    f"不能转让。续期请重新 grant")
            if any(g.subject == to_subject and g.status == ACTIVE for g in grants):
                raise AuthorityError(
                    f"受让方 {to_subject!r} 已有活跃授权：一份主体一份权，"
                    f"先吊销它的再转让")
            # 剩余期限照搬：ttl_seconds 给了才延长，且必须为正
            if ttl_seconds is not None:
                if ttl_seconds <= 0:
                    raise AuthorityError("转让延长有效期必须为正")
                expires_at = (moment + timedelta(seconds=float(ttl_seconds))).isoformat()
            else:
                expires_at = source.expires_at
            grant = SourceGrant(
                subject=to_subject,
                allowed_prefixes=source.allowed_prefixes,
                forbidden_paths=source.forbidden_paths,
                granted_by=by,
                granted_at=moment.isoformat(),
                expires_at=expires_at,
                note=note or f"自 {from_subject} 转让",
                supersedes=from_subject)
            signed = replace(grant, signature=self.identity.sign(grant.signable()))
            moved = replace(source, status=TRANSFERRED, transferred_to=to_subject)
            moved = replace(moved,
                            signature=self.identity.sign(moved.signable()))
            grants = [moved if g is source else g for g in grants]
            grants.append(signed)
            self._save(grants)
            return signed

    def revoke(self, subject: str, *, by: str, reason: str) -> SourceGrant:
        """吊销授权。**记录不删**：吊销过谁、为什么，是审计要回答的问题。"""
        if not by.strip():
            raise AuthorityError("吊销必须署名（by 不得为空）——匿名吊销不可审计")
        with self._lock:
            grants = self._load()
            target = next((g for g in grants
                           if g.subject == subject and g.status == ACTIVE), None)
            if target is None:
                raise AuthorityError(
                    f"主体 {subject!r} 没有可吊销的活跃授权（可能从未授权、"
                    f"或已吊销/已过期/已转出）")
            revoked = replace(
                target, status=REVOKED,
                note=(f"{target.note}｜吊销：{reason}（by {by}）"
                      if target.note else f"吊销：{reason}（by {by}）"))
            revoked = replace(revoked,
                              signature=self.identity.sign(revoked.signable()))
            grants = [revoked if g is target else g for g in grants]
            self._save(grants)
            return revoked

    # ------------------------------------------------------------------ #
    # 内部                                                                #
    # ------------------------------------------------------------------ #
    def _sweep(self, grants: list[SourceGrant],
               moment: datetime) -> list[SourceGrant]:
        """把到期未转状态的活跃授权标为 expired（**重签**：迁移出自本簿）。

        只在写路径上做：读路径（`check`）不写文件，到期与否当场算。
        """
        out: list[SourceGrant] = []
        for grant in grants:
            if grant.status == ACTIVE and moment >= grant.expires_at_dt:
                expired = replace(grant, status=EXPIRED)
                expired = replace(
                    expired, signature=self.identity.sign(expired.signable()))
                out.append(expired)
            else:
                out.append(grant)
        return out

    # ------------------------------------------------------------------ #
    # 观测                                                                #
    # ------------------------------------------------------------------ #
    def status(self) -> dict[str, Any]:
        """授权簿摘要（给 CLI 的 `status` 子命令）。不含密钥。

        `tampered` 是**观测项不是操作项**：历史记录被手改过不阻塞操作
        （它们不参与判定），但要让人看见「这本簿被动过」。
        """
        grants = self.grants()
        tampered = [g for g in grants
                    if not self.identity.verify(g.signable(), g.signature)]
        return {
            "machine_id": self.identity.machine_id,
            "grants_file": str(self._grants_path),
            "counts": {status: sum(1 for g in grants if g.status == status)
                       for status in (ACTIVE, EXPIRED, REVOKED, TRANSFERRED)},
            "tampered": [
                {"subject": g.subject, "status": g.status,
                 "granted_at": g.granted_at} for g in tampered],
            "active": [
                {"subject": g.subject,
                 "allowed_prefixes": list(g.allowed_prefixes),
                 "forbidden_paths": list(g.forbidden_paths),
                 "granted_by": g.granted_by, "granted_at": g.granted_at,
                 "expires_at": g.expires_at, "note": g.note,
                 "supersedes": g.supersedes, "transferred_to": g.transferred_to}
                for g in grants if g.status == ACTIVE],
        }
