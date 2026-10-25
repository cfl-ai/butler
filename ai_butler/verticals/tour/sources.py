"""统一采集底座对接（裁定 U37）。

计划书依据
----------
- 五、第二阶段表 11「对接统一底层采集底座：天气 API、景区公开信息、交通实时状态；
  **统一清洗、来源标记、去重**；**输出转为 Agent-Wiki 公共 Workspace 的 source 源页面**」
- 表 11 步骤2「采集底座返回**带来源标记**的结构化数据；校验数据有效性；
  将原始采集结果生成 Agent-Wiki source 源页面，**生成 claim 声明绑定 evidence 证据**」
- 「**公私域隔离**：用户出行偏好属于私有数据，存入 user_id 绑定的私有 Vault，
  **公共采集底座不可读取隐私**」

裁定 U37
--------
天气用和风天气，景区/POI 用 OSM Overpass API。**本模块不内置任何真实网络调用** ——
对外数据源经**端口注入**，这样 CI 不依赖外部服务，也不会因第三方限流而红。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from ai_butler.agent_wiki.ontology import EVIDENCE, SOURCE, new_page

#: 采集类别（表 11 逐条）
SOURCE_WEATHER = "weather"
SOURCE_SCENIC = "scenic"
SOURCE_TRAFFIC = "traffic"
SOURCE_KINDS: tuple[str, ...] = (SOURCE_WEATHER, SOURCE_SCENIC, SOURCE_TRAFFIC)

#: 数据源（裁定 U37）
PROVIDER_QWEATHER = "qweather"
PROVIDER_OVERPASS = "overpass"


class CollectError(RuntimeError):
    """采集失败。**注意：这类失败禁止回退**（表 12）。"""


@dataclass(frozen=True)
class CollectedFact:
    """一条采集结果。**公共数据**，不含任何用户私有信息。"""

    kind: str
    subject: str
    payload: Mapping[str, Any]
    provider: str
    evidence_id: str
    low_confidence: bool = False

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise CollectError(f"未知采集类别 {self.kind!r}，可选 {list(SOURCE_KINDS)}")
        if not self.subject or not self.evidence_id:
            raise CollectError("采集结果必须有 subject 与 evidence_id（表 11 要求绑定证据）")
        for key in self.payload:
            if key in ("user_id", "preferences", "taboos"):
                raise CollectError(
                    f"公共采集结果不得包含用户私有字段 {key!r}"
                    f"（表 11：公共采集底座不可读取隐私）")


class CollectorPort(Protocol):
    """采集端口。真机实现走和风天气 / Overpass；测试注入假实现。"""

    def fetch(self, kind: str, *, subject: str) -> CollectedFact | None: ...


@dataclass
class CollectResult:
    facts: list[CollectedFact] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def collect_all(collector: CollectorPort, *, subject: str,
                kinds: Sequence[str] = SOURCE_KINDS) -> CollectResult:
    """拉取全部所需数据。**任一类失败都记入 failures**，由调用方按表 12 处置（重试）。"""
    result = CollectResult()
    for kind in kinds:
        try:
            fact = collector.fetch(kind, subject=subject)
        except Exception as error:                      # 采集端异常 = 采集失败
            result.failures.append(f"{kind}: {error}")
            continue
        if fact is None:
            result.failures.append(f"{kind}: 数据源无返回")
            continue
        result.facts.append(fact)
    return result


async def collect_all_async(collector: CollectorPort, *, subject: str,
                            kinds: Sequence[str] = SOURCE_KINDS) -> CollectResult:
    """`collect_all` 的异步姊妹（U96）。

    真实网络端口（httpx）天生异步，而测试替身是同步的——**两边都要能用**，
    所以这里不挑食：`fetch` 返回 awaitable 就 await，不是就直接用。
    失败纪律与 `collect_all` 逐字相同：记 failures，不静默跳过。
    """
    result = CollectResult()
    for kind in kinds:
        try:
            fact = collector.fetch(kind, subject=subject)
            if inspect.isawaitable(fact):
                fact = await fact
        except Exception as error:                      # 采集端异常 = 采集失败
            result.failures.append(f"{kind}: {error}")
            continue
        if fact is None:
            result.failures.append(f"{kind}: 数据源无返回")
            continue
        result.facts.append(fact)
    return result


def source_page_id(fact: CollectedFact) -> str:
    return f"source-{fact.kind}-{fact.evidence_id}"


def to_source_page(fact: CollectedFact, *, now: str = "") -> Any:
    """把采集结果转成**公共 Workspace** 的 source 源页面（无 user_id）。"""
    # 不传 status：本体对每种页面类型有既定的默认状态，
    # 自造一个 "collected" 会被本体校验直接拒掉（本阶段实际踩过）。
    return new_page(
        SOURCE, source_page_id(fact), title=f"{fact.subject}｜{fact.kind}",
        now=now, url=f"{fact.provider}://{fact.kind}/{fact.evidence_id}",
        provider=fact.provider, subject=fact.subject, payload=dict(fact.payload),
        low_confidence=fact.low_confidence)


def to_claim_fact(fact: CollectedFact, *, subject_id: str, claim_page_id: str) -> dict[str, Any]:
    """生成绑定证据的 Claim 声明（表 11 要求 claim 绑定 evidence）。"""
    if not fact.evidence_id:
        raise CollectError("Claim 必须绑定 evidence")
    return {"claim_page_id": claim_page_id, "evidence_ids": [fact.evidence_id],
            "statement": f"{fact.subject} 的 {fact.kind} 采集结果",
            "subject_id": subject_id, "object_id": fact.evidence_id,
            "confidence": 0.5 if fact.low_confidence else 0.9}
