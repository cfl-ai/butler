"""入库准入三轮核验的守护。

要点：**判不了 ≠ 判不过**。测试必须能区分「被判为虚假」与「无法核实」，
否则报告里一句「筛掉 8 条」会被读成「抓到 8 条假消息」。
"""

from __future__ import annotations

import pytest

from ai_butler.material_center.admission import (
    ACCEPT_AUTHORITATIVE,
    ACCEPT_CORROBORATED,
    ACCEPT_THIRD_ROUND,
    Candidate,
    REJECT_CONTRADICTED,
    REJECT_DUPLICATE,
    REJECT_NO_BODY,
    REJECT_PARTIAL,
    document_numbers,
    review,
    shingles,
    similarity,
    source_tier,
    TIER_AUTHORITATIVE,
    TIER_INSTITUTIONAL,
    TIER_UNKNOWN,
)

#: 一段足够长的制度正文（用于「正文完整」判定）。**必须带文号**：
#: 文号是「同一文件」的锚点，缺了它矛盾检测根本无从比对。
BODY_A = (
    "财行〔2013〕531号\n"
    "第一条 为加强和规范中央和国家机关国内差旅费管理，推进厉行节约反对浪费，"
    "根据《党政机关厉行节约反对浪费条例》，制定本办法。\n"
    "第三条 差旅费是指工作人员临时到常驻地以外地区公务出差所发生的城市间交通费、"
    "住宿费、伙食补助费和市内交通费。\n"
    "第二十条 市内交通费按出差自然日历天数计算，每人每天80元包干使用。\n"
    "第二十三条 城市间交通费按乘坐交通工具的等级凭据报销，订票费、经批准发生的"
    "签转或退票费、交通意外保险费凭据报销。住宿费在标准限额之内凭发票据实报销。\n"
    "第三十四条 本办法自2014年1月1日起施行。\n"
) * 4

#: 同一政策的另一种表述（换词、换说法，核心数字一致）。
#: 与 BODY_A 的字符 3-gram 重合约 0.7 —— 正落在「互证通过、又不到逐字重复」的区间。
BODY_B = BODY_A.replace("中央和国家机关", "中央单位").replace(
    "差旅费", "出差费用").replace("包干使用", "包干").replace(
    "凭据报销", "按票据结算").replace("工作人员", "职工").replace("常驻地", "常驻地区")

#: 与 BODY_A 主题相同但**表述完全不同**的长文本：用来造「同文号不同内容」
BODY_FAKE = (
    "第一条 本规范适用于全体职工的出差活动，出差前须填写电子申请单并经系统审批。\n"
    "第二条 住宿费一律按每人每晚三百元包干，无需发票，由本人自行联系酒店入住。\n"
    "第三条 伙食补助按出差天数乘以五十元发放，现金形式当月结清，不计入工资总额。\n"
    "第四条 市内交通费实报实销，网约车与出租车均可，不设上限，凭支付截图办理。\n"
    "第五条 本规范由行政部门负责解释，自发布之日起执行，原有规定同时废止。\n"
) * 6


def _doc(doc_id: str, url: str, text: str, title: str = "中央和国家机关差旅费管理办法"):
    return Candidate(doc_id=doc_id, url=url, title=title, text=text)


# ---------------------------------------------------------------- 来源分级
@pytest.mark.parametrize("url,tier", [
    ("https://www.mof.gov.cn/gkml/x.htm", TIER_AUTHORITATIVE),
    ("https://www.gov.cn/gongbao/content_1.htm", TIER_AUTHORITATIVE),
    ("https://cpc.people.com.cn/n/2014/0106/x.html", TIER_AUTHORITATIVE),
    ("http://audit.shandong.gov.cn/art/x.html", TIER_INSTITUTIONAL),
    ("http://tcx.xnjwjc.gov.cn/zcfg/x.html", TIER_INSTITUTIONAL),
    ("https://www.toutiao.com/article/6715/", TIER_UNKNOWN),
    ("", TIER_UNKNOWN),
])
def test_source_tier_reads_only_the_host(url, tier):
    """地方 `.gov.cn` 不能算中央权威 —— 一刀切 `endswith('.gov.cn')` 会把
    省审计厅、县纪委全升成 A 级，相当于「谁转载谁权威」。"""
    assert source_tier(url) == tier


def test_similarity_ignores_punctuation_and_space():
    assert similarity("市内交通费每人每天80元。", "市内交通费 每人每天80元") > 0.9
    assert similarity("完全不相关的内容甲", "另一份毫不相干的文本乙") < 0.1


def test_similarity_of_empty_is_zero_not_one():
    """两个空集合不能被当成「完全吻合」—— 那会让没抓到正文的候选互证通过。"""
    assert similarity("", "") == 0.0
    assert shingles("", 3) == set()


def test_document_numbers_normalizes_brackets():
    assert document_numbers("（财行〔2013〕531号）") == {"2013-531"}
    assert document_numbers("财行[2016]71号") == {"2016-71"}


# ---------------------------------------------------------------- 第 1 轮
def test_round1_takes_authoritative_with_full_body():
    report = review([_doc("a", "https://www.mof.gov.cn/x.htm", BODY_A)])
    assert [c.doc_id for c in report.accepted] == ["a"]
    assert report.verdicts[0].code == ACCEPT_AUTHORITATIVE
    assert report.rounds[0]["accepted"] == 1


def test_round1_rejects_duplicate_of_already_taken():
    """同一份文件的第二个发布渠道不能重复入库 —— 否则检索被同一段话霸屏。"""
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.gov.cn/y.htm", BODY_A),
    ])
    assert [c.doc_id for c in report.accepted] == ["a"]
    second = next(v for v in report.verdicts if v.doc_id == "b")
    assert second.code == REJECT_DUPLICATE
    assert not second.accepted
    assert not second.is_false, "重复转载不是虚假信息，不能记成假"


def test_authoritative_but_short_body_does_not_pass_round1():
    """域名权威 ≠ 内容够格：节选要先过互证这一关。"""
    report = review([_doc("a", "https://www.mof.gov.cn/x.htm", BODY_A[:200])])
    assert report.rounds[0]["accepted"] == 0
    assert report.rounds[0]["pending"] == 1


# ---------------------------------------------------------------- 第 2 轮
def test_round2_takes_institutional_source_corroborated_by_peer():
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "http://audit.shandong.gov.cn/x.html", BODY_B),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.accepted and got.code == ACCEPT_CORROBORATED
    assert got.similarity and 0.45 <= got.similarity < 0.85


def test_round2_rejects_near_verbatim_copy_from_non_authority():
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.toutiao.com/a/", BODY_A),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.code == REJECT_DUPLICATE and not got.accepted


# ---------------------------------------------------------------- 第 3 轮
def test_round3_flags_same_doc_number_with_different_content_as_false():
    """自称是 531 号办法、正文却是另一套内容 → 判为虚假（这类才是「假消息」）。"""
    fake = "财行〔2013〕531号\n" + BODY_FAKE
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.toutiao.com/a/", fake),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.code == REJECT_CONTRADICTED
    assert got.is_false is True
    assert report.false_count() == 1


def test_round3_does_not_call_short_digest_contradiction():
    """短报道与全文重合度天然低，不能因此被当成篡改 —— 宁可漏判不可错杀。"""
    short = "财行〔2013〕531号\n" + BODY_FAKE[:300]
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.toutiao.com/a/", short),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.code != REJECT_CONTRADICTED


def test_round3_rejects_partial_body_that_only_echoes_existing():
    """节选与已收内容有交叉但自身不完整 → 不独立成证据。"""
    digest = BODY_A[:400]
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://cpc.people.com.cn/x.html", digest),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.code in (REJECT_PARTIAL, REJECT_DUPLICATE)
    assert not got.is_false


def test_no_body_document_is_unverifiable_not_false():
    """抓不到正文 = 无法核实，**绝不能记成虚假**。"""
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.lj.gov.cn/x.html", ""),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.code == REJECT_NO_BODY
    assert not got.is_false
    assert report.unverifiable_count() == 1
    assert report.false_count() == 0


def test_unknown_source_without_overlap_is_rejected():
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.toutiao.com/a/", BODY_FAKE),
    ])
    got = next(v for v in report.verdicts if v.doc_id == "b")
    assert got.code == REJECT_CONTRADICTED or got.code == "unverifiable_no_overlap"
    assert not got.accepted


# ---------------------------------------------------------------- 整体口径
def test_three_rounds_run_in_order_and_report_them():
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "http://audit.shandong.gov.cn/x.html", BODY_A.replace("80元", "80 元")),
        _doc("c", "https://www.lj.gov.cn/x.html", ""),
    ])
    numbers = [r["round"] for r in report.rounds]
    assert numbers[:3] == [1, 2, 3]
    # 每轮都要有被检查的数量（不能出现空转的「0 检查」）
    assert all(r["checked"] > 0 for r in report.rounds)


def test_report_counts_false_and_unverifiable_separately():
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.toutiao.com/a/", "财行〔2013〕531号\n" + BODY_FAKE),
        _doc("c", "https://www.lj.gov.cn/x.html", ""),
    ])
    data = report.as_dict()
    assert data["accepted"] == 1
    assert data["false_rejected"] == 1
    assert data["unverifiable_rejected"] == 1
    assert data["rejected"] == data["false_rejected"] + data["unverifiable_rejected"]


def test_every_candidate_gets_exactly_one_verdict():
    docs = [
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "http://audit.shandong.gov.cn/x.html", BODY_A.replace("80元", "80 元")),
        _doc("c", "https://www.lj.gov.cn/x.html", ""),
        _doc("d", "https://www.toutiao.com/a/", BODY_FAKE),
    ]
    report = review(docs)
    assert len(report.verdicts) == len(docs)
    assert {v.doc_id for v in report.verdicts} == {"a", "b", "c", "d"}
    assert all(v.round_no >= 1 for v in report.verdicts)


def test_accepted_documents_are_never_duplicates_of_each_other():
    """入库集合内部不能有近乎重复的两条 —— 这是索引冗余的根因。"""
    report = review([
        _doc("a", "https://www.mof.gov.cn/x.htm", BODY_A),
        _doc("b", "https://www.gov.cn/y.htm", BODY_A),
        _doc("c", "http://audit.shandong.gov.cn/z.html", BODY_A),
    ])
    for i, left in enumerate(report.accepted):
        for right in report.accepted[i + 1:]:
            assert similarity(left.text, right.text) < 0.85, "入库集合里出现了逐字重复"
