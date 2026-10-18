"""P9 领域 SFT 数据集构建（裁定 U29：LLaMA-Factory Alpaca 格式）。

计划书依据
----------
三、P9「领域 SFT 数据集构建；**剥离用户身份隐私**；
**禁止把私有 Vault 原始隐私放入训练集**；**公共 Workspace 知识库可用于训练**」

这是 P9 里**最容易出事的一步**：训练集一旦混入私有 Vault 内容，
隐私就从「运行时可访问」变成了「模型权重里固化」。因此这里做成**来源白名单**：
只有公共 Workspace 的来源能进，其余一律拒绝——不是过滤，是**拒绝**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_butler.evaluation.redaction import Redactor

#: 允许进训练集的来源（计划书：「公共 Workspace 知识库可用于训练」）
SOURCE_PUBLIC_WORKSPACE = "public_workspace"
#: 计划书点名**禁止**的来源
SOURCE_PRIVATE_VAULT = "private_vault"

ALLOWED_SOURCES: tuple[str, ...] = (SOURCE_PUBLIC_WORKSPACE,)
FORBIDDEN_SOURCES: tuple[str, ...] = (SOURCE_PRIVATE_VAULT,)

#: 离线链路（含本模块）严禁访问的 scope
OFFLINE_FORBIDDEN_SCOPES: tuple[str, ...] = ("vault",)


class DatasetError(ValueError):
    """数据集构建违规。**宁可构建失败，也不产出带隐私的训练集。**"""


@dataclass(frozen=True)
class SftSample:
    """一条指令监督微调样本（Alpaca 格式，裁定 U29）。"""

    instruction: str
    output: str
    input: str = ""
    system: str | None = None
    history: tuple[tuple[str, str], ...] = ()
    source: str = SOURCE_PUBLIC_WORKSPACE
    #: 必须为空：**身份已经在入口被剥离**，不是靠这里再脱敏
    user_id: str = ""

    def __post_init__(self) -> None:
        if not self.instruction.strip():
            raise DatasetError("instruction 不得为空（Alpaca 格式必填）")
        if not self.output.strip():
            raise DatasetError("output 不得为空（Alpaca 格式必填）")
        if self.source in FORBIDDEN_SOURCES:
            raise DatasetError(
                f"来源 {self.source!r} 被计划书三、P9 明令禁止进入训练集"
                f"（「禁止把私有 Vault 原始隐私放入训练集」）")
        if self.source not in ALLOWED_SOURCES:
            raise DatasetError(
                f"来源 {self.source!r} 不在白名单 {list(ALLOWED_SOURCES)} 内；"
                f"未列入即不允许，而不是默认允许")
        if self.user_id:
            raise DatasetError(
                "样本不得携带 user_id —— 计划书要求「剥离用户身份隐私」，"
                "身份应在入口处剥离，而不是留到这里再脱敏")

    def to_alpaca(self, redactor: Redactor) -> dict[str, Any]:
        row: dict[str, Any] = {"instruction": self.instruction, "input": self.input,
                               "output": self.output}
        if self.system:
            row["system"] = self.system
        if self.history:
            row["history"] = [[q, a] for q, a in self.history]
        safe = redactor.redact_deep(row)
        redactor.assert_deep_clean(safe, where="SFT 样本")
        return safe


@dataclass
class SftDataset:
    name: str
    samples: list[SftSample] = field(default_factory=list)

    def add(self, sample: SftSample) -> None:
        self.samples.append(sample)

    def to_alpaca(self, redactor: Redactor) -> list[dict[str, Any]]:
        return [s.to_alpaca(redactor) for s in self.samples]

    def sources(self) -> tuple[str, ...]:
        return tuple(sorted({s.source for s in self.samples}))

    def as_dict(self, redactor: Redactor) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "count": len(self.samples),
            "sources": list(self.sources()),
            "allowed_sources": list(ALLOWED_SOURCES),
            "forbidden_sources": list(FORBIDDEN_SOURCES),
            "carries_user_id": any(s.user_id for s in self.samples),
            "rows": self.to_alpaca(redactor),
        }
        redactor.assert_deep_clean(payload, where="SFT 数据集")
        return payload


def dataset_info_entry(*, name: str, file_name: str) -> dict[str, Any]:
    """生成 LLaMA-Factory ~{dataset_info.json}~ 里的一条数据集描述（裁定 U29）。"""
    return {name: {"file_name": file_name,
                   "columns": {"prompt": "instruction", "query": "input",
                               "response": "output", "system": "system",
                               "history": "history"}}}


def assert_offline_source(*, scope: str) -> None:
    """离线链路（增量微调数据集构建）**严禁访问**私有 Vault。

    计划书三、P10「离线链路严禁访问未脱敏用户私有 Vault」——
    这条约束在离线的入口处就要挡住，不能等到构建阶段。
    """
    if scope in OFFLINE_FORBIDDEN_SCOPES:
        raise DatasetError(
            f"离线链路严禁访问 scope={scope!r}（计划书三、P10 + 三、P9 训练集约束）")
