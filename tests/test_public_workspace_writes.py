"""公共 Workspace 的写入白名单。

为什么要有它
------------
WikiStore.write_page(page, user_id=...) 里，**传不传 user_id 决定了写公共库还是私有 Vault**：

    write_page(page, user_id="u-1")   ->  vault/u-1/   只有本人可见
    write_page(page)                  ->  workspace/   所有人可见

于是「用户来源的内容被误写成公共页面」这件事**没有任何机制拦得住** ——
而公共库一旦被污染，所有用户都会看到那条虚假信息。这是最高价值的投毒目标。

本文件把「哪些代码允许写公共库」变成**显式清单**：
新增一处不带 user_id 的写入，若没在这里登记理由，测试立刻变红。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ai_butler"

#: 允许写公共 Workspace 的位置 -> 理由。
#: 判据是「内容是否来自用户」：系统自身的采集与归档可以写公共库，
#: **用户提交的内容一律不行**。
PUBLIC_WRITE_ALLOWLIST: dict[str, str] = {
    "ai_butler/rag/materials.py": (
        "系统采集流水线归档 SOURCE 页（把过期素材标为 archived）。"
        "内容是采集器按计划书三、P2 抓取并清洗过的，不来自用户输入。"
    ),
}


def _public_writes() -> dict[str, list[int]]:
    """扫描所有 write_page(...) 调用，返回**没有传 user_id 的**那些。"""
    found: dict[str, list[int]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "write_page":
                continue
            if any(kw.arg == "user_id" for kw in node.keywords):
                continue
            rel = path.relative_to(ROOT).as_posix()
            found.setdefault(rel, []).append(node.lineno)
    return found


def test_public_workspace_writes_are_explicitly_allowlisted():
    """只有白名单里的位置可以写公共 Workspace。

    这条防的是「以后新加一处 write_page(page)，把用户内容写进了公共库」——
    那种错误**不会让任何既有测试变红**，却会影响全部用户。
    """
    writes = _public_writes()
    unexpected = {
        path: lines for path, lines in writes.items()
        if path not in PUBLIC_WRITE_ALLOWLIST
    }
    assert not unexpected, (
        "以下位置在写公共 Workspace（没传 user_id），但未登记理由：\n  "
        + "\n  ".join(f"{p}: 第 {ls} 行" for p, ls in unexpected.items())
        + "\n\n若确属系统自身的采集/归档，请加入 PUBLIC_WRITE_ALLOWLIST 并写明理由；"
        "若内容来自用户，**必须传 user_id 写进私有 Vault**。"
    )


def test_allowlist_entries_still_have_public_writes():
    """白名单里的条目必须仍然真的存在 —— 改了代码却忘了删条目，会让人误以为风险还在。"""
    writes = _public_writes()
    stale = [p for p in PUBLIC_WRITE_ALLOWLIST if p not in writes]
    assert not stale, f"这些白名单条目已不再写公共库，应删除：{stale}"


def test_every_allowlist_entry_explains_itself():
    """每个豁免都要写清楚「为什么这条内容可以进公共库」。"""
    for path, reason in PUBLIC_WRITE_ALLOWLIST.items():
        assert len(reason) >= 20, f"{path} 的豁免理由过于简略"
        assert "用户" in reason, (
            f"{path} 的豁免理由没有说明与用户内容的关系 —— "
            "判据是「内容是否来自用户」，理由必须正面回答这一点"
        )
