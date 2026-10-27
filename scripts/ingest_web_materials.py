"""联网素材 → 三轮核验 → 入库（素材溯源库 + Wiki 源页面 + 检索索引）。

用法
----
    python scripts/ingest_web_materials.py --candidates data/dev/_web/candidates_raw.json

流程
----
1. 读**抓取脚本落下的原文**（不是模型转述 —— 转述会把模型可能的编造写进事实源）
2. 正文裁剪：剥掉页面导航与页脚，只留制度正文
3. `material_center.admission.review()` 三轮核验：真的收，假的剔，判不了的剔且标「无法核实」
4. 通过者走 `MaterialIngestor`（与线上同一条管道、同一套 chat_vector / chat_bm25 / wiki 路径）
5. 写报告 + **检索自检**（入库 ≠ 能检索到）

为什么必须做检索自检
------------------
「入了几条」是管道自己报的数，不是用户能不能用到的证据（E-114 同族）。
落库后立刻用标题词检索一遍，命中 0 就等于没入。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ai_butler.core.config import load_settings            # noqa: E402
from ai_butler.core.db import Database                     # noqa: E402
from ai_butler.material_center.admission import Candidate, review  # noqa: E402
from ai_butler.material_center.cleaner import RawDocument  # noqa: E402
from ai_butler.material_center.materials import MaterialIngestor  # noqa: E402

#: 页脚/导航噪音：遇到就截断（这些行对检索只有负作用）
_TAIL_MARKERS: tuple[str, ...] = (
    "扫一扫", "打印  关闭", "打印 关闭", "相关附件", "相关新闻", "相关图片",
    "Produced By", "版权所有", "网站标识码", "京ICP", "京公网安备",
    "呼叫中心", "违法和不良信息举报", "主办：", "运行维护单位", "部信息网络中心",
)
#: 正文起点标记：制度正文真正的开头
_HEAD_MARKERS: tuple[str, ...] = ("第一章", "第一条", "题：", "日电")
#: 文号：常出现在抬头之前，保留它才能判断「这是哪份文件」
_DOC_NO = re.compile(r"财[行办]*\s*[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*\d{1,4}\s*号")
#: 站点名式标题：`<title>` 取到的是网站名而不是文件标题时，得从正文里找回
_GENERIC_TITLE = re.compile(r"^(财政法规管理系统|.*(?:管理系统|门户网站|首页))$")
#: 正文里的正式标题
_TITLE_IN_BODY = re.compile(r"关于[印转发]{1,2}发?《[^》]{4,60}》的(?:通知|办法|规定)")
#: 标题的站点后缀：人民网、中国政府网的 `--栏目-站点` / `_栏目_站点`
_SITE_SUFFIX = re.compile(r"(?:--|__|_)[^-_]{0,24}(?:人民网|中国政府网|新华网|央广网|中国之声).*$|(?:--|__)[^-_]{2,24}$")

#: 原文发布日期（页面标注/正文落款）。**不从 URL 猜**，映射不到就留空。
PUBLISHED_BY_URL: dict[str, str] = {
    "https://www.mof.gov.cn/gkml/caizhengwengao/wg2014/wg201402/201407/t20140728_1118944.htm": "2013-12-31",
    "https://www.gov.cn/gongbao/content/2014/content_2667621.htm": "2013-12-31",
    "https://cpc.people.com.cn/n/2014/0106/c64387-24039018.html": "2014-01-06",
    "http://audit.shandong.gov.cn/art/2018/8/17/art_89386_7102382.html": "2013-12-31",
    "http://tcx.xnjwjc.gov.cn/zcfg_115/201407/t20140701_1869371.html": "2013-12-31",
    "http://fgk.mof.gov.cn/ui/src/views/law_html/84298.html": "2016-04-01",
    "https://cpc.people.com.cn/n/2015/1020/c64387-27720758.html": "2015-10-20",
    "https://www.gov.cn/zhengce/2019-07/18/content_5411420.htm": "2019-07-18",
    "https://m.cnr.cn/henan/xw/xwzt/20190718/t20190718_524696805.html": "2019-07-18",
}


def trim_body(text: str, *, head_markers: tuple[str, ...] = _HEAD_MARKERS) -> str:
    """裁掉导航与页脚，只留正文。

    不能只靠「去空行」：导航段落同样有内容，会把检索稀释成「首页 职能机构 新闻…」。
    """
    body = (text or "").strip()
    if not body:
        return ""

    # 起点先定：页脚标记（如「扫一扫」）在**导航区**也会出现，
    # 先截页脚会把正文一起砍掉（财政部那页只剩 203 字，就是这么丢的）。
    starts = [body.find(m) for m in head_markers if body.find(m) >= 0]
    start = min(starts) if starts else 0
    if start > 0:
        doc = _DOC_NO.search(body)
        if doc and 0 <= doc.start() < start and start - doc.start() < 1200:
            start = doc.start()          # 从文号起，保住「这是哪份文件」

    head = body[:start]
    body = body[start:]

    end = len(body)
    for marker in _TAIL_MARKERS:
        idx = body.find(marker)
        if 0 <= idx < end:
            end = idx
    # start == 0 表示没找到正文标记：前面那段导航也要留着（总比没有正文好），
    # 仍按页脚截断处理。
    body = (head if start == 0 else "") + body[:end]

    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and len(ln) >= 4]
    return "\n".join(_strip_leading_noise(lines)).strip()


def _strip_leading_noise(lines: list[str]) -> list[str]:
    """丢掉**开头连续的**导航碎片（「专题 云平台」「高层 动态 组织结构」这类）。

    只处理开头：正文中间的短行可能是「第一章 总 则」，一律丢会把结构弄没。
    """
    i = 0
    while i < len(lines):
        line = lines[i]
        if len(line) >= 12 or re.search(r"[。！？；：，、]", line):
            # 「题：…」「日电 题：…」是标题行（正文标题已在 title 字段），
            # 起因是正文标记正好落在「题：」「日电」上 —— 这一行不是正文。
            if not (line.startswith("题：") or line.startswith("日电")) or i > 0:
                break
        i += 1
    return lines[i:]


def pick_title(title: str, body: str) -> str:
    """`<title>` → 真正的文件标题。

    两种坏情况：拿到的只是网站名（「财政法规管理系统」），或带着栏目与站点后缀
    （「…明年起调整--新闻报道-人民网」）。前者从正文找回，后者切掉后缀。
    """
    clean = (title or "").strip()
    if not clean or _GENERIC_TITLE.match(clean):
        found = _TITLE_IN_BODY.search(body or "")
        if found:
            return found.group(0)
    trimmed = _SITE_SUFFIX.sub("", clean).strip(" -_")
    return trimmed if len(trimmed) >= 6 else clean


def build_candidates(rows: list[dict]) -> list[Candidate]:
    """抓取结果 → 候选。**取不到正文的也保留**，交给核验判「无法核实」并留痕。"""
    out: list[Candidate] = []
    for i, row in enumerate(rows, 1):
        url = row.get("url", "")
        text = trim_body(row.get("text") or "") if row.get("http_code") == 200 else ""
        out.append(Candidate(
            doc_id=f"web-{i:02d}", url=url,
            title=pick_title(row.get("title") or "", text),
            text=text, publisher=row.get("publisher", ""),
            published_at=PUBLISHED_BY_URL.get(url)))
    return out


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="联网素材三轮核验后入库")
    parser.add_argument("--candidates", default="data/dev/_web/candidates_raw.json")
    parser.add_argument("--report", default="data/dev/_web/admission_report.json")
    parser.add_argument("--dry-run", action="store_true", help="只核验不落库")
    args = parser.parse_args(argv)

    settings = load_settings()
    rows = json.loads(pathlib.Path(args.candidates).read_text(encoding="utf-8"))
    candidates = build_candidates(rows)

    report = review(candidates)
    summary = report.as_dict()

    print(f"候选 {summary['candidates']} 条")
    for rnd in summary["rounds"]:
        print(f"  第 {rnd['round']} 轮：检查 {rnd['checked']}，收 {rnd['accepted']}，"
              f"待定 {rnd['pending']}")
    for v in summary["verdicts"]:
        flag = "收" if v["accepted"] else ("假" if v["is_false"] else "拒")
        print(f"  [{flag}] {v['doc_id']} {v['code']}: {v['reason']}")

    out: dict = {"admission": summary}
    if not args.dry_run and report.accepted:
        from ai_butler.agent_wiki.wiring import build_wiki_store
        from ai_butler.data_platform.models import Base
        from ai_butler.retrieval.wiring import SearchWiring

        db = Database(settings)
        wiki = build_wiki_store(settings)
        service = SearchWiring(settings).service()

        async with db.write_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        raws = [RawDocument(doc_id=c.doc_id, url=c.url, title=c.title, text=c.text,
                            publisher=c.publisher, published_at=c.published_at,
                            fetched_at=datetime.now(timezone.utc).isoformat())
                for c in report.accepted]
        ingestor = MaterialIngestor(database=db, wiki_store=wiki, search_service=service)
        ingest = await ingestor.ingest(raws, user_id=None)
        out["ingest"] = {
            "collected": ingest.collected, "cleaned": ingest.cleaned,
            "duplicates": ingest.duplicates, "created": ingest.created,
            "refreshed": ingest.refreshed, "superseded": ingest.superseded,
            "wiki_pages": ingest.wiki_pages, "indexed_chunks": ingest.indexed_chunks,
            "failures": ingest.failures,
        }
        print(f"入库：新建 {len(ingest.created)}，刷新 {len(ingest.refreshed)}，"
              f"重复 {ingest.duplicates}，切片 {ingest.indexed_chunks}，"
              f"Wiki 页面 {len(ingest.wiki_pages)}")
        if ingest.failures:
            print("失败：", ingest.failures)

        checks = []
        for c in report.accepted:
            probe = re.sub(r"[《》〈〉（）()\s]", "", c.title)[:18]
            hits = service.search(probe, user_id=settings.guest_public_scope)
            checks.append({"doc_id": c.doc_id, "probe": probe, "hits": hits.count,
                           "top": hits.hits[0].doc_id if hits.hits else None})
            print(f"  检索自检 {probe} → {hits.count} 条")
        out["retrieval_check"] = checks

    pathlib.Path(args.report).write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("报告：", args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
