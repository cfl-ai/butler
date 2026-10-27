"""采集脚本的正文裁剪与标题恢复的守护。

这两处踩过坑，都留在这里钉住：
- 页脚标记（「扫一扫」）在**导航区**也会出现，先截页脚会把正文一起砍掉；
- `<title>` 常常是网站名（「财政法规管理系统」），直接入库等于给检索一个错误标题。
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "ingest_web_materials", ROOT / "scripts" / "ingest_web_materials.py")
mod = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(mod)


def test_footer_marker_inside_navigation_does_not_cut_the_body():
    """财政部那页「扫一扫」出现在导航区（第 280 字），若先按页脚截断，
    正文只剩 203 字 —— 一份 34 条的办法会被截成一段公告旁白。"""
    text = (
        "首页 职能机构 新闻报道 扫一扫\n"
        "财政部关于印发《中央和国家机关差旅费管理办法》的通知\n"
        "第一章 总 则\n"
        "第一条 为加强和规范中央和国家机关国内差旅费管理，推进厉行节约反对浪费，制定本办法。\n"
        "第二条 本办法适用于中央和国家机关，以及参照公务员法管理的事业单位。\n"
        "第三十四条 本办法自2014年1月1日起施行。\n"
        "版权所有 财政部信息网络中心\n"
    )
    body = mod.trim_body(text)
    assert "第一条" in body and "第三十四条" in body
    assert "扫一扫" not in body
    assert "版权所有" not in body
    assert len(body) > 80


def test_leading_navigation_fragments_are_dropped():
    """人民网页面没有「第一条」这类正文标记，导航会整段留在开头。"""
    text = (
        "专题 云平台\n"
        "高层 动态 组织结构\n"
        "中央和国家机关差旅住宿费标准明年起调整，财政部20日发布通知，调整住宿费标准。\n"
    )
    body = mod.trim_body(text)
    assert body.splitlines()[0].startswith("中央和国家机关差旅住宿费标准明年起调整")


def test_mid_body_short_lines_are_kept():
    """只丢**开头连续**的碎片：正文中间的「第一章 总 则」不能被当成导航删掉。"""
    text = (
        "第一章 总 则\n"
        "第一条 为加强和规范中央和国家机关国内差旅费管理，推进厉行节约反对浪费，制定本办法。\n"
        "第二章 城市间交通费\n"
        "第六条 城市间交通费是指工作人员因公到常驻地以外地区出差乘坐交通工具发生的费用。\n"
    )
    body = mod.trim_body(text)
    assert "第二章 城市间交通费" in body


def test_pick_title_recovers_real_title_from_body():
    """`<title>` 只是网站名时，标题要从正文里认回来。"""
    body = (
        "关于印发《中央和国家机关工作人员赴地方差旅住宿费标准明细表》的通知\n"
        "财行〔2016〕71号\n财政部 2016年4月1日\n"
    )
    got = mod.pick_title("财政法规管理系统", body)
    assert got.startswith("关于印发《中央和国家机关工作人员赴地方差旅住宿费标准明细表》")


@pytest.mark.parametrize("raw,expected_prefix", [
    ("中央和国家机关差旅住宿费标准明年起调整--新闻报道-人民网",
     "中央和国家机关差旅住宿费标准明年起调整"),
    ("落实中央八项规定精神的务实之举——详解中央单位差旅伙食费管理新规_政策解读_中国政府网",
     "落实中央八项规定精神的务实之举"),
])
def test_pick_title_strips_site_suffix(raw, expected_prefix):
    assert mod.pick_title(raw, "").startswith(expected_prefix)


def test_pick_title_keeps_good_title_untouched():
    good = "公职人员注意！8月起，差旅伙食费和市内交通费有变化！"
    assert mod.pick_title(good, "") == good


def test_failed_fetch_is_kept_as_empty_candidate():
    """抓取失败不能悄悄丢掉 —— 丢掉了就没人知道「这条被考虑过但没成」。"""
    rows = [
        {"url": "https://www.lj.gov.cn/a.html", "http_code": 521, "text": "", "title": "x"},
        {"url": "https://www.mof.gov.cn/a.htm", "http_code": 200,
         "text": "第一章 总 则\n第一条 为加强和规范中央和国家机关国内差旅费管理，制定本办法。",
         "title": "财政部关于印发《中央和国家机关差旅费管理办法》的通知"},
    ]
    got = mod.build_candidates(rows)
    assert len(got) == 2
    assert got[0].body_chars == 0
    assert got[1].body_chars > 0


def test_published_date_is_never_invented():
    """发布时间只从映射表取；查不到就留空，**不拿抓取时间冒充发布日**。"""
    rows = [{"url": "https://example.com/unknown", "http_code": 200,
             "text": "第一章 总 则\n第一条 为加强和规范中央和国家机关国内差旅费管理的办法。",
             "title": "某个文件"}]
    assert mod.build_candidates(rows)[0].published_at is None
