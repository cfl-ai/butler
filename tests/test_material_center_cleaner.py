"""P2 清洗 / 去重 / 分类 / 时效打标测试。

计划书依据：三、P2「实现文档清洗、去重、分类、时效打标，转换为 Wiki Source 源页面」
+「强制所有素材绑定溯源来源，对应 Wiki Evidence 证据条目」
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_butler.agent_wiki.ontology import SOURCE, SOURCE_ACTIVE
from ai_butler.material_center.cleaner import (
    CATEGORY_NEWS,
    CATEGORY_OTHER,
    CATEGORY_SCENIC,
    CATEGORY_TICKET,
    CATEGORY_TRAFFIC,
    CATEGORY_WEATHER,
    CATEGORIES,
    CATEGORY_TTL_DAYS,
    CleanError,
    RawDocument,
    clean_batch,
    clean_document,
    clean_text,
    classify,
    content_hash,
    deduplicate,
    is_normative_document,
    ttl_days,
    to_wiki_source_page,
)

HTML = """<html><head><style>body{}</style><script>alert(1)</script></head>
<body><h1>西湖景区公告</h1>
<p>西湖景区即日起调整开放时间，春季 6:30 开放。</p>
<p>门票预约请通过官方渠道办理。</p>
版权所有 某某公司
转载请注明出处
编辑：张三
</body></html>"""


def _raw(doc_id="doc-1", text=HTML, title="西湖景区公告", **kw):
    base = dict(doc_id=doc_id, url=f"https://example.com/{doc_id}", title=title, text=text)
    base.update(kw)
    return RawDocument(**base)


# ------------------------------ 清洗 ------------------------------ #
def test_clean_strips_markup_and_boilerplate():
    text, removed = clean_text(HTML)
    assert "<" not in text and "alert(1)" not in text and "body{}" not in text
    assert "西湖景区即日起调整开放时间" in text
    assert "版权所有" not in text and "转载请注明" not in text and "编辑：" not in text
    assert removed >= 3


def test_block_tags_preserve_line_structure_so_boilerplate_is_removable():
    """块级标签若被当成空格，整页压成一行，行首样板行就再也删不掉了。"""
    html = ("<div><p>正文第一段。</p><p>正文第二段。</p>"
            "版权所有 某某公司</div>")
    text, removed = clean_text(html)
    assert "版权所有" not in text, "块级标签必须换成换行，否则样板行不在行首、删不掉"
    assert removed >= 1
    assert "正文第一段。" in text and "正文第二段。" in text


def test_clean_normalises_repeated_punctuation_and_blank_lines():
    text, _ = clean_text("好。。。。\n\n\n\n下一段")
    assert "好。" in text and "。。" not in text
    assert "\n\n\n" not in text


def test_empty_after_clean_is_refused():
    with pytest.raises(CleanError):
        clean_document(_raw(text="<html><script>x</script></html>"))


# ------------------------------ 分类 ------------------------------ #
def test_categories_come_from_plan_public_domain_and_travel_module():
    assert CATEGORIES == ("景点", "天气", "交通", "门票", "公开资讯", "其他")
    assert set(CATEGORY_TTL_DAYS) == set(CATEGORIES)


@pytest.mark.parametrize("text,expected", [
    ("明天天气晴朗，气温 22 度", CATEGORY_WEATHER),
    ("地铁 1 号线因施工限流，交通拥堵", CATEGORY_TRAFFIC),
    ("门票价格调整为 60 元，需提前预约", CATEGORY_TICKET),
    ("景区开放时间调整，排队时间较长", CATEGORY_SCENIC),
    ("官方发布公告通知", CATEGORY_NEWS),
    ("一段与任何类别都无关的文本", CATEGORY_OTHER),
])
def test_classification(text, expected):
    assert classify(text) == expected


def test_ttl_differs_by_category():
    assert ttl_days(CATEGORY_WEATHER) < ttl_days(CATEGORY_TRAFFIC)
    assert ttl_days(CATEGORY_TRAFFIC) < ttl_days(CATEGORY_SCENIC)
    assert ttl_days("不存在的类别") == CATEGORY_TTL_DAYS[CATEGORY_OTHER]


def test_stale_marking_uses_category_ttl():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    weather = clean_document(_raw(text="明天天气晴朗，气温 22 度"), now=now)
    assert weather.category == CATEGORY_WEATHER

    assert weather.is_stale(now=now) is False
    assert weather.is_stale(now=now + timedelta(days=2)) is True

    scenic = clean_document(_raw(doc_id="d2", text="景区开放时间调整"), now=now)
    assert scenic.is_stale(now=now + timedelta(days=2)) is False
    assert scenic.is_stale(now=now + timedelta(days=400)) is True


# ------------------------------ 去重 ------------------------------ #
def test_content_hash_ignores_whitespace_differences():
    assert content_hash("西湖 春季\n最佳") == content_hash("西湖春季最佳")


def test_duplicates_are_marked_not_silently_dropped():
    a = clean_document(_raw(doc_id="d1", text="西湖春季最佳游览季节，景色宜人。"))
    b = clean_document(_raw(doc_id="d2", text="西湖春季最佳游览季节，  景色宜人。"))
    out = deduplicate([a, b])
    assert len(out) == 2, "重复项必须保留在结果里，只是打上标记"
    assert out[0].is_duplicate is False
    assert out[1].is_duplicate is True and out[1].duplicate_of == "d1"


def test_clean_batch_pipeline():
    docs = [_raw(doc_id="d1"), _raw(doc_id="d2")]
    out = clean_batch(docs)
    assert len(out) == 2 and all(d.text for d in out)


# ---------------------- 转换为 Wiki Source 页面 ---------------------- #
def test_wiki_source_page_is_public_by_default_and_carries_provenance():
    """计划书三、P2「转换为 Wiki Source 源页面，保存来源元数据」+ 素材溯源库无用户 ID。"""
    cleaned = clean_document(_raw())
    page = to_wiki_source_page(cleaned, now="2026-10-16T00:00:00Z")

    assert page.type == SOURCE
    assert page.user_id is None, "素材溯源库属公共 Workspace，不得带 user_id"
    assert page.status == SOURCE_ACTIVE
    assert page.extra["url"] == cleaned.url
    assert page.extra["category"] == cleaned.category
    assert page.extra["content_hash"] == cleaned.content_hash
    assert page.extra["stale_after"] == cleaned.stale_after


def test_wiki_source_page_can_target_a_private_vault():
    cleaned = clean_document(_raw(doc_id="mine", text="我自己的行程笔记，仅供参考。"))
    page = to_wiki_source_page(cleaned, user_id="user-A")
    assert page.user_id == "user-A"


def test_duplicate_document_cannot_become_a_source_page():
    a = clean_document(_raw(doc_id="d1", text="西湖春季最佳游览季节，景色宜人。"))
    b = clean_document(_raw(doc_id="d2", text="西湖春季最佳游览季节，  景色宜人。"))
    dup = deduplicate([a, b])[1]
    with pytest.raises(CleanError):
        to_wiki_source_page(dup)


# ------------------- 规范性文件不按资讯 TTL 过期（U122） ------------------- #
def test_normative_document_gets_no_expiry_date():
    """制度文件靠新版本替换，不靠时间到期。

    按「发布日 + 资讯 TTL(30 天)」算，2013 年的 531 号办法入库即过期十年；
    `archive_stale` 一旦被装配，这批素材会被静默清掉（用户侧表现为「答不出来」）。
    """
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    doc = clean_document(_raw(
        text="财政部关于印发《中央和国家机关差旅费管理办法》的通知 财行〔2013〕531号\n"
             "第一条 为加强和规范中央和国家机关国内差旅费管理，制定本办法。",
        published_at="2013-12-31"), now=now)
    assert is_normative_document(doc.text)
    assert doc.stale_after == "", "规范性文件不设到期日"
    assert doc.is_stale(now=now) is False
    assert doc.is_stale(now=now + timedelta(days=3650)) is False, "十年后也不该判过期"


def test_news_without_doc_number_still_expires():
    """没有文号的新闻/解读照旧按 TTL 过期 —— 否则「永不过期」会被这一条规则放宽到全库。"""
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    doc = clean_document(_raw(text="差旅住宿费标准调整，明年起执行，各地按新标准报销。",
                              published_at="2015-10-20"), now=now)
    assert not is_normative_document(doc.text)
    assert doc.stale_after, "资讯类必须仍被打上到期日"
    assert doc.is_stale(now=now) is True
