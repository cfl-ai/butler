"""输入风控闸门：防「虚假信息」与「编码混淆」。

为什么要有它
------------
用户指令：「做好风控（不能被用户以虚假信息或者 base 代码混淆）」。

**这不是第六层意图路由。** 五层路由链路已由计划书【八】冻结
（表 6：硬规则 -> Jev -> 向量 -> 垂类模型 -> 大模型兜底），不得增删。
本模块是一道**前置闸门**：在进入路由之前对用户原文做风险判定，
结论只有三种 ——

    ALLOW  放行，原样进入五层路由
    TAINT  放行，但打上「不可信」标记：可参与对话，**不得直接沉淀为事实**
    BLOCK  拒绝，本次业务不进入路由

两类风险
--------
1. **编码混淆**（「base 代码」）：把指令藏进 base64 / 十六进制 / 百分号编码 /
   Unicode 转义里，绕过关键词筛查。对策是**逐层解码后再扫一遍**，
   并设深度上限，防止解压炸弹式的递归。
2. **虚假信息**：用户把未经核实的内容说成「已确认 / 官方」，
   或直接给出结论要求系统采信。对策是**不判真假，只判可信度**：
   一律标记为未验证，由下游（P6 质检 / Agent-Wiki 证据链）决定能否入库。

**判真假不是本模块的职责。** 它只回答「这条输入能不能作为事实依据」。

开源借鉴
--------
风险分类参照 OWASP Top 10 for LLM Applications（LLM01 Prompt Injection、
LLM09 Misinformation）的分档思路；不引入其代码，仅借鉴分类法（见台账 U41）。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any


class InputGuardError(RuntimeError):
    """输入被闸门拒绝。"""


# ---------------- 判定结论 ---------------- #
ALLOW = "allow"
TAINT = "taint"
BLOCK = "block"

ACTION_NAMES: tuple[str, ...] = (ALLOW, TAINT, BLOCK)

#: 单层解码出的候选载荷最短长度（太短的 base64 太容易误报）
MIN_ENCODED_LEN = 16
#: 最多解几层（防递归炸弹）
MAX_DECODE_DEPTH = 3
#: 单次输入长度上限（防超长载荷拖垮服务）
MAX_INPUT_CHARS = 20_000


@dataclass(frozen=True)
class Finding:
    """一条风险发现。"""

    kind: str                 # encoded_payload / instruction_injection / unverified_claim
    action: str               # 该发现建议的处置
    reason: str
    evidence: str = ""        # 命中片段（已截断，避免把隐私写进日志）


@dataclass(frozen=True)
class GuardVerdict:
    """闸门结论。"""

    action: str
    findings: tuple[Finding, ...] = ()
    decoded: tuple[tuple[str, str], ...] = ()   # ((编码方式, 解码结果), ...)

    @property
    def allowed(self) -> bool:
        return self.action != BLOCK

    @property
    def untrusted(self) -> bool:
        """是否**不得**作为事实依据 —— TAINT 与 BLOCK 都算。"""
        return self.action in (TAINT, BLOCK)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f.reason for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "untrusted": self.untrusted,
            "findings": [
                {"kind": f.kind, "action": f.action, "reason": f.reason,
                 "evidence": f.evidence}
                for f in self.findings
            ],
            "decoded_layers": len(self.decoded),
        }


# ---------------- 编码混淆 ---------------- #
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_B64URL = re.compile(r"[A-Za-z0-9_-]{16,}={0,2}")
_HEX = re.compile(r"(?:\\x[0-9a-fA-F]{2}){4,}|(?:[0-9a-fA-F]{2}){8,}")
_PERCENT = re.compile(r"(?:%[0-9A-Fa-f]{2}){4,}")
_UNI_ESC = re.compile(r"(?:\\u[0-9a-fA-F]{4}){4,}")


def _try_base64(text: str) -> str | None:
    for pattern in (_B64, _B64URL):
        for match in pattern.finditer(text):
            raw = match.group(0)
            if len(raw) < MIN_ENCODED_LEN:
                continue
            padded = raw + "=" * (-len(raw) % 4)
            data = None
            for fn in (base64.b64decode, base64.urlsafe_b64decode):
                try:
                    data = fn(padded, validate=True) if fn is base64.b64decode else fn(padded)
                except (binascii.Error, ValueError):
                    continue
                break
            if data is None:
                continue
            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not decoded.strip():
                continue
            printable = sum(1 for ch in decoded if ch.isprintable() or ch.isspace())
            if printable / max(len(decoded), 1) >= 0.9:
                return decoded
    return None


def _try_hex(text: str) -> str | None:
    match = _HEX.search(text)
    if not match:
        return None
    raw = match.group(0).replace("\\x", "")
    if len(raw) % 2:
        raw = raw[:-1]
    try:
        decoded = bytes.fromhex(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if decoded.strip() else None


def _try_percent(text: str) -> str | None:
    match = _PERCENT.search(text)
    if not match:
        return None
    from urllib.parse import unquote

    decoded = unquote(match.group(0))
    return decoded if decoded.strip() else None


def _try_unicode_escape(text: str) -> str | None:
    match = _UNI_ESC.search(text)
    if not match:
        return None
    try:
        decoded = match.group(0).encode("utf-8").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return None
    return decoded if decoded.strip() else None


_DECODERS = (
    ("base64", _try_base64),
    ("hex", _try_hex),
    ("percent", _try_percent),
    ("unicode_escape", _try_unicode_escape),
)


def decode_layers(text: str, *, max_depth: int = MAX_DECODE_DEPTH) -> list[tuple[str, str]]:
    """逐层解码，返回 [(编码方式, 解码结果), ...]。

    一旦某层没有新内容就停止 —— 防无限递归。
    """
    found: list[tuple[str, str]] = []
    seen = {text}
    current = text
    for _ in range(max_depth):
        progressed = False
        for name, decoder in _DECODERS:
            decoded = decoder(current)
            if decoded and decoded not in seen:
                seen.add(decoded)
                found.append((name, decoded))
                current = decoded
                progressed = True
                break
        if not progressed:
            break
    return found


# ---------------- 指令注入 ----------------
#: 高置信注入话术：命中即 BLOCK（直接终止本次业务）。
#: 用中英双语，因为这些是跨语言的通用越权表达。
INJECTION_HARD: tuple[tuple[str, str], ...] = (
    (r"忽略(?:以上|之前|上面|前面|所有)(?:的)?(?:全部)?(?:指令|要求|提示|设定)",
     "要求忽略既有指令"),
    (r"(?:无视|不要理会|别管)(?:以上|之前|上面|前面)(?:的)?(?:指令|要求|提示)",
     "要求无视既有指令"),
    (r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)",
     "要求忽略既有指令"),
    (r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)",
     "要求忽略既有指令"),
    (r"(?:打印|输出|显示|告诉我|重复)(?:一下)?(?:你的)?(?:系统|初始|隐藏)(?:提示词|指令|prompt)",
     "索取系统提示词"),
    # 中文常把动词放到后面：「系统提示词打印出来」
    (r"(?:系统|初始|隐藏)(?:提示词|指令|prompt)(?:给我|打印|输出|显示|发给我)",
     "索取系统提示词"),
    (r"(?:reveal|print|show|repeat)\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions)",
     "索取系统提示词"),
    (r"(?:你现在是|从现在起你是|扮演)(?:一个)?(?:不受限制|无限制|越狱|DAN)",
     "要求切换为无限制角色"),
    (r"developer\s+mode|jailbreak|do\s+anything\s+now",
     "要求进入越狱模式"),
)

#: 可疑但不致命的表达：命中即 TAINT（不阻断，但不得作为事实依据）。
INJECTION_SOFT: tuple[tuple[str, str], ...] = (
    (r"(?:请)?(?:扮演|假设你是|假装你是)", "要求角色扮演"),
    (r"(?:以|用)(?:管理员|开发者|系统)(?:身份|权限)(?:回答|执行)", "要求提权"),
    (r"act\s+as\s+(?:an?\s+)?(?:admin|developer|system)", "要求提权"),
)

# ---------------- 未经验证的事实断言 ----------------
#: 用户把未经核实的内容说成已确认。**不判真假**，只降低可信度。
UNVERIFIED_CLAIMS: tuple[tuple[str, str], ...] = (
    (r"(?:已|已经)(?:确认|核实|验证|证实|核准)(?:过)?", "声称内容已核实但未给证据"),
    (r"官方(?:已)?(?:确认|证实|公布|宣布)", "声称官方确认但未给来源"),
    (r"(?:百分之百|100%|绝对|肯定|一定)(?:是|正确|没错)", "绝对化断言"),
    (r"(?:据|根据)(?:可靠|内部|权威)(?:消息|人士|来源)", "来源不可核验"),
    (r"我自己(?:已经)?(?:查过|验证过|试过)", "以个人声明替代证据"),
)

#: 证据位：出现这些词说明用户**给了可核验的依据**，可抵消一部分 TAINT。
EVIDENCE_HINTS = (
    r"https?://", r"doi:", r"arxiv", r"wiki/", r"来源[:：]",
    r"文件[:：]", r"附件", r"截图", r"原文[:：]",
)


def _compile(pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple((re.compile(p, re.IGNORECASE), why) for p, why in pairs)


_INJECTION_HARD = _compile(INJECTION_HARD)
_INJECTION_SOFT = _compile(INJECTION_SOFT)
_UNVERIFIED = _compile(UNVERIFIED_CLAIMS)
_EVIDENCE = re.compile("|".join(EVIDENCE_HINTS), re.IGNORECASE)

#: 证据片段在日志里的最大长度
MAX_EVIDENCE_CHARS = 60


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat[:MAX_EVIDENCE_CHARS] + ("…" if len(flat) > MAX_EVIDENCE_CHARS else "")


def _scan(text: str, patterns: tuple[tuple[re.Pattern[str], str], ...],
          *, kind: str, action: str) -> list[Finding]:
    out: list[Finding] = []
    for pattern, reason in patterns:
        match = pattern.search(text)
        if match:
            out.append(Finding(kind=kind, action=action, reason=reason,
                               evidence=_clip(match.group(0))))
    return out


@dataclass(frozen=True)
class InputGuard:
    """前置风控闸门。

    用法：

        guard = InputGuard()
        verdict = guard.guard(user_text)
        if not verdict.allowed:
            ...            # 拒绝，不进入路由
        if verdict.untrusted:
            ...            # 可对话，但产出的事实不得直接入库
    """

    #: 带证据位的输入可以免除「未验证」标记
    honor_evidence: bool = True
    #: 出现编码载荷即 TAINT（即便解出来没发现注入）
    taint_on_any_encoding: bool = True
    #: 超过这个长度直接拒（防超长载荷）
    max_chars: int = MAX_INPUT_CHARS

    def guard(self, text: str) -> GuardVerdict:
        if not isinstance(text, str):
            raise InputGuardError("输入必须是字符串")
        if len(text) > self.max_chars:
            return GuardVerdict(action=BLOCK, findings=(
                Finding("oversized", BLOCK,
                        f"输入长度 {len(text)} 超过上限 {self.max_chars}"),
            ))

        findings: list[Finding] = []

        # 1) 编码载荷：先解，再对**解出来的内容**重新扫一遍
        decoded = decode_layers(text)
        if decoded:
            names = ",".join(name for name, _ in decoded)
            inner = decoded[-1][1]
            inner_findings = self._scan_all(inner, kind_prefix="encoded:")
            if any(f.action == BLOCK for f in inner_findings):
                findings.extend(inner_findings)
                findings.append(Finding(
                    "encoded_payload", BLOCK,
                    f"编码载荷（{names}）解出后含越权指令",
                    evidence=_clip(inner)))
            elif self.taint_on_any_encoding:
                findings.append(Finding(
                    "encoded_payload", TAINT,
                    f"输入含编码载荷（{names}），解码结果不作为事实依据",
                    evidence=_clip(inner)))
                findings.extend(inner_findings)

        # 2) 明文注入
        findings.extend(self._scan_all(text, kind_prefix=""))

        # 3) 未验证断言：有证据位就不算
        if not (self.honor_evidence and _EVIDENCE.search(text)):
            findings.extend(_scan(text, _UNVERIFIED,
                                  kind="unverified_claim", action=TAINT))

        return GuardVerdict(
            action=self._decide(findings),
            findings=tuple(findings),
            decoded=tuple(decoded),
        )

    # ---------------- 内部 ----------------
    def _scan_all(self, text: str, *, kind_prefix: str) -> list[Finding]:
        hard = _scan(text, _INJECTION_HARD, kind="instruction_injection", action=BLOCK)
        soft = _scan(text, _INJECTION_SOFT, kind="instruction_injection", action=TAINT)
        out = hard + soft
        if kind_prefix:
            out = [Finding(kind_prefix + f.kind, f.action, f.reason, f.evidence)
                   for f in out]
        return out

    @staticmethod
    def _decide(findings: list[Finding]) -> str:
        if any(f.action == BLOCK for f in findings):
            return BLOCK
        if any(f.action == TAINT for f in findings):
            return TAINT
        return ALLOW


#: 进程级默认闸门
DEFAULT_GUARD = InputGuard()


def guard_input(text: str, *, guard: InputGuard | None = None) -> GuardVerdict:
    """便捷入口。"""
    return (guard or DEFAULT_GUARD).guard(text)
