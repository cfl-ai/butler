"""仓库卫生守护测试（计划书三、P0-2「制定 Git 分支规范、CI 自动构建」）。

为什么要有它
------------
架构规范第 12 章把分支模型写得很完整，`.github/workflows/ci.yml` 也早就就位，
但**仓库本身从未 `git init`** —— 规范和 CI 是「写了却没接上」的状态。
这是本项目反复出现的同一个毛病的又一例（MEMORY E-21 / E-29 / E-33）。

本文件守的不是「文件在不在」，而是**规则是不是真的生效**：
文件此刻不存在，不等于 `.gitignore` 写了那条规则（MEMORY E-27）。
所以敏感文件那条用 `git check-ignore` 反向验规则，而不是只看有没有被跟踪。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "统一架构规范文档.md"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"

#: 含凭据的环境文件。计划书三、P0-1 要求隐私字段独立加密，
#: 这些文件无论如何都不能进版本库 —— **只允许 .env.example 入库**。
SECRET_ENV_FILES = (".env", ".env.dev", ".env.test", ".env.staging", ".env.prod")

#: 体积以 GB 计的本地产物，入库会把仓库撑爆且毫无意义。
BULKY_PATHS = (
    "models/bge-small-zh-v1.5/model.safetensors",
    "models/bge-reranker-base/model.safetensors",
    "mobile/build/app/outputs/flutter-apk/app-debug.apk",
    "mobile/.dart_tool/flutter_build/does-not-matter.bin",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )


def test_repository_is_initialised():
    """仓库必须真的存在 —— 规范与 CI 都在，唯独仓库缺席过一次。"""
    assert (ROOT / ".git").exists(), (
        "项目根缺少 .git：架构规范第 12 章与 .github/workflows/ci.yml 就位，"
        "但仓库从未初始化，CI 没有可运行的前提"
    )
    proc = _git("rev-parse", "--is-inside-work-tree")
    assert proc.returncode == 0 and proc.stdout.strip() == "true", (
        "git rev-parse 失败（git 不可用或不在仓库内）：" + proc.stderr.strip()
    )


def test_no_environment_file_is_tracked():
    """含凭据的 .env.* 一律不得入库；.env.example 是模板，必须在。"""
    proc = _git("ls-files")
    assert proc.returncode == 0, "git ls-files 失败：" + proc.stderr.strip()
    tracked = set(proc.stdout.splitlines())

    leaked = sorted(f for f in SECRET_ENV_FILES if f in tracked)
    assert not leaked, f"以下环境文件已被跟踪，含凭据会随仓库扩散：{leaked}"
    assert ".env.example" in tracked, ".env.example 是配置模板，必须入库"


def test_gitignore_rules_actually_work():
    """.gitignore 必须**真的**排除掉这些路径。

    只看「当前有没有被跟踪」是不够的：文件不存在时断言恒绿，
    而规则可能从来没写过。这里用 git check-ignore 直接问规则本身。
    """
    unchecked: list[str] = []
    for rel in (*SECRET_ENV_FILES, *BULKY_PATHS):
        proc = _git("check-ignore", "-q", rel)
        # check-ignore 命中返回 0，未命中返回 1
        if proc.returncode != 0:
            unchecked.append(rel)
    assert not unchecked, (
        "以下路径没有被 .gitignore 覆盖（规则无效或缺失，"
        "当前没出事只是因为文件恰好不存在）：\n  " + "\n  ".join(unchecked)
    )


def _spec_branches() -> list[str]:
    """从架构规范 12.1 的分支表里抽出分支名。"""
    text = SPEC_PATH.read_text(encoding="utf-8")
    section = text.split("### 12.1", 1)[1].split("### 12.2", 1)[0]
    return re.findall(r"^\|\s*`([^`]+)`\s*\|", section, re.M)


def _ci_trigger_branches() -> set[str]:
    ci = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
    on = ci.get("on") or ci.get(True)
    branches: set[str] = set()
    for event in ("push", "pull_request"):
        branches |= set((on.get(event) or {}).get("branches", []) or [])
    return branches


def test_spec_branch_model_matches_ci_triggers():
    """规范 12.1 的每一个分支，都必须在 CI 触发条件里有对应项。

    规范第 13 章写着「分支表与 CI 触发条件一致（`tests/test_ci_pipeline.py`）」，
    但那个文件**并没有这条断言** —— 又一处「写了却没接上」。这里补上。

    映射说明：CI 触发用 `**` 匹配含斜杠的分支名（GitHub Actions 与 Gitea 一致），
    故规范的 `feature/*` 对应 CI 的 `feature/**`。
    """
    spec = _spec_branches()
    assert spec, "未能从架构规范 12.1 解析出分支表（表格格式可能已变更）"

    triggers = _ci_trigger_branches()
    assert triggers, "未能从 ci.yml 解析出分支触发条件"

    def to_glob(branch: str) -> str:
        return branch.replace("/*", "/**") if branch.endswith("/*") else branch

    missing = [b for b in spec if to_glob(b) not in triggers]
    assert not missing, (
        "架构规范 12.1 定义了这些分支，但 ci.yml 不会为它们触发："
        f"{missing}\n规范分支={spec}\nCI 触发={sorted(triggers)}"
    )
