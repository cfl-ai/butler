"""P2 采集链路测试。

计划书依据：三、P2「开发多源自动化爬虫采集链路」；五、3「统一底层采集底座」
"""

from __future__ import annotations

import httpx
import pytest

from ai_butler.material_center.cleaner import RawDocument
from ai_butler.material_center.collector import (
    CollectError,
    CollectPolicy,
    Collector,
    HttpConnector,
    LocalDirConnector,
    StaticConnector,
)


def _doc(doc_id, text="西湖景区公告，开放时间调整。"):
    return RawDocument(doc_id=doc_id, url=f"https://e.com/{doc_id}", title=doc_id, text=text)


# ------------------------------ 连接器 ------------------------------ #
def test_static_connector():
    conn = StaticConnector({"t1": _doc("t1")})
    assert conn.fetch("t1").doc_id == "t1"
    with pytest.raises(CollectError):
        conn.fetch("缺失")


def test_local_dir_connector_reads_files(workdir):
    root = workdir / "materials"
    root.mkdir()
    (root / "a.txt").write_text("西湖景区公告\n开放时间调整。", encoding="utf-8")
    (root / "b.txt").write_text("黄山门票价格公告", encoding="utf-8")

    conn = LocalDirConnector(root)
    assert conn.targets() == ["a", "b"]
    doc = conn.fetch("a")
    assert doc.title == "西湖景区公告" and "开放时间调整" in doc.text
    assert doc.url.startswith("file://")
    with pytest.raises(CollectError):
        conn.fetch("缺失")


def _mock_http(html: str, *, charset: str = "utf-8"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html.encode(charset),
                              headers={"content-type": f"text/html; charset={charset}"})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_connector_extracts_title_and_host():
    html = "<html><head><title> 西湖景区公告 </title></head><body>开放时间调整</body></html>"
    conn = HttpConnector(client=_mock_http(html))
    doc = conn.fetch("https://travel.example.com/news/1")
    assert doc.title == "西湖景区公告"
    assert doc.publisher == "travel.example.com"
    assert doc.url == "https://travel.example.com/news/1"


def test_http_connector_wraps_errors():
    def handler(request):
        return httpx.Response(500, content=b"boom")

    conn = HttpConnector(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CollectError) as ei:
        conn.fetch("https://e.com/x")
    assert "采集" in str(ei.value)


def test_collect_policy_validates():
    with pytest.raises(CollectError):
        CollectPolicy(min_interval_seconds=-1)
    with pytest.raises(CollectError):
        CollectPolicy(max_documents=0)


# ------------------------------ 编排 ------------------------------ #
def test_collector_routes_to_the_right_connector(workdir):
    root = workdir / "m"
    root.mkdir()
    (root / "local1.txt").write_text("本地素材标题\n正文内容。", encoding="utf-8")

    collector = Collector([StaticConnector({"s1": _doc("s1")}), LocalDirConnector(root)],
                          policy=CollectPolicy(min_interval_seconds=0))
    result = collector.collect([("static", "s1"), ("local-dir", "local1")])
    assert result.ok_count == 2 and result.failed_count == 0
    assert {d.doc_id for d in result.documents} == {"s1", "local1"}


def test_single_source_failure_does_not_break_the_rest():
    collector = Collector([StaticConnector({"ok": _doc("ok")})],
                          policy=CollectPolicy(min_interval_seconds=0))
    result = collector.collect([("static", "ok"), ("static", "缺失"), ("不存在", "x")])
    assert result.ok_count == 1
    assert result.failed_count == 2
    assert all("缺失" in target or "不存在" in target for target, _ in result.failures)


def test_max_documents_is_enforced():
    docs = {f"d{i}": _doc(f"d{i}") for i in range(10)}
    collector = Collector([StaticConnector(docs)], policy=CollectPolicy(
        min_interval_seconds=0, max_documents=3))
    assert collector.collect([("static", f"d{i}") for i in range(10)]).ok_count == 3


def test_politeness_interval_is_applied():
    import time
    collector = Collector([StaticConnector({f"d{i}": _doc(f"d{i}") for i in range(3)})],
                          policy=CollectPolicy(min_interval_seconds=0.05))
    started = time.monotonic()
    collector.collect([("static", "d0"), ("static", "d1"), ("static", "d2")])
    assert time.monotonic() - started >= 0.09, "同一来源的连续采集应遵守间隔"


def test_collector_requires_at_least_one_connector():
    with pytest.raises(CollectError):
        Collector([])
