r"""P1–P4 端到端冒烟：真实 dev 环境（PostgreSQL + Redis）串起四个阶段的模块。

用法：.venv\Scripts\python.exe scripts\smoke_p1_p4.py
说明：
- 单元测试是分模块的；本脚本验证**跨模块串起来还能跑**（计划书七「验收标准」）。
- 嵌入优先用真实 bge-small-zh-v1.5；模型不在则回退到确定性替身并明确标注，
  这样在没有 requirements-ml.txt 的环境（例如 CI）也能跑。
- 所有写入使用可识别前缀并在结束前清理，不污染 dev 环境既有数据。
"""

from __future__ import annotations

# 临时目录自愈：系统临时目录不可写时改用工作区目录，
# 否则 chromadb 的 rust 绑定会报「unable to open database file」（见 MEMORY E-14）。
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(_ROOT))
from ai_butler.devkit.tempdir import ensure_writable_temp as _ensure_temp
_ensure_temp(_ROOT)


import asyncio
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AIBUTLER_ENV", str(ROOT / ".env.dev"))

from sqlalchemy import select  # noqa: E402

from ai_butler.agent_wiki.ontology import CLAIM, EVIDENCE, SOURCE, new_page  # noqa: E402
from ai_butler.agent_wiki.workspace import WikiStore  # noqa: E402
from ai_butler.agents.capabilities import CAP_RETRIEVAL, CAP_WIKI_READ, CAP_WIKI_WRITE, grant  # noqa: E402
from ai_butler.agents.facts import AgentFact, FactWriter  # noqa: E402
from ai_butler.agents.planner import PlannerAgent  # noqa: E402
from ai_butler.agents.qa import QualityAgent  # noqa: E402
from ai_butler.agents.registry import AgentRegistry  # noqa: E402
from ai_butler.core.config import load_settings  # noqa: E402
from ai_butler.core.db import Base, Database  # noqa: E402
from ai_butler.data_platform.confidence import EvidenceRef, score_confidence  # noqa: E402
from ai_butler.data_platform.governance import LOG_ONLY, PERSIST, REVIEW, DataGovernor  # noqa: E402
from ai_butler.data_platform.models import (  # noqa: E402
    CORE_TABLES,
    PLAN_DOMAIN_ASSIGNMENT,
    SOURCE_ACTIVE,
    KnowledgeGraphRecord,
    MaterialSourceRecord,
)
from ai_butler.data_platform.sdk import ALLOWED_METHODS, DataSDK  # noqa: E402
from ai_butler.material_center.chunking import ChunkingRule  # noqa: E402
from ai_butler.material_center.cleaner import CATEGORIES, RawDocument, clean_document  # noqa: E402
from ai_butler.material_center.embedding import HashingEmbedder  # noqa: E402
from ai_butler.material_center.materials import MaterialIngestor  # noqa: E402
from ai_butler.material_center.retrieval import BM25Store, HeuristicReranker, HybridRetriever  # noqa: E402
from ai_butler.material_center.service import SearchService  # noqa: E402
from ai_butler.material_center.vector_store import ChromaVectorStore  # noqa: E402
from ai_butler.scheduler.graph import DispatchPlan, SceneScheduler  # noqa: E402
from ai_butler.scheduler.observe import ListObserver  # noqa: E402
from ai_butler.scheduler.router import HardRule, HardRuleLayer, IntentRouter, JevLayer  # noqa: E402

USER = "smoke-p1p4-user"
DOC_PREFIX = "smoke-p1p4"
LABELS = ("book_hotel", "ask_weather")

FAILURES: list[str] = []
#: 上一次运行遗留的临时目录。Windows 上 chromadb 会占着 chroma.sqlite3，
#: 收尾的 rmtree(ignore_errors=True) 删不掉 —— 若不处理会一路堆下去
#: （实测 .test-work 两天堆了 823 MB，.smoke-work 也有同类残渣）。
_warm_sweep = ROOT / ".smoke-work"
if _warm_sweep.exists():
    import shutil as _sh

    for _child in _warm_sweep.iterdir():
        if _child.is_dir():
            _sh.rmtree(_child, ignore_errors=True)

WORK = ROOT / ".smoke-work" / uuid.uuid4().hex


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


def ok(msg: str) -> None:
    print(f"    OK  {msg}")


def fail(name: str, exc: Exception) -> None:
    FAILURES.append(f"{name}: {type(exc).__name__}: {exc}")
    print(f"    !!  {name} 失败：{type(exc).__name__}: {exc}")


def make_embedder():
    model = ROOT / "models" / "bge-small-zh-v1.5"
    if model.is_dir():
        try:
            from ai_butler.material_center.embedding import BGEEmbedder
            emb = BGEEmbedder(model)
            return emb, f"真实模型 {emb.name}（{emb.dim} 维）"
        except Exception as exc:                      # noqa: BLE001
            return HashingEmbedder(dim=256), f"回退替身（真实模型加载失败：{type(exc).__name__}）"
    return HashingEmbedder(dim=256), "回退替身（models/ 下无 bge-small-zh-v1.5）"


async def main() -> int:
    s = load_settings()
    WORK.mkdir(parents=True, exist_ok=True)
    db = Database(s)
    wiki = WikiStore(WORK / "wiki", settings=s)
    embedder, embedder_note = make_embedder()
    print(f"环境 {s.environment}  |  嵌入：{embedder_note}")

    # ============================ P1 ============================ #
    step(1, "P1 四大核心数据表与域归属")
    try:
        async with db.write_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        assert set(CORE_TABLES) == {"用户画像库", "知识图谱库", "素材溯源库", "设备状态库"}
        assert PLAN_DOMAIN_ASSIGNMENT["素材溯源库"] == "public"
        ok(f"四表就绪 {'/'.join(CORE_TABLES)}；素材溯源库=公共域（无 user_id）")
    except Exception as exc:                          # noqa: BLE001
        fail("建表", exc)

    step(2, "P1 双域隔离：绑定账号见「公共+本人」，未绑定只见公共")
    try:
        async with db.session_scope(user_id=None) as ses:
            ses.add(KnowledgeGraphRecord(user_id=None, subject=f"{DOC_PREFIX}-西湖",
                                         predicate="位于", object="杭州",
                                         statement="西湖位于杭州", confidence=0.95))
        async with db.session_scope(user_id=USER) as ses:
            ses.add(KnowledgeGraphRecord(user_id=USER, subject=f"{DOC_PREFIX}-我的偏好",
                                         predicate="是", object="高铁",
                                         statement="本人偏好高铁", confidence=0.9))
        async with db.session_scope(user_id=USER, readonly=True) as ses:
            bound = (await ses.execute(select(KnowledgeGraphRecord).where(
                KnowledgeGraphRecord.subject.like(f"{DOC_PREFIX}%")))).scalars().all()
        async with db.session_scope(user_id=None, readonly=True) as ses:
            unbound = (await ses.execute(select(KnowledgeGraphRecord).where(
                KnowledgeGraphRecord.subject.like(f"{DOC_PREFIX}%")))).scalars().all()
        assert len(bound) == 2, f"绑定账号应见 2 条，实际 {len(bound)}"
        assert len(unbound) == 1, f"未绑定应只见公共 1 条，实际 {len(unbound)}"
        ok(f"绑定账号可见 {len(bound)} 条；未绑定可见 {len(unbound)} 条（只含公共域）")
    except Exception as exc:                          # noqa: BLE001
        fail("双域隔离", exc)

    step(3, "P1 置信度打分 + 三条治理规则")
    try:
        c = score_confidence(base=0.5, evidences=[EvidenceRef("ev-1", weight=3.0,
                                                              reliability=0.95)])
        gov = DataGovernor(settings=s)
        high = gov.decide(confidence=c.confidence)
        low = gov.decide(confidence=0.2)
        conflict = gov.decide(confidence=0.99, conflict_status="pending")
        assert high.action == PERSIST and low.action == LOG_ONLY and conflict.action == REVIEW
        ok(f"融合置信 {c.confidence:.3f}（base 0.5 + 证据 0.95）→ {high.action}；"
           f"低分 → {low.action}；冲突 → {conflict.action}（阈值 {gov.threshold}）")
    except Exception as exc:                          # noqa: BLE001
        fail("置信度与治理", exc)

    step(4, "P1 统一数据 SDK（只读通道 + 双域）")
    try:
        sdk = DataSDK(database=db, wiki_store=wiki)
        rows = await sdk.read_knowledge(user_id=USER, limit=50)
        public_only = await sdk.read_knowledge(limit=50)
        assert all(r["user_id"] in (None, USER) for r in rows)
        assert all(r["user_id"] is None for r in public_only)
        try:
            await sdk.read_profile(user_id="")
            raise AssertionError("空 user_id 竟被接受")
        except Exception as exc:                      # noqa: BLE001
            assert "user_id" in str(exc)
        ok(f"SDK 公开面 {len(ALLOWED_METHODS)} 项；"
           f"绑定读到 {len(rows)} 条，未绑定读到 {len(public_only)} 条，空 user_id 被拒")
    except Exception as exc:                          # noqa: BLE001
        fail("数据 SDK", exc)

    # ============================ P2 ============================ #
    step(5, "P2 采集 → 清洗 → 分类 → 时效打标")
    try:
        raw = RawDocument(doc_id=f"{DOC_PREFIX}-doc", url=f"https://smoke.example.com/{DOC_PREFIX}",
                          title="西湖景区公告",
                          text="<html><body><h1>西湖景区公告</h1>"
                               "<p>西湖景区即日起调整开放时间，春季 6:30 开放。</p>"
                               "<p>门票可在线预约。</p>版权所有 某某公司</body></html>",
                          publisher="smoke.example.com")
        cleaned = clean_document(raw, now=datetime(2026, 10, 16, tzinfo=timezone.utc))
        assert "<" not in cleaned.text and "版权所有" not in cleaned.text
        assert cleaned.category in CATEGORIES
        ok(f"清洗后 {len(cleaned.text)} 字（剔样板行 {cleaned.removed_lines}）；"
           f"分类={cleaned.category}；时效至 {cleaned.stale_after[:10]}")
    except Exception as exc:                          # noqa: BLE001
        fail("清洗链路", exc)

    step(6, "P2 入库：素材溯源库 + Wiki Source 页面 + 建索引")
    service = SearchService(
        retriever=HybridRetriever(
            vector_store=ChromaVectorStore(WORK / "chroma", collection="smoke_materials"),
            bm25_store=BM25Store(WORK / "bm25"), embedder=embedder,
            reranker=HeuristicReranker()),
        chunking=ChunkingRule(max_chars=200, overlap_chars=0, min_chars=1))
    ingestor = MaterialIngestor(database=db, wiki_store=wiki, search_service=service)
    try:
        report = await ingestor.ingest([raw], now=datetime(2026, 10, 16, tzinfo=timezone.utc))
        assert report.created, f"未入库：{report.failures}"
        async with db.session_scope(user_id=None, readonly=True) as ses:
            row = (await ses.execute(select(MaterialSourceRecord).where(
                MaterialSourceRecord.title == "西湖景区公告"))).scalars().first()
        assert row is not None and row.status == SOURCE_ACTIVE
        page = wiki.read_page(report.wiki_pages[0])
        assert page is not None and page.type == SOURCE and page.user_id is None
        ok(f"create={report.created} 切片={report.indexed_chunks}；"
           f"溯源库 category={row.category}；Wiki 源页面 {page.id}（user_id={page.user_id}）")
    except Exception as exc:                          # noqa: BLE001
        fail("素材入库", exc)

    step(7, "P2 三重混合检索（BM25 + 向量 + Rerank）")
    try:
        resp = service.search("西湖景区开放时间", user_id=USER)
        assert resp.hits, "检索无结果"
        assert resp.searched_scopes == ["public", f"vault:{USER}"]
        assert resp.reranked is True
        ok(f"命中 {resp.count} 条；作用域={resp.searched_scopes}；"
           f"Top1 doc={resp.hits[0].doc_id} rerank={resp.hits[0].rerank_score:.3f}")
    except Exception as exc:                          # noqa: BLE001
        fail("混合检索", exc)

    # ============================ P3 ============================ #
    step(8, "P3 五层路由 + JEV-003 多意图必须澄清")
    try:
        router = IntentRouter([HardRuleLayer([HardRule(r"订.*酒店", "book_hotel")])])
        from ai_butler.infra.llm_gateway import JevDecision

        class _ClarifyJev:
            async def decide(self, *, user_id, question, allowed_labels, model=None):
                return JevDecision("book_hotel", 0.6, "jev", require_clarification=True,
                                   clarification_category="multi_intent",
                                   candidate_labels=("book_hotel", "ask_weather"))

        consulted: list[str] = []

        class _Spy:
            name = "vector"

            async def evaluate(self, request):
                consulted.append("vector")
                from ai_butler.scheduler.router import LayerVerdict
                return LayerVerdict(accepted=True, label="ask_weather", confidence=0.99)

        from ai_butler.scheduler.router import RouteRequest
        clarify_router = IntentRouter([JevLayer(_ClarifyJev()), _Spy()])
        decision = await clarify_router.route(RouteRequest(
            user_id=USER, session_id="smoke", utterance="订酒店顺便看看天气",
            labels=LABELS))
        assert decision.must_pause and not consulted, "澄清结论被更低层覆盖了"
        assert decision.clarifying_question.endswith("？")
        hit = await router.route(RouteRequest(user_id=USER, session_id="smoke",
                                              utterance="帮我订个酒店", labels=LABELS))
        ok(f"硬规则命中 layer={hit.layer} label={hit.label}；"
           f"JEV-003 澄清暂停分发（更低层未被调用），话术={decision.clarifying_question}")
    except Exception as exc:                          # noqa: BLE001
        fail("五层路由", exc)

    step(9, "P3 LangGraph 主状态机：路由 → 分发 → 聚合")
    try:
        from ai_butler.agents.capabilities import CAP_DATA_READ
        from ai_butler.data_platform.models import DeviceStateRecord
        from ai_butler.agents.mops import (
            DevicePerceptionAgent,
            IncrementalExtractionAgent,
            WeatherPerceptionAgent,
        )

        registry = AgentRegistry()
        planner = PlannerAgent(tools=grant(PlannerAgent, {
            CAP_RETRIEVAL: service,
            CAP_WIKI_WRITE: FactWriter(wiki)}))
        qa = QualityAgent(tools=grant(QualityAgent, {CAP_WIKI_READ: wiki}), settings=s)

        # P4 集群成员：设备感知 → 天气感知 → 增量抽取
        device = DevicePerceptionAgent(tools=grant(DevicePerceptionAgent,
                                                   {CAP_DATA_READ: sdk}))
        weather = WeatherPerceptionAgent(tools=grant(WeatherPerceptionAgent,
                                                     {CAP_RETRIEVAL: service}))
        extract = IncrementalExtractionAgent(tools=grant(
            IncrementalExtractionAgent,
            {CAP_RETRIEVAL: service, CAP_WIKI_WRITE: FactWriter(wiki)}))
        for agent in (planner, qa, device, weather, extract):
            registry.register(agent)

        # 给设备感知 Agent 备一条真实设备数据（否则它只能如实汇报 0 台）
        async with db.session_scope(user_id=USER) as ses:
            ses.add(DeviceStateRecord(user_id=USER, device_id="smoke-dev", metric="battery",
                                      value="80", confidence=0.99))
            ses.add(DeviceStateRecord(user_id=USER, device_id="smoke-dev", metric="network",
                                      value="wifi", confidence=0.99))

        # 先给规划 Agent 备好一条可绑定的证据（否则「无证据不产事实」会正确跳过）
        wiki.write_page(new_page(EVIDENCE, "ev-smoke", confidence=0.9, user_id=USER,
                                 source_id=f"{DOC_PREFIX}-src", source_text="来源片段",
                                 text_unit_id="tu-smoke"), user_id=USER)
        service.index_text(doc_id=f"{DOC_PREFIX}-evidenced",
                           text="西湖景区春季六点半开放，门票可在线预约。",
                           confidence=0.93, evidence_ids=["ev-smoke"],
                           page_id="src-smoke")

        observer = ListObserver()
        chain = ("device_perception_agent", "weather_perception_agent",
                 "incremental_extraction_agent", "planner_agent", "qa_agent")
        scheduler = SceneScheduler(
            router=IntentRouter([HardRuleLayer([HardRule(r"订.*酒店", "book_hotel")])]),
            agents={a.name: a for a in (planner, qa, device, weather, extract)},
            plan=DispatchPlan(chains={"book_hotel": chain}),
            observer=observer)
        # utterance 必须同时满足：命中硬规则（订…酒店）+ 含可检索的素材关键词
        result = await scheduler.run(user_id=USER, session_id="smoke",
                                     utterance="订酒店前先查：西湖景区春季六点开放吗",
                                     labels=LABELS)
        assert result.resolved, "未解析出意图"
        assert result.dispatched == list(chain), (
            f"调用链不完整：dispatched={result.dispatched} failed={result.failed}\n"
            f"降级明细：\n  " + "\n  ".join(result.degradation))
        nodes = [sp["node"] for sp in result.spans]
        ok(f"意图={result.intent} 链路={result.dispatched} 回滚={result.rollback_count}；"
           f"埋点节点={nodes}")
        print(f"        质检：校验 {result.agent_results['qa_agent']['qa_report']['claims_checked']} "
              f"条声明，发现 {len(result.agent_results['qa_agent']['qa_report']['findings'])} 项问题")
        dev = result.agent_results["device_perception_agent"]["device_snapshot"]
        inc = result.agent_results["incremental_extraction_agent"]["extracted_facts"]
        print(f"        P4 集群：设备 {dev['device_count']} 台 / 指标 {len(dev['metrics'])} 项；"
              f"增量抽取产出 {len(inc['facts'])} 条绑定证据的事实，"
              f"跳过 {len(inc['skipped'])} 条")
    except Exception as exc:                          # noqa: BLE001
        fail("主状态机", exc)

    # ============================ P4 ============================ #
    step(10, "P4 Agent 权责台账与能力裁剪")
    try:
        ledger = registry.ledger()
        assert len(registry.names()) == 5, registry.names()
        forbidden = {"dispatch", "call", "invoke", "forward", "delegate", "route"}
        assert not ({n for n in dir(AgentRegistry) if not n.startswith("_")} & forbidden)
        tools = registry.tools_for("qa_agent", {CAP_WIKI_READ: wiki, CAP_RETRIEVAL: service})
        assert not tools.has(CAP_RETRIEVAL), "未声明的能力不应发放"
        ok(f"已注册 {registry.names()}；能力裁剪生效；注册表无 Agent 互调入口")
        for entry in ledger:
            print(f"        {entry['name']:14s} 能力={entry['capabilities']} "
                  f"写入={entry['writes']}")
    except Exception as exc:                          # noqa: BLE001
        fail("Agent 协同", exc)

    # ============================ 管家管辖 ============================ #
    step(11, "管家 Agent 统一管辖全部阶段")
    try:
        from ai_butler.agents.butler import ButlerAgent
        from ai_butler.core import phases as phases_mod
        from ai_butler.agents.phase import PhaseNotImplemented
        from ai_butler.agents.phases_bootstrap import build_butler
        from ai_butler.agents.state import new_state

        butler = build_butler(settings=s, data_sdk=sdk, search_service=service,
                              wiki_store=wiki)
        coverage = butler.coverage()
        assert coverage["phases_total"] == len(phases_mod.PHASES)
        assert coverage["phases_enrolled"] == coverage["phases_implemented"]
        assert coverage["phases_implemented"] >= 6, coverage

        # 台账必须把未落地阶段也列出来，且不得注册空壳
        planned = [row for row in butler.ledger() if not row["implemented"]]
        assert len(planned) == len(phases_mod.planned_phases())
        assert all(not row["enrolled"] for row in planned)

        # 调未落地阶段必须抛错，且报错带上计划书该阶段的核心能力。
        # 目标阶段**从真值源取**而不是写死——否则每有一个阶段落地，这里都要跟着改一次。
        pending = phases_mod.planned_phases()
        assert pending, "已无未落地阶段，本项检查需改为「全部阶段均可放行」的验证"
        target = pending[0]
        try:
            await butler.run_phase(target.code, new_state(user_id=USER, session_id="smoke"))
            raise AssertionError("未落地阶段竟被放行")
        except PhaseNotImplemented as exc:
            assert target.title in str(exc), "报错要指出是哪个阶段"
            assert target.core_capability in str(exc), "报错要带上计划书该阶段的核心能力"

        # 会话失效时管家先拦下
        blocked = butler.govern({"user_id": USER, "session_id": "smoke",
                                 "login_status": False})
        assert blocked.granted is False and blocked.code == ButlerAgent.LOGIN_REQUIRED

        ok(f"管家管辖 {coverage['phases_total']} 个阶段："
           f"已落地 {coverage['implemented']}，未落地显式标注 {coverage['planned']}")
        for row in butler.ledger():
            mark = "已落地" if row["implemented"] else "未落地"
            print(f"        {row['code']:5s} {mark}  enrolled={str(row['enrolled']):5s} "
                  f"{row['display']}")
    except Exception as exc:                          # noqa: BLE001
        fail("管家管辖", exc)

    # ============================ 清理 ============================ #
    step(12, "清理冒烟数据")
    try:
        async with db.session_scope(user_id=None) as ses:
            for row in (await ses.execute(select(KnowledgeGraphRecord).where(
                    KnowledgeGraphRecord.subject.like(f"{DOC_PREFIX}%")))).scalars().all():
                await ses.delete(row)
            for row in (await ses.execute(select(MaterialSourceRecord).where(
                    MaterialSourceRecord.title == "西湖景区公告"))).scalars().all():
                await ses.delete(row)
        async with db.session_scope(user_id=USER) as ses:
            for row in (await ses.execute(select(KnowledgeGraphRecord).where(
                    KnowledgeGraphRecord.subject.like(f"{DOC_PREFIX}%")))).scalars().all():
                await ses.delete(row)
            for row in (await ses.execute(select(DeviceStateRecord).where(
                    DeviceStateRecord.device_id == "smoke-dev"))).scalars().all():
                await ses.delete(row)
        ok("冒烟数据已清理")
    except Exception as exc:                          # noqa: BLE001
        fail("清理", exc)

    await db.dispose()
    shutil.rmtree(WORK, ignore_errors=True)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"P1–P4 端到端冒烟：{len(FAILURES)} 项失败")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("P1–P4 端到端冒烟：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
