"""md 产出白名单守护。

约束来源（用户明确要求）
------------------------
「md 产出仅限计划书要求的文档」「设计过程中所涵盖的 md 文档非计划书所要求的则不保存」。

允许的只有这几类：
  1. docs/统一架构规范文档.md
  2. docs/用户数据隔离与隐私开发规范.md
  3. docs/Jev决策模块开发规范.md
  4. docs/Agent-Wiki知识库开发规范.md
  5. MEMORY.md
  6. Agent-Wiki 页面族与编译产物（落在 settings.wiki_root 下的 .md）

为什么要有机检
--------------
这条约束**被静默破坏过一次**：flutter create 顺手在 mobile/ 下生成了一个
模板 README.md，没有任何测试发现它。约束只写在文档里就等于没有约束。

这个测试的作用不是「证明写对了」，而是**让多出来的 md 当场变红**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 明确允许的 md（相对项目根）
ALLOWED_FILES = {
    "MEMORY.md",
    "docs/统一架构规范文档.md",
    "docs/用户数据隔离与隐私开发规范.md",
    "docs/Jev决策模块开发规范.md",
    "docs/Agent-Wiki知识库开发规范.md",
}

#: Agent-Wiki 页面族与编译产物的落盘目录名（第三类允许项）
WIKI_DIR_MARKER = "agent_wiki"

#: 第三方 / 生成物目录，一律不参与检查
SKIP_DIRS = {
    ".git", ".venv", ".dart_tool", ".idea", ".pytest_tmp", ".pytest_cache",
    "node_modules", "build", "__pycache__", ".gradle", ".cxx",
}


def _iter_markdown() -> list[Path]:
    """在项目里找 md，**边走边剪枝**。

    不能写成先 rglob("*.md") 再过滤：项目里有 data/dev/pgdata（PostgreSQL 数据目录）
    与 mobile/build 这类文件极多的目录，先遍历再丢弃会白白走一遍几万个文件。
    本机可用内存本来就紧（约 2.25GB），这种无谓遍历是实打实的负担。
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 就地剪枝：os.walk 会照着 dirnames 继续往下走，
        # 把它改掉就等于整棵子树都不进了。
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".md"):
                found.append(Path(dirpath) / name)
    return sorted(found)


@pytest.fixture(scope="module")
def markdown_files() -> list[Path]:
    return _iter_markdown()


def test_every_markdown_is_whitelisted(markdown_files):
    """每一个 md 都必须落在白名单里，否则失败并指出多余的是哪个。"""
    offenders: list[str] = []
    for path in markdown_files:
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        if WIKI_DIR_MARKER in rel:
            continue          # Agent-Wiki 页面族与编译产物
        offenders.append(rel)

    assert not offenders, (
        "以下 md 不在计划书要求的文档范围内，按约束不应保存：\n  "
        + "\n  ".join(offenders)
        + "\n（若确属需要，先确认它是否属于白名单的六类；"
          "约束是「md 产出仅限计划书要求的文档」。）"
    )


def test_the_five_named_docs_still_exist(markdown_files):
    """白名单里的文档也必须**真的还在** —— 白名单不能靠删文件来变绿。"""
    present = {p.relative_to(ROOT).as_posix() for p in markdown_files}
    missing = sorted(ALLOWED_FILES - present)
    assert not missing, f"白名单文档缺失：{missing}"


def test_no_readme_left_behind_by_scaffolding(markdown_files):
    """工具脚手架（flutter create / npm init 之类）留下的 README 必须清掉。

    它们是模板样板文，既不是计划书要求的文档，也不属于任何一类允许项。
    """
    stray = [
        p.relative_to(ROOT).as_posix()
        for p in markdown_files
        if p.name.lower().startswith("readme")
    ]
    assert not stray, (
        "发现脚手架生成的 README（不在白名单内）：" + str(stray)
    )



def test_no_unsubstituted_placeholders_in_docs(markdown_files):
    """白名单文档里不得残留生成时的占位符。

    真事：本项目用 ~B~xxx~B~ 表示行内代码、~F~ 表示代码围栏（避免在生成脚本里
    与模板字符串冲突）。有一次替换漏了，于是**字面的 ~B~ 直接进了正式文档** ——
    两份文件里共 158 处，读者看到的不是代码格式而是一串波浪号加字母。

    这类残留不会让任何断言变红：文档照样「存在」，只是**排版坏了**。
    """
    import re

    pattern = re.compile(r'~[A-Z]~')
    offenders: list[str] = []
    for path in markdown_files:
        text = path.read_text(encoding='utf-8')
        hits = pattern.findall(text)
        if hits:
            from collections import Counter
            offenders.append(
                f"{path.relative_to(ROOT).as_posix()}: {len(hits)} 处 {dict(Counter(hits))}"
            )
    assert not offenders, (
        "以下文档残留未替换的占位符（应替换为反引号或代码围栏）：\n  "
        + "\n  ".join(offenders)
    )
