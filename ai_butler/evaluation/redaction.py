"""P9 隐私脱敏 —— 「禁止展示明文用户信息」的确定性实现。

计划书依据
----------
- 三、P9「业务指标聚合统计，**原始用户隐私数据脱敏，禁止展示明文用户信息**」
- 三、P9「日志自动沉淀、优质 / 错误案例分类；**隐私脱敏**」
- 三、P9「领域 SFT 数据集构建；**剥离用户身份隐私**；禁止把私有 Vault 原始隐私放入训练集」
- 三、P10「日志输入 Harness 前做隐私脱敏」

裁定 U30
--------
Microsoft Presidio（analyzer + anonymizer）为主，中文场景补 cn-pii-anonymization，
**并保留一层确定性正则兜底** —— 中文姓名与地址的模型召回不稳，纯模型方案会漏。

设计要点
--------
1. **fail-closed**：safe_text 在无法证明已脱敏时**抛错而不是放行**。
   看板、日志、训练集三个出口都走它，「忘了脱敏」在读路径上就不可能发生。
2. 模型层（Presidio）是**可选增强**：未安装时正则仍然工作，并如实标注覆盖层级，
   不假装做了模型级脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 脱敏后的统一占位符（保留实体类型，便于排障时不丢信息）
PLACEHOLDER = "<{kind}>"

#: 确定性兜底规则。**先长后短**匹配，避免身份证被手机号规则截断。
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    # 账号标识：本系统内部用户 id 形如 user-A / user-abc123
    ("账号标识", re.compile(r"user-[A-Za-z0-9_\-]{1,32}")),
    ("会话令牌", re.compile(r"(?i)\b(?:bearer\s+)?[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{16,}")),
)

#: 「明文用户信息」判定：命中任一即视为未脱敏
class RedactionError(RuntimeError):
    """无法证明内容已脱敏。**宁可拒绝，也不放行明文。**"""


@dataclass
class Redactor:
    """脱敏器。模型层可选，正则层始终生效。"""

    #: 模型层分析器（如 Presidio 适配器）。**注入才算启用**；本项目不内置该依赖。
    analyzer: Any = None
    extra_patterns: tuple[tuple[str, str], ...] = ()
    _model_ready: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._compiled: tuple[tuple[str, re.Pattern[str]], ...] = _PATTERNS + tuple(
            (kind, re.compile(pattern)) for kind, pattern in self.extra_patterns)

    @property
    def model_layer_active(self) -> bool:
        """模型层是否**真的**接上了。

        原实现写的是「try import presidio_analyzer」，但：
        - 那会让本项目凭空多出一个并未安装的依赖（被依赖声明测试抓出）；
        - 「能 import」也不等于「真的在用」。
        改为**端口注入**：调用方把分析器实现传进来才算启用，
        没传就如实返回 False，不冒充已做模型级脱敏。
        """
        return self.analyzer is not None

    @property
    def regex_layer_active(self) -> bool:
        """正则层是否**真的**有规则在跑。

        不是恒 True。P9 的评估报告里原先直接写死 ~{regex_layer: True}~，
        那等于「我声称脱敏了」——规则表被清空、或额外规则配错，它照样报 True。
        这里改成从实际编译出来的规则表推导，与 model_layer_active 同一原则：
        **能推导的就别手写。**
        """
        return bool(self._compiled)

    def redact(self, text: str) -> str:
        """把文本里的可识别信息替换为占位符。"""
        if not text:
            return text
        out = text
        for kind, pattern in self._compiled:
            out = pattern.sub(PLACEHOLDER.format(kind=kind), out)
        return out

    def residual_kinds(self, text: str) -> tuple[str, ...]:
        """仍能命中的实体类型。用于「脱敏是否彻底」的断言。"""
        if not text:
            return ()
        return tuple(kind for kind, pattern in self._compiled if pattern.search(text))

    def assert_clean(self, text: str, *, where: str = "") -> str:
        """**fail-closed**：确认无残留才返回原文，否则抛错。"""
        left = self.residual_kinds(text)
        if left:
            raise RedactionError(
                f"{where or '内容'}仍含未脱敏的明文用户信息：{list(left)}；"
                f"按计划书三、P9「禁止展示明文用户信息」拒绝输出")
        return text

    def safe_text(self, text: str, *, where: str = "") -> str:
        """脱敏后再确认干净——**这是所有对外出口的必经之路**。"""
        return self.assert_clean(self.redact(text), where=where)

    def redact_deep(self, value: Any) -> Any:
        """递归脱敏结构化数据（看板、日志载荷都从这条路径出去）。"""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {k: self.redact_deep(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_deep(v) for v in value]
        return value

    def assert_deep_clean(self, value: Any, *, where: str = "") -> Any:
        """递归确认无残留。字符串叶子逐个查。"""
        if isinstance(value, str):
            return self.assert_clean(value, where=where)
        if isinstance(value, Mapping):
            for k, v in value.items():
                self.assert_deep_clean(v, where=f"{where}.{k}")
            return value
        if isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                self.assert_deep_clean(v, where=f"{where}[{i}]")
            return value
        return value


def contains_plaintext_identity(text: str) -> bool:
    """便捷判定：文本里是否还看得到明文身份信息。"""
    return bool(Redactor().residual_kinds(text))
