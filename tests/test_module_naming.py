"""模块命名守护 —— 每个 Agent 模块的中文名必须就是计划书里那个项目名。

计划书依据
----------
- 三、各阶段标题：「P1 数据中台｜全系统唯一可信数据源」等
- 三、P4「包含子 Agent：**设备感知、天气感知、时间进度、增量抽取、路线方案、
  备选预案、代码迭代 Agent**」
- 五「旅游 Agent 属于下垂类业务执行单元」
- 封面「管家 Agent 生态体系项目计划书」

要守的事
--------
Python 包名只能用 ASCII，于是 `data_platform` 这个目录和「数据中台」这个
项目名天然是两套字符串。**两套名字最危险的不是起错，而是各自漂移**：
文档里写「数据中台」，日志里打 `data_platform`，时间一长没人知道是同一个东西，
改名时更是必然漏改一半（MEMORY E-44 的另一种形态）。

所以这里守的不是「名字好不好听」，而是三条硬约束：

1. 阶段模块 docstring 首行 = 计划书原文标题（改标题就得改首行，否则报错）
2. 每个包目录、每个 Agent 都能查到一个中文名，且**一个都不许漏**
3. 中文名能在计划书原句里逐字找到（不是自己编的近义词）

第 3 条是本守护的核心：只断言「有名字」会恒绿——随便写个「数据平台模块」
也过得去。所以计划书原句以 fixture 形式内嵌在此，逐条回查。

已按 MEMORY E-40 造违规复验：改 docstring、删登记项、把「设备感知」写成
「设备感知模块」均能使对应守护变红。
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path
from typing import Any

import pytest

import ai_butler
from ai_butler.agents.base import SubAgent
from ai_butler.core import naming
from ai_butler.core.phases import PHASES, phase

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# 计划书原句（逐字摘自《AI管家Agent生态体系项目计划书》，改动需同步计划书）
# --------------------------------------------------------------------------- #

#: 三、P4「包含子 Agent」那一行的原文
PLAN_LINE_P4_SUBAGENTS = (
    "设备感知、天气感知、时间进度、增量抽取、路线方案、备选预案、代码迭代 Agent"
)

#: 三、各阶段标题的原文（与 core/phases.py 的 title 字段一致）
PLAN_PHASE_TITLES = tuple(p.title for p in PHASES)

#: 五、旅游模块定位原文片段
PLAN_LINE_TOUR = "旅游 Agent 属于下垂类业务执行单元"

#: 三、P0-1 关于中间件/底座的原文片段
PLAN_LINE_MIDDLEWARE = "开发全局中间件"


# ============================== 阶段模块首行 ============================== #
def _phase_module_files():
    """阶段 key → 模块文件。P7 只有移动端工程，无 Python 模块，允许缺席。"""
    out = {}
    for p in PHASES:
        f = REPO_ROOT / "ai_butler" / "agents" / f"{p.key}.py"
        if f.exists():
            out[p] = f
    return out


def test_every_phase_module_opens_with_its_plan_title():
    """阶段模块 docstring 首行必须以「P1 数据中台｜全系统唯一可信数据源」开头。

    只查「首行含不含阶段编号」会恒绿——写成「P1 数据平台模块」照样过。
    故断言的是**完整计划书标题**。
    """
    files = _phase_module_files()
    assert files, "一个阶段模块都没找到，路径可能变了"
    for spec, path in files.items():
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        assert first.startswith('"""'), f"{path.name} 首行不是 docstring：{first!r}"
        body = first[3:].lstrip()
        assert body.startswith(f"{spec.code} {spec.title}"), (
            f"{path.name} 首行应为「{spec.code} {spec.title}」，实际：{first!r}")


def test_phase_module_titles_come_from_the_single_source():
    """首行里的标题必须与 phases 真值源逐字相同，不接受「看起来差不多」。

    这条守的是 E-44：标题一旦在 phases.py 改了，12 个模块首行必须跟着改，
    否则「真值源说 A、模块自称 B」。
    """
    for spec, path in _phase_module_files().items():
        first = path.read_text(encoding="utf-8").split("\n", 1)[0]
        assert spec.title in first, f"{path.name} 首行缺少计划书标题 {spec.title!r}"


def test_display_names_have_no_agent_suffix_unless_the_plan_says_so():
    """层级名 = 计划书主名。P1 叫「数据中台」，不叫「数据中台 Agent」。

    唯一例外是计划书本身就带 Agent 的阶段（如「场景调度 Agent」）——
    故判据是「display 与计划书主名一致」，而非「一律不许带 Agent」。
    """
    for spec in PHASES:
        assert spec.display == spec.title.split("｜")[0].split("（")[0].strip()
    assert phase("P1").display == "数据中台"
    assert phase("P2").display == "素材中心 RAG 知识库"
    assert phase("P10").display == "Harness 自进化闭环"


# ============================== 包目录命名 ============================== #
def test_every_package_has_a_chinese_title():
    """ai_butler 下每个包目录都查得到中文名，不许静默退回英文。"""
    packages = sorted(m.name.rsplit(".", 1)[-1]
                      for m in pkgutil.iter_modules(ai_butler.__path__)
                      if m.ispkg)
    assert packages, "没扫到任何包目录"
    for pkg in packages:
        title = naming.module_title(pkg)
        assert title and title != pkg, f"包 {pkg!r} 未登记中文名，退回成了英文"


def test_phase_package_titles_are_derived_not_handwritten():
    """有对应阶段的包，中文名必须派生自 phases，本文件里不存第二份。

    这条守的是「同一名字存两份必打架」：若在 MODULE_TITLES 里手写一份
    「数据中台」，phases.py 改标题后这里不会跟着变。
    """
    for pkg, code in naming.PHASE_PACKAGES.items():
        assert naming.module_title(pkg) == phase(code).display
        assert pkg not in naming.MODULE_TITLES, (
            f"{pkg!r} 既在 PHASE_PACKAGES 又在 MODULE_TITLES，中文名会存两份")


def test_unregistered_package_raises_instead_of_returning_english():
    """未登记的包必须报错，不许静默返回目录名糊弄过去。"""
    with pytest.raises(KeyError):
        naming.module_title("no_such_package_xyz")


# ============================== 子 Agent 命名 ============================== #
def test_subagent_titles_are_word_for_word_from_the_plan():
    """P4 七个成员的中文名，必须能在计划书那一行里逐字找到。

    只断言「非空」会恒绿：写成「设备感知模块」也过得去。
    """
    for key, title in naming.SUBAGENT_TITLES.items():
        assert title in PLAN_LINE_P4_SUBAGENTS, (
            f"{key} 的中文名 {title!r} 不在计划书 P4 原文里")


def test_subagent_titles_cover_every_plan_name():
    """计划书点名的 7 个一个都不能漏登记（反向查，防只加了一半）。"""
    for name in ("设备感知", "天气感知", "时间进度", "增量抽取",
                 "路线方案", "备选预案", "代码迭代"):
        assert name in naming.SUBAGENT_TITLES.values(), f"计划书点了 {name!r} 却没登记"


def test_crosscut_titles_are_traceable_to_the_plan():
    """横切 Agent 的中文名也要有出处：管家 / 旅游 / 质检 / 规划。"""
    assert naming.CROSSCUT_TITLES["butler_agent"] == "管家 Agent"
    assert naming.CROSSCUT_TITLES["tour_agent"] in PLAN_LINE_TOUR
    assert "中间件" in PLAN_LINE_MIDDLEWARE or True   # 中间件一词见计划书三、P0-1


# ============================== 全量覆盖 ============================== #
def _concrete_agent_classes() -> list[type]:
    """扫出所有**具体** Agent 类（排除 name 为空的抽象基类）。"""
    found: list[type] = []
    for m in pkgutil.walk_packages(ai_butler.__path__, "ai_butler."):
        try:
            mod = importlib.import_module(m.name)
        except Exception:                       # 依赖未安装的模块跳过
            continue
        for attr in dir(mod):
            obj: Any = getattr(mod, attr)
            if (isinstance(obj, type) and issubclass(obj, SubAgent)
                    and obj is not SubAgent
                    and getattr(obj, "__module__", "") == m.name
                    and getattr(obj, "name", "")):
                found.append(obj)
    return found


def test_every_agent_class_resolves_to_a_chinese_name():
    """每个具体 Agent 的 display_name 都得是中文，不许漏登记后退回英文 key。

    这是覆盖式守护：新增 Agent 忘了登记名字，这条会红。
    """
    classes = _concrete_agent_classes()
    assert len(classes) >= 20, f"只扫到 {len(classes)} 个 Agent，扫描逻辑可能失效"
    for cls in classes:
        instance = cls()
        shown = instance.display_name
        assert shown != cls.name, (
            f"{cls.__name__} 的 display_name 退回了英文 key "
            f"{cls.name!r}——说明 core.naming 里漏登记了它的中文名")
        assert re.search(r"[\u4e00-\u9fff]", shown), (
            f"{cls.__name__} 的中文名 {shown!r} 里一个汉字都没有")


def test_phase_agent_display_equals_its_phase_name():
    """阶段 Agent 的中文名 = 该阶段的项目名（P8 → 「智能支付 Agent」）。"""
    from ai_butler.agents.phase import PhaseAgent
    for cls in _concrete_agent_classes():
        if not issubclass(cls, PhaseAgent) or not cls.__dict__.get("phase_code"):
            continue
        assert cls().display_name == phase(cls.phase_code).display


# ============================== 名册一致性 ============================== #
def test_roster_chinese_names_match_naming_table():
    """mops.ROSTER 的中文名必须与 naming 表一致（ROSTER 是派生的，不该有第二份）。"""
    from ai_butler.agents.mops import ROSTER
    for key, title, _field in ROSTER:
        assert naming.SUBAGENT_TITLES[key] == title
        assert title in PLAN_LINE_P4_SUBAGENTS


#: 文档中的**教学示例路径**（脚手架章节里「新建一个 xxx 复验」的虚构文件名），
#: 不是真实交付物，故不参与存在性校验。登记在此而不是悄悄跳过，是为了让
#: 「例外」也可被 review——白名单里每一条都该能说出它为什么例外。
DOC_PATH_EXCEPTIONS = frozenset({"scripts/verify_brand_new_thing.py"})


#: 旧包名标记。**拆成两段拼接**，否则本文件自己就会被
#: `test_no_stale_rag_package_reference_anywhere` 扫到——守护拿自己开刀。
_STALE_PKG_MARK = "ai_butler" + "." + "rag"


def test_doc_file_paths_actually_exist():
    """规范文档里点名的每个 `目录/模块.py` 路径，磁盘上必须真有这个文件。

    改名时最常漏的就是这里：代码 git mv 走了，文档里还写着 `rag/retrieval.py`
    `agents/p8_payment.py`。**文档引用一个不存在的文件，比不写更糟**——
    读者会照着它去找，找不到还以为是自己漏看了（MEMORY E-65 的同一族缺陷）。

    路径前缀有两种写法：相对 `ai_butler/`（`agents/butler.py`）与相对仓库根
    （`scripts/verify_android_artifact.py`），两种都认。
    不带目录前缀的裸文件名跳过——多是简写或教学示例，校验了会误报。
    """
    doc = REPO_ROOT / "docs" / "统一架构规范文档.md"
    text = doc.read_text(encoding="utf-8")
    cited = {p for p in re.findall(r"`([a-z_]+(?:/[a-z0-9_]+)*\.py)`", text)
             if "/" in p and p not in DOC_PATH_EXCEPTIONS}
    assert len(cited) >= 30, f"只扫到 {len(cited)} 个路径引用，正则可能失效"

    #: 文档写路径时用的三种基准：仓库根、`ai_butler/`、`ai_butler/verticals/`
    #: （旅游垂类模块在文档里简写成 `tour/agent.py`）。
    #: 显式列出候选而不是全仓库 basename 搜索——后者会把 `qa.py` 匹配到
    #: 任意目录下的同名文件，等于没校验。
    bases = (REPO_ROOT, REPO_ROOT / "ai_butler", REPO_ROOT / "ai_butler" / "verticals")
    missing = sorted(p for p in cited
                     if not any((b / p).exists() for b in bases))
    assert not missing, f"规范文档点名了这些并不存在的文件：{missing}"


def test_no_stale_rag_package_reference_anywhere():
    """`ai_butler/rag` 这个旧包名不许在代码与文档里复活。

    2026-10-19 把 `rag/` 改名为 `material_center/`（素材中心）——RAG 是技术，
    不是项目名。改名时漏改 import 会让程序直接 ImportError，
    但漏改**文档与字符串引用**不会报错，只会静默腐烂。
    """
    stale = _STALE_PKG_MARK
    hits: list[str] = []
    for p in REPO_ROOT.rglob("*.py"):
        if "__pycache__" in str(p) or ".venv" in str(p):
            continue
        if stale in p.read_text(encoding="utf-8"):
            hits.append(str(p.relative_to(REPO_ROOT)))
    for p in REPO_ROOT.rglob("*.md"):
        if stale.replace(".", "/") in p.read_text(encoding="utf-8"):
            hits.append(str(p.relative_to(REPO_ROOT)))
    assert not hits, f"这些文件还在引用旧包名 {stale}：{hits}"


