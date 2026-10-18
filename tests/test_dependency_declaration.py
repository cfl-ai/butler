"""依赖声明漂移防护。

计划书依据：三、P0-2「CI 自动构建、自动部署流水线」

为什么需要这个测试
------------------
P2/P3 引入了 chromadb / jieba / rank-bm25 / langgraph，但 requirements.txt 当时
没同步 —— 单元测试在本机全绿，CI 却会在 pip install 之后直接 ImportError。
这类漂移**不会在本地暴露**，必须由测试盯住。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ai_butler"

#: 导入名 → PyPI 发行包名（仅列与导入名不一致的）
DISTRIBUTION_ALIASES: dict[str, str] = {
    "pydantic_settings": "pydantic-settings",
    "jwt": "pyjwt",
    "frontmatter": "python-frontmatter",
    "yaml": "pyyaml",
    "sentence_transformers": "sentence-transformers",
    "rank_bm25": "rank-bm25",
}

#: 必须放在 requirements-ml.txt（重型、延迟导入、CI 不装）的包
ML_ONLY: frozenset[str] = frozenset({"sentence-transformers", "torch", "transformers"})


def _declared(filename: str) -> set[str]:
    path = ROOT / filename
    assert path.is_file(), f"缺少依赖声明文件 {filename}"
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        out.add(re.split(r"[<>=!\[; ]", line)[0].strip().lower())
    return out


def _third_party_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                       # 相对导入（包内）
                    continue
                names = [node.module.split(".")[0]] if node.module else []
            else:
                continue
            for name in names:
                if name in sys.stdlib_module_names or name == "ai_butler":
                    continue
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def _distribution(module: str) -> str:
    return DISTRIBUTION_ALIASES.get(module, module.replace("_", "-")).lower()


def test_declared_files_exist_and_are_not_empty():
    assert _declared("requirements.txt")
    assert _declared("requirements-ml.txt")


def test_every_third_party_import_is_declared():
    """凡代码里 import 的第三方包，必须在两个依赖文件中声明其一。"""
    declared = _declared("requirements.txt") | _declared("requirements-ml.txt")
    problems = []
    for module, files in sorted(_third_party_imports().items()):
        dist = _distribution(module)
        if dist not in declared and module.lower() not in declared:
            problems.append(f"{module} (发行包 {dist}) 被 {', '.join(sorted(files))} 导入")
    assert not problems, "以下第三方依赖未在任何依赖文件中声明：\n  " + "\n  ".join(problems)


def test_heavy_ml_stack_stays_out_of_the_ci_requirements():
    """torch 系依赖约 2GB：必须在 requirements-ml.txt，不得污染 CI 安装。"""
    ci = _declared("requirements.txt")
    ml = _declared("requirements-ml.txt")
    assert ML_ONLY <= ml, f"requirements-ml.txt 缺少 {sorted(ML_ONLY - ml)}"
    assert not (ML_ONLY & ci), f"requirements.txt 不应包含重型依赖 {sorted(ML_ONLY & ci)}"


def test_ml_dependencies_are_imported_lazily():
    """重依赖必须延迟导入，否则不装 torch 的环境连模块都导不进来。"""
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 只看模块顶层语句（函数体内的 import 即延迟导入）
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if _distribution(name) in ML_ONLY:
                    offenders.append(f"{path.relative_to(ROOT)} 顶层导入了 {name}")
    assert not offenders, "重型依赖必须放在函数内延迟导入：\n  " + "\n  ".join(offenders)


def test_ci_requirements_cover_the_rag_and_scheduler_stack():
    """回归锁定：P2/P3 引入的包曾漏声明，导致 CI 会在 pip install 后 ImportError。"""
    declared = _declared("requirements.txt")
    for pkg in ("chromadb", "jieba", "rank-bm25", "langgraph"):
        assert pkg in declared, f"CI 依赖缺少 {pkg}（P2/P3 引入）"
