"""交付物对照表必须覆盖计划书的全部条目，**做不到的要标成缺口而不是省略**。

缺陷背景
--------
《统一架构规范文档》§13 的 P0-2 对照表，每行都带「交付物 + 验证方式」，
读起来像是计划书九步全覆盖。但计划书第一步是
「制定 Git 分支规范、CI 自动构建、**自动部署流水线**」，
而表里那一行写的是「Git 分支规范 + CI」—— **把「自动部署」从原句里省掉了**。

省略比写「未落地」更危险：**读者会以为它已经做了。**
本文件守住这条：计划书点名的每一条都必须在表里有落点，
没做的可以标缺口，但不能不提。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "统一架构规范文档.md"

#: 计划书三、P0-2「开发内容与步骤」的九条（第一步含三个并列项，拆开守）。
#: 每一项给出若干关键词，**全部命中**才算这一条有落点。
P0_2_PLAN_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("①a Git 分支规范", ("Git 分支规范",)),
    ("①b CI 自动构建", ("CI 自动构建",)),
    ("①c 自动部署流水线", ("自动部署流水线",)),
    ("② LangGraph 单元测试框架", ("LangGraph", "单元测试框架")),
    ("③ 表 1 专项测试集合", ("表 1", "专项测试集合")),
    ("④ Prompt 版本库", ("Prompt 版本库",)),
    ("⑤ 全场景测试用例库", ("全场景测试用例库",)),
    ("⑥ 日志检索与缺陷统计", ("日志检索", "缺陷")),
    ("⑦ 自动化评测脚本", ("自动化评测脚本",)),
    ("⑧ Agent 标准化脚手架", ("Agent 标准化脚手架",)),
    ("⑨ 对接 Harness 自进化", ("Harness", "自进化")),
)

#: §13 的起止标记
SECTION_START = "## 13. AI 研发效能平台（P0-2）"


@pytest.fixture(scope="module")
def p0_2_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(SECTION_START)
    # 到下一个二级标题为止
    nxt = text.find("\n## ", start + len(SECTION_START))
    return text[start:nxt if nxt != -1 else len(text)]


def test_every_p0_2_plan_step_has_a_row(p0_2_section):
    """计划书九步，一步都不能从表里消失。

    **只在表格行里查，不在整节正文里查** —— 第一版查的是整节，
    结果表前面的一句说明里提到了「自动部署流水线」，就把删掉表格行的
    情况给放过了。关键词出现在散文里，不等于它是一行交付物。
    """
    table_text = "\n".join(_mapping_table(p0_2_section))
    missing = [
        label for label, keywords in P0_2_PLAN_STEPS
        if not all(k in table_text for k in keywords)
    ]
    assert not missing, (
        "§13 对照表漏掉了这些计划书条目（没做也要写成缺口，不能省略）：\n  "
        + "\n  ".join(missing)
    )


def test_unfinished_items_are_marked_as_gaps_not_omitted(p0_2_section):
    """没落地的条目必须**明确写成缺口**，并给出依据。

    自动部署流水线的目标环境已由裁定 U09 缓做 —— 这是可接受的现状，
    但表里必须说清楚，否则读者会以为它已经做了。
    同样只看表格行，不看正文。
    """
    table_text = "\n".join(_mapping_table(p0_2_section))
    assert "自动部署流水线" in table_text, "表里没有自动部署流水线这一行"
    assert "未落地" in table_text or "缺口" in table_text, (
        "自动部署流水线尚未落地，表中必须标明缺口"
    )
    assert "U09" in table_text, "缺口要给出裁定依据（U09）"


def _mapping_table(section: str) -> list[str]:
    """只取 P0-2 对照表本身。

    §13 里还有别的表（如场景条数统计），不能一把梭全查 ——
    否则会拿三列的规则去要求两列的表（第一版就这么错了）。
    """
    lines = section.splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if line.startswith("|") and "计划书条目" in line),
        None,
    )
    assert start is not None, "§13 找不到 P0-2 对照表"
    rows: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped:
            continue                       # 本文件的表格行之间夹着空行
        if not stripped.startswith("|"):
            break                          # 表格结束
        if set(stripped) <= set("|- "):
            continue                       # 分隔行
        rows.append(stripped)
    return rows


def test_table_rows_declare_both_deliverable_and_verification(p0_2_section):
    """对照表每一行都要有交付物与验证方式 —— 空壳行等于没写。"""
    rows = _mapping_table(p0_2_section)
    assert rows, "§13 的 P0-2 对照表是空的"
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) >= 3, f"对照表列数不足：{row}"
        assert cells[0], f"计划书条目列为空：{row}"
        assert cells[1], f"交付物列为空：{row}"
        assert cells[2], f"验证方式列为空：{row}"


# ============================== 四、全局固化参数 ============================== #
PARAM_SECTION_START = "## 4. 全局固化参数"


@pytest.fixture(scope="module")
def param_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(PARAM_SECTION_START)
    nxt = text.find("\n## ", start + len(PARAM_SECTION_START))
    return text[start:nxt if nxt != -1 else len(text)]


def test_doc_no_longer_claims_any_table_13_param_is_pending(param_section):
    """文档不得把**已经落地**的参数说成「待纳入」。

    §4.2 曾长期写着「待纳入（8 项，P3 路由落地时加入）」，
    而 P3 早已落地：那 8 个参数都在 TABLE_13 里，
    落点模块（scheduler.graph / scheduler.router / core.db / rag.collector）也都能导入。

    **文档说「待办」而代码早已做完，与反过来（说做了其实没做）一样有害** ——
    读者会据此以为还有活没干，或者据此重新去「纳入」一遍。
    """
    assert "待纳入" not in param_section, (
        "§4 仍写着「待纳入」——若确实还有未纳入的参数，请写清是哪几项、依据是什么"
    )


def test_param_section_lists_every_table_13_param(param_section):
    """表 13 的 18 个参数，§4 里一个都不能少。"""
    from ai_butler.devkit.acceptance import TABLE_13

    missing = [p.name for p in TABLE_13 if p.name not in param_section]
    assert not missing, f"§4 未列出这些全局固化参数：{missing}"


def test_the_pending_claim_would_have_been_false(param_section):
    """这条守的是**前提**：那 8 项确实已落地，所以「待纳入」本就是错的。

    如果哪天它们真的没落地，那说明本节的前提变了 ——
    这时应当去改文档与代码，而不是让这条测试悄悄失效。
    """
    from ai_butler.devkit.acceptance import TABLE_13, verify_table_13
    from ai_butler.core import phases

    assert "P3" in [p.code for p in phases.implemented_phases()], "P3 应已落地"
    assert verify_table_13() == [], "表 13 与代码应完全一致"
    assert len(TABLE_13) == 18

    # §4 的标题也要如实反映「全部已纳入」
    assert "18 项" in param_section, "§4.1 标题应写明已纳入 18 项"


# ============================== 四十六、计划书表格索引 ============================== #
TABLE_INDEX_START = "## 46. 计划书表格索引"

#: 计划书正文一共 13 张表，一张都不能从索引里消失
PLAN_TABLES = tuple(range(1, 14))


def _table_rows(section: str, header_keyword: str) -> list[list[str]]:
    """从某节里取表格行（本文件的表格行之间夹着空行）。"""
    lines = section.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.startswith("|") and header_keyword in l), None)
    assert start is not None, f"找不到表头含「{header_keyword}」的表"
    rows: list[list[str]] = []
    for line in lines[start + 1:]:
        s = line.strip()
        if not s:
            continue
        if not s.startswith("|"):
            break
        if set(s) <= set("|- "):
            continue
        rows.append([c.strip() for c in s.strip("|").split("|")])
    return rows


@pytest.fixture(scope="module")
def table_index_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(TABLE_INDEX_START)
    nxt = text.find(chr(10) + "## ", start + len(TABLE_INDEX_START))
    return text[start:nxt if nxt != -1 else len(text)]


def test_plan_table_index_covers_all_thirteen_tables(table_index_section):
    """计划书 13 张表，索引里一张都不能少。

    内容实现了却查不到对应关系，与「对照表省掉一条」是同一类问题：
    **汇总与真值源之间缺一层索引**。
    """
    rows = _table_rows(table_index_section, "计划书表")
    listed = set()
    for cells in rows:
        m = re.match(r"表\s*(\d+)", cells[0])
        if m:
            listed.add(int(m.group(1)))
    missing = sorted(set(PLAN_TABLES) - listed)
    assert not missing, f"计划书表格索引漏了：表 {missing}"
    assert not (listed - set(PLAN_TABLES)), f"索引里有多余的表号：{sorted(listed - set(PLAN_TABLES))}"


def test_every_indexed_chapter_actually_exists(table_index_section):
    """索引指向的章节必须真实存在 —— 改了章节号却忘了改索引会在这里变红。"""
    text = DOC.read_text(encoding="utf-8")
    chapters = {int(m.group(1)) for m in re.finditer(r"^##\s*(\d+)\.", text, re.M)}
    assert chapters, "文档里一个二级章节都没解析到"

    rows = _table_rows(table_index_section, "计划书表")
    problems: list[str] = []
    for cells in rows:
        for num in re.findall(r"第\s*(\d+)\s*章", cells[2]):
            if int(num) not in chapters:
                problems.append(f"{cells[0]} 指向的第 {num} 章不存在")
    assert not problems, "索引指向了不存在的章节：\n  " + "\n  ".join(problems)


# ============================== 文档里的计数必须是真的 ============================== #
def test_documented_case_count_matches_reality():
    """文档里写「全场景用例库：N 条」，那个 N 必须是**真的**。

    真事：用例库从 397 涨到 463，而文档一直写着 397 ——
    没有任何测试会因此变红，读者看到的是一个过期的数字。
    与 E-41（文档说「待办」而代码早做完）同型：**文档里的数字也是断言**。
    """
    import re

    from ai_butler.devkit.scenario_library import default_library

    text = DOC.read_text(encoding="utf-8")
    stated = re.findall(r"全场景用例库\*\*：?(\d+)\s*条", text)
    assert stated, "文档里没找到「全场景用例库：N 条」的表述（判据可能已失效）"
    actual = len(default_library().cases)
    for n in stated:
        assert int(n) == actual, f"文档写 {n} 条，实际 {actual} 条"


def test_documented_ledger_count_matches_reality():
    """文末「未定义项台账：N 项」的那个 N 和四项分布，也必须是**真的**。

    这是上一条的同类问题：台账从 41 长到 66，而文档结尾一直写着
    「41 项全部裁定（开源借鉴 26 / 人工授权 6 / 实现参数 1 / 文档推导 8）」。
    数字本身没错过，只是**没人回头改**——正好是「文档里的数字也是断言」
    最容易漏掉的那一半：过期不等于写错，但读者一样被误导。

    这里连**分布**一起守，因为只对总数时，改了分类归属也不会变红。
    """
    from collections import Counter

    from ai_butler.core import decisions

    text = DOC.read_text(encoding="utf-8")
    matched = re.search(
        r"未定义项台账\*\*：(\d+)\s*项全部裁定"
        r"（开源借鉴\s*(\d+)\s*/\s*人工授权\s*(\d+)\s*/\s*实现参数\s*(\d+)\s*/\s*文档推导\s*(\d+)）",
        text,
    )
    assert matched, "文末没找到「未定义项台账：N 项全部裁定（…）」的表述（判据可能已失效）"

    total, borrow, manual, param, derived = (int(g) for g in matched.groups())
    items = decisions.resolved()
    actual = Counter(i.source for i in items)

    assert total == len(items), f"文档写 {total} 项，实际 {len(items)} 项"
    assert borrow == actual["开源借鉴"], f"文档写开源借鉴 {borrow}，实际 {actual['开源借鉴']}"
    assert manual == actual["人工授权"], f"文档写人工授权 {manual}，实际 {actual['人工授权']}"
    assert param == actual["实现参数"], f"文档写实现参数 {param}，实际 {actual['实现参数']}"
    assert derived == actual["文档推导"], f"文档写文档推导 {derived}，实际 {actual['文档推导']}"

    # 分布之和必须等于总数：否则即使四项各自「看着对」，也拼不回总数。
    assert borrow + manual + param + derived == total, (
        f"四项分布之和 {borrow + manual + param + derived} 不等于总数 {total}"
    )

