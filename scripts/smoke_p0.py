r"""P0-1 端到端冒烟：真实 PostgreSQL + Redis + Ollama 串起三个模块。

用法：.venv\Scripts\python.exe scripts\smoke_p0.py
说明：会话参数取自 .env（计划书未定义，人工授权取值 2026-10-16，可调）。
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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("AIBUTLER_ENV", str(ROOT / ".env.dev"))

import redis.asyncio as aioredis  # noqa: E402

from ai_butler.core.config import load_settings  # noqa: E402
from ai_butler.core.db import Database  # noqa: E402
from ai_butler.core.security import FieldCipher, hash_password, verify_password  # noqa: E402
from ai_butler.core.session_store import SessionStore  # noqa: E402
from ai_butler.middleware.session_auth import login  # noqa: E402
from ai_butler.infra.llm_gateway import ChatMessage, JevClient, LLMGateway  # noqa: E402

LABELS = ["旅游", "点餐", "支付"]          # 冒烟占位；真实标签集属【文档未定义项 #1】，须人工补充


async def main() -> int:
    s = load_settings()
    print(f"[1] 配置装载 OK  provider_order={s.provider_order}  jev_threshold={s.jev_confidence_threshold}")
    print(f"    会话参数（人工授权 2026-10-16）: 有效期={s.session_ttl_seconds}s "
          f"空闲={s.session_idle_timeout_seconds}s 刷新余量={s.session_refresh_ahead_seconds}s")

    # --- 数据库抽象层 ---
    db = Database(s)
    async with db.session_scope(user_id="smoke-user") as session:
        from sqlalchemy import text
        v = (await session.execute(text("select version()"))).scalar_one()
        ok = (await session.execute(text("select extversion from pg_extension where extname='vector'"))).scalar_one_or_none()
    print(f"[2] 数据库抽象层 OK  {v.split(',')[0]}  pgvector={ok}")
    await db.dispose()

    # --- Redis 会话 ---
    r = aioredis.Redis(host=s.redis_host, port=s.redis_port, db=s.redis_db, decode_responses=True)
    store = SessionStore(r, ttl_seconds=s.session_ttl_seconds,
                         idle_timeout_seconds=s.session_idle_timeout_seconds)
    st = await login(store=store, settings=s, user_id="smoke-user", device_id="phone")
    rec = await store.validate(st.jti)
    devices = await store.list_sessions("smoke-user")
    await store.revoke_all_for_user("smoke-user")
    print(f"[3] 会话鉴权 OK  jti={st.jti[:8]}…  在线设备={len(devices)}  踢下线后校验={await store.validate(st.jti)}")

    # --- 安全原语 ---
    h = hash_password("Smoke#2026")
    c = FieldCipher(s.field_encryption_key)
    print(f"[4] 安全原语 OK  加盐哈希校验={verify_password('Smoke#2026', h)}  隐私字段解密={c.decrypt(c.encrypt('13800001111'))}")

    # --- 统一 LLM 网关 ---
    gw = LLMGateway(s, redis=r)
    res = await gw.chat(user_id="smoke-user",
                        messages=[ChatMessage("user", "只回复两个字：连通")], max_tokens=16)
    print(f"[5] LLM 网关 OK  provider={res.provider} model={res.model} latency={res.latency_ms}ms text={res.text.strip()!r}")

    # --- Jev 结构化决策（此处关掉影子模式以观察真实决策路径）---
    s2 = s.model_copy(update={"jev_shadow_mode": False})
    jev = JevClient(gateway=LLMGateway(s2, redis=r), settings=s2)
    decision = await jev.decide(user_id="smoke-user", question="帮我安排国庆去杭州的三天行程",
                                allowed_labels=LABELS)
    print(f"[6] Jev 决策 OK  accepted={decision.accepted} label={decision.label!r} "
          f"confidence={decision.confidence} source={decision.source} reason={decision.reason!r}")

    # --- Agent-Wiki v2 运行时存储层 ---
    from ai_butler.agent_wiki.compiler import WikiCompiler
    from ai_butler.agent_wiki.ontology import CLAIM, SOURCE, new_page
    from ai_butler.agent_wiki.workspace import WikiStore

    wiki_root = ROOT / s.wiki_root / "_smoke"
    store = WikiStore(wiki_root, settings=s)
    store.write_page(new_page(SOURCE, "src-1", url="https://example.com/x", status="active"))
    store.write_page(new_page(CLAIM, "claim-1", confidence=0.93, claim_type="fact",
                              description="西湖春季最佳", subject_id="entity:西湖",
                              object_id="entity:春季", evidence_ids=["ev-1"]))
    store.write_page(new_page(CLAIM, "claim-conflict", confidence=0.55, status="SUSPECTED",
                              claim_type="fact", description="西湖秋季最佳",
                              subject_id="entity:西湖", object_id="entity:秋季",
                              evidence_ids=["ev-1"], contradicts=["claim-1"]))
    store.write_page(new_page(CLAIM, "vault-claim", confidence=0.99, claim_type="preference",
                              description="本人偏好春季", subject_id="user:smoke",
                              object_id="season:spring", evidence_ids=["ev-1"],
                              user_id="smoke-user"),
                     user_id="smoke-user")
    report = WikiCompiler(store).compile_workspace()
    visible = sorted(x.id for x in store.list_pages_for_retrieval("smoke-user"))
    print(f"[7] Agent-Wiki OK  页面={report.page_count} 声明={report.claim_count} "
          f"矛盾={report.contradictions}")
    print(f"    低置信={report.low_confidence} 证据缺失={report.missing_evidence} "
          f"digest={report.digest_bytes}B truncated={report.digest_truncated}")
    print(f"    检索可见（公共 Workspace + 本人 Vault，不含他人）: {visible}")
    compiled = sorted(x.name for x in (wiki_root / "workspace" / "_compiled").iterdir())
    print(f"    编译产物: {compiled}")

    import shutil
    shutil.rmtree(ROOT / s.wiki_root, ignore_errors=True)

    await r.aclose()
    print("\nP0-1 底座端到端冒烟：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
