"""P4 Mops 多智能体集群 —— 感知与基础执行单元。

计划书依据
----------
- 三、P4「Mops 多智能体集群｜感知与基础执行单元」
- 三、P4「包含子 Agent：**设备感知、天气感知、时间进度、增量抽取、路线方案、
  备选预案、代码迭代 Agent**」
- 三、P4「逐个开发、逐个单测、逐个上线；所有子 Agent **只读全局状态、仅写入自有字段、
  无跨模块越权调用**」
- 三、P4 表注「隐私约束：读写私有数据必须从 LangGraph State 获取 user_id，不允许硬编码、
  不能为空；禁止读取其他 user_id 数据。**Wiki 约束：子 Agent 产出事实优先写入对应用户私有
  Vault；输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID**」
- 三、P4 触发时机「全局状态变更自动触发对应子任务执行；会话失效则跳过执行」

分工口径
--------
| 类别 | Agent | 产出 |
|---|---|---|
| 感知 | 设备感知 / 天气感知 / 时间进度 | 本账号的**观测快照**（自有字段） |
| 抽取 | 增量抽取 | 把快照转成**绑定证据的事实**并写入本人私有 Vault |
| 执行 | 路线方案 / 备选预案 | 依据检索到的权威素材产出**带证据的方案事实** |
| 迭代 | 代码迭代 | 产出**代码迭代提案记录**（**不执行任何代码**，执行属 P10 沙箱） |

「产出事实」的三个 Agent 走 FactWriter：绑定 Claim 声明 ID 与 Evidence 证据 ID，
只写本人 Vault。感知类只产出观测快照，不硬凑成事实（避免造出无证据的「声明」）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Sequence

from ai_butler.agents.base import SubAgent
from ai_butler.agents.capabilities import (
    CAP_DATA_READ,
    CAP_RETRIEVAL,
    CAP_WIKI_WRITE,
)
from ai_butler.agents.facts import AgentFact, FactWriter

#: 集群名册：(Agent 名, 计划书里的中文名, 自有写入字段)。逐条对应计划书三、P4 的 7 个名字。
ROSTER: tuple[tuple[str, str, str], ...] = (
    ("device_perception_agent", "设备感知 Agent", "device_snapshot"),
    ("weather_perception_agent", "天气感知 Agent", "weather_snapshot"),
    ("time_progress_agent", "时间进度 Agent", "time_progress"),
    ("incremental_extraction_agent", "增量抽取 Agent", "extracted_facts"),
    ("route_plan_agent", "路线方案 Agent", "route_options"),
    ("contingency_plan_agent", "备选预案 Agent", "contingency_plans"),
    ("code_iteration_agent", "代码迭代 Agent", "code_iteration_proposals"),
)

#: 单次最多产出多少条事实（实现参数）
DEFAULT_MAX_FACTS = 5
#: 快照/事实里保留的文本长度上限（实现参数）
MAX_STATEMENT_CHARS = 200


class MopsError(RuntimeError):
    pass


def claim_id_for(*, user_id: str, agent: str, statement: str) -> str:
    digest = hashlib.sha256(f"{user_id}|{agent}|{statement}".encode("utf-8")).hexdigest()
    return f"claim-{agent.split('_')[0]}-{digest[:16]}"


@dataclass
class _MopsAgent(SubAgent):
    """集群内子 Agent 的公共骨架：工具面由装配层按声明裁剪。"""

    tools: Any = None
    settings: Any = None
    max_facts: int = DEFAULT_MAX_FACTS

    # ---------------- 共用：检索命中 → 绑定证据的事实 ---------------- #
    def facts_from_hits(self, hits: Sequence[Any], *, user_id: str) -> tuple[list[AgentFact],
                                                                            list[str]]:
        """把检索命中转成事实。**缺证据或缺置信度的一律跳过并记录原因**，
        不为了凑数造出无证据的声明。"""
        facts: list[AgentFact] = []
        skipped: list[str] = []
        for hit in hits:
            if len(facts) >= self.max_facts:
                break
            if not getattr(hit, "evidence_ids", None):
                skipped.append(f"{hit.chunk_id}: 缺证据 ID")
                continue
            if getattr(hit, "confidence", None) is None:
                skipped.append(f"{hit.chunk_id}: 缺置信度")
                continue
            statement = str(hit.text).strip()[:MAX_STATEMENT_CHARS]
            facts.append(AgentFact(
                statement=statement,
                claim_page_id=claim_id_for(user_id=user_id, agent=self.name,
                                           statement=statement),
                evidence_ids=tuple(hit.evidence_ids),
                confidence=float(hit.confidence),
                subject_id=getattr(hit, "source_id", "") or getattr(hit, "doc_id", ""),
                object_id=hit.chunk_id,
            ))
        return facts, skipped

    def write_facts(self, facts: Sequence[AgentFact], *, user_id: str,
                    untrusted: bool = False) -> list[dict[str, Any]]:
        """计划书三、P4 Wiki 约束：产出事实优先写入**本人私有 Vault**。"""
        if not facts:
            return []
        writer: FactWriter = self.tools.require(CAP_WIKI_WRITE)
        # 风控（台账 U41）：不可信输入产出的事实只能落成 SUSPECTED
        pages = writer.write_many(list(facts), user_id=user_id, untrusted=untrusted)
        return [{"claim_page_id": p.id, "confidence": p.confidence,
                 "evidence_ids": list(p.extra.get("evidence_ids") or [])} for p in pages]


# --------------------------------------------------------------------------- #
# 1. 设备感知
# --------------------------------------------------------------------------- #
@dataclass
class DevicePerceptionAgent(_MopsAgent):
    name: ClassVar[str] = "device_perception_agent"
    writes: ClassVar[tuple[str, ...]] = ("device_snapshot",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_DATA_READ,)
    responsibilities: ClassVar[str] = "读取本账号设备状态库，产出设备观测快照"

    async def run(self, state: dict[str, Any]) -> dict[str, Any]:
        # 必须异步：asyncpg 连接与事件循环绑定（见 agents/runtime.py 的说明）
        uid = self.require_user_id(state)
        sdk = self.tools.require(CAP_DATA_READ)
        rows = await sdk.read_device_state(user_id=uid, limit=50)
        metrics: dict[str, str] = {}
        devices: set[str] = set()
        for row in rows:
            devices.add(row.get("device_id", ""))
            metrics[row.get("metric", "")] = row.get("value", "")
        return {"device_snapshot": {
            "user_id": uid, "agent": self.name,
            "device_count": len(devices), "metrics": metrics, "sample_size": len(rows),
        }}


# --------------------------------------------------------------------------- #
# 2. 天气感知
# --------------------------------------------------------------------------- #
@dataclass
class WeatherPerceptionAgent(_MopsAgent):
    name: ClassVar[str] = "weather_perception_agent"
    writes: ClassVar[tuple[str, ...]] = ("weather_snapshot",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_RETRIEVAL,)
    responsibilities: ClassVar[str] = "经统一检索 API 取天气权威素材，产出天气观测快照"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        service = self.tools.require(CAP_RETRIEVAL)
        query = str(state.get("weather_query") or state.get("utterance") or "天气").strip()
        response = service.search(query, user_id=uid, top_k=self.max_facts * 2)
        return {"weather_snapshot": {
            "user_id": uid, "agent": self.name, "query": query,
            "hits": response.count,
            "evidence_ids": [e for h in response.hits for e in h.evidence_ids],
            "searched_scopes": list(response.searched_scopes),
        }}


# --------------------------------------------------------------------------- #
# 3. 时间进度
# --------------------------------------------------------------------------- #
@dataclass
class TimeProgressAgent(_MopsAgent):
    name: ClassVar[str] = "time_progress_agent"
    writes: ClassVar[tuple[str, ...]] = ("time_progress",)
    capabilities: ClassVar[tuple[str, ...]] = ()
    responsibilities: ClassVar[str] = "依据 State 里的时间上下文计算行程时间进度"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        now = float(state.get("now_epoch") or time.time())
        started = state.get("task_started_epoch")
        deadline = state.get("deadline_epoch")

        elapsed = (now - float(started)) if started is not None else None
        remaining = (float(deadline) - now) if deadline is not None else None
        total = (float(deadline) - float(started)) if (started is not None
                                                       and deadline is not None) else None
        ratio = None
        if total and total > 0:
            ratio = max(0.0, min(1.0, (now - float(started)) / total))

        return {"time_progress": {
            "user_id": uid, "agent": self.name,
            "now": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "elapsed_seconds": elapsed,
            "remaining_seconds": remaining,
            "progress_ratio": ratio,
            "overdue": bool(remaining is not None and remaining < 0),
        }}


# --------------------------------------------------------------------------- #
# 4. 增量抽取（把感知快照转成绑定证据的事实）
# --------------------------------------------------------------------------- #
@dataclass
class IncrementalExtractionAgent(_MopsAgent):
    name: ClassVar[str] = "incremental_extraction_agent"
    writes: ClassVar[tuple[str, ...]] = ("extracted_facts",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_RETRIEVAL, CAP_WIKI_WRITE)
    responsibilities: ClassVar[str] = "把感知快照与检索命中抽取为绑定 Claim/Evidence 的事实"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        service = self.tools.require(CAP_RETRIEVAL)
        query = str(state.get("utterance") or "").strip()
        if not query:
            return {"extracted_facts": {"user_id": uid, "agent": self.name,
                                        "facts": [], "skipped": ["无检索词"]}}

        response = service.search(query, user_id=uid, top_k=self.max_facts * 2)
        facts, skipped = self.facts_from_hits(response.hits, user_id=uid)
        written = self.write_facts(facts, user_id=uid,
                                      untrusted=bool(state.get("input_untrusted")))
        return {"extracted_facts": {
            "user_id": uid, "agent": self.name,
            "facts": written, "skipped": skipped,
            "observed": sorted(k for k in ("device_snapshot", "weather_snapshot",
                                           "time_progress") if state.get(k)),
        }}


# --------------------------------------------------------------------------- #
# 5. 路线方案
# --------------------------------------------------------------------------- #
@dataclass
class RoutePlanAgent(_MopsAgent):
    name: ClassVar[str] = "route_plan_agent"
    writes: ClassVar[tuple[str, ...]] = ("route_options",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_RETRIEVAL, CAP_WIKI_WRITE)
    responsibilities: ClassVar[str] = "依据检索到的交通素材产出带证据的路线候选"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        service = self.tools.require(CAP_RETRIEVAL)
        query = str(state.get("route_query") or state.get("utterance")
                    or "交通路线").strip()
        response = service.search(query, user_id=uid, top_k=self.max_facts * 2)
        facts, skipped = self.facts_from_hits(response.hits, user_id=uid)
        written = self.write_facts(facts, user_id=uid,
                                      untrusted=bool(state.get("input_untrusted")))
        return {"route_options": {
            "user_id": uid, "agent": self.name, "query": query,
            "options": written, "skipped": skipped,
            "note": "只产出带证据的候选素材；行程编排属 P5 动态事件运营 Agent",
        }}


# --------------------------------------------------------------------------- #
# 6. 备选预案
# --------------------------------------------------------------------------- #
@dataclass
class ContingencyPlanAgent(_MopsAgent):
    name: ClassVar[str] = "contingency_plan_agent"
    writes: ClassVar[tuple[str, ...]] = ("contingency_plans",)
    capabilities: ClassVar[tuple[str, ...]] = (CAP_RETRIEVAL, CAP_WIKI_WRITE)
    responsibilities: ClassVar[str] = "主方案受阻时依据权威素材产出备选方案事实"

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        service = self.tools.require(CAP_RETRIEVAL)
        disruption = str(state.get("disruption") or "").strip()
        if not disruption:
            # 没有被打破的前提就不产出备选——不凭空造预案
            return {"contingency_plans": {"user_id": uid, "agent": self.name,
                                          "plans": [], "disruption": "",
                                          "note": "State 未给出受阻前提，不产出备选"}}
        query = f"{disruption} 备选".strip()
        response = service.search(query, user_id=uid, top_k=self.max_facts * 2)
        facts, skipped = self.facts_from_hits(response.hits, user_id=uid)
        written = self.write_facts(facts, user_id=uid,
                                      untrusted=bool(state.get("input_untrusted")))
        return {"contingency_plans": {
            "user_id": uid, "agent": self.name, "disruption": disruption,
            "plans": written, "skipped": skipped,
        }}


# --------------------------------------------------------------------------- #
# 7. 代码迭代（只出提案，不执行）
# --------------------------------------------------------------------------- #
@dataclass
class CodeIterationAgent(_MopsAgent):
    name: ClassVar[str] = "code_iteration_agent"
    writes: ClassVar[tuple[str, ...]] = ("code_iteration_proposals",)
    capabilities: ClassVar[tuple[str, ...]] = ()
    responsibilities: ClassVar[str] = "汇总质检与降级信号产出代码迭代提案（不执行代码）"

    #: 计划书三、P10 才允许在隔离沙箱里改代码与跑测试；本 Agent 严格止步于提案
    EXECUTES_CODE: ClassVar[bool] = False

    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        uid = self.require_user_id(state)
        signals: list[dict[str, Any]] = []

        report = state.get("qa_report") or {}
        for finding in (report.get("findings") or []):
            signals.append({"source": "qa_report", "kind": finding.get("kind", ""),
                            "ref": finding.get("page_id", "")})
        for note in (state.get("degradation") or []):
            signals.append({"source": "degradation", "kind": "degradation", "ref": str(note)[:120]})

        proposals = [{"proposal_id": hashlib.sha256(
            f"{uid}|{s['source']}|{s['kind']}|{s['ref']}".encode("utf-8")).hexdigest()[:16],
            **s} for s in signals][:self.max_facts]

        return {"code_iteration_proposals": {
            "user_id": uid, "agent": self.name,
            "proposals": proposals,
            "signal_count": len(signals),
            "executes_code": self.EXECUTES_CODE,
            "note": "仅产出提案；改代码/跑测试属 P10 Harness 离线沙箱，业务链路内不得执行",
        }}


#: 全部集群成员类（供装配与台账遍历）
MOPS_AGENT_CLASSES: tuple[type, ...] = (
    DevicePerceptionAgent, WeatherPerceptionAgent, TimeProgressAgent,
    IncrementalExtractionAgent, RoutePlanAgent, ContingencyPlanAgent,
    CodeIterationAgent,
)


def build_mops_cluster(*, settings: Any = None) -> list[SubAgent]:
    """实例化 7 个集群子 Agent。工具面由装配层按各自声明裁剪后注入。"""
    return [cls(settings=settings) for cls in MOPS_AGENT_CLASSES]


def roster_manifest() -> list[dict[str, Any]]:
    """名册台账：名字逐条对应计划书三、P4 的 7 个中文名。"""
    rows = []
    for (name, plan_name, write_field), cls in zip(ROSTER, MOPS_AGENT_CLASSES):
        rows.append({"agent": name, "plan_name": plan_name, "writes": write_field,
                     "capabilities": list(cls.capabilities),
                     "responsibilities": cls.responsibilities})
    return rows
