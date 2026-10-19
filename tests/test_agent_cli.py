"""Agent 命令行入口的守护 —— `python ai_butler/agents/<模块>.py` 必须能跑、必须有输出。

背景（U52）
--------
此前各 Agent 模块只有类定义、没有 `__main__` 入口，PyCharm 右键运行会
**静默退出**：退出代码 0、零输出。开发者看到「进程已结束」的第一反应是
文件坏了或者项目没实现 —— **能跑但不说话，比不能跑更糟**，因为它伪装成正常。

守护分两层，缺一层都会漏：
1. **静态**：所有定义了具体 Agent 类的模块都必须有 `__main__` 入口。
   只靠运行时实测会漏掉「新增模块忘了加」——新增文件当天没人右键跑它。
2. **实测**：抽代表模块真的 subprocess 跑一遍，断言退出码 0 且有中文名。
   静态过了不代表跑得起来（import 顺序、sys.path 引导位置都会坑）。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "ai_butler" / "agents"

#: 基类模块：只定义抽象基类（SubAgent / PhaseAgent），运行它们没有意义
BASE_MODULES = frozenset({"base.py", "phase.py"})

#: 实测样本：阶段 Agent / 管家 / 集群 / 横切各取一个，全跑一遍太慢
RUNTIME_SAMPLES = (
    "p1_data_platform.py",   # 阶段 Agent
    "butler.py",             # 管家（非 SubAgent 子类）
    "mops.py",               # 集群名册
    "planner.py",            # 横切 Agent
    "p10_harness.py",        # 最后一个阶段
)

PY = sys.executable


def _agent_module_files() -> list[Path]:
    """定义了具体 Agent 类的模块文件（按文件名排序，保证断言稳定）。"""
    found = []
    for path in sorted(AGENTS_DIR.glob("*.py")):
        if path.name in BASE_MODULES or path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name.startswith("_"):
                continue
            bases = {ast.unparse(b) for b in node.bases}
            if bases & {"SubAgent", "PhaseAgent", "_MopsAgent"} or node.name.endswith("Agent"):
                found.append(path)
                break
    return found


def _has_main_guard(path: Path) -> bool:
    """AST 判定：文件里有没有 `if __name__ == "__main__":` 且调用了 cli.main。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        # 条件是 __name__ == "__main__"
        test = ast.unparse(node.test)
        if "__name__" not in test or "__main__" not in test:
            continue
        body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
        if "main(" in body:
            return True
    return False


def _has_syspath_bootstrap(path: Path) -> bool:
    """AST 判定：有没有在**业务 import 之前**把项目根补进 sys.path 的引导。

    位置很关键：放在文件末尾的兜底救不了模块顶部的 import ——
    `python xxx.py` 时 `import ai_butler` 在头部就先失败了。

    `from __future__` 例外：它按语言规范必须位于文件最前，引导不可能排在它前面，
    所以从它之后开始算。除此之外，引导必须早于任何 import。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return False        # 碰到第一条业务 import 还没出现引导，就是位置错了
        if isinstance(node, ast.If) and "__package__" in ast.unparse(node.test):
            return True
    return False


# ============================== 静态守护 ============================== #

def test_every_agent_module_has_main_entry():
    """定义了具体 Agent 类的模块，一个都不许没有命令行入口。"""
    files = _agent_module_files()
    assert len(files) >= 10, f"只扫到 {len(files)} 个 Agent 模块，扫描逻辑可能失效"
    missing = [p.name for p in files if not _has_main_guard(p)]
    assert not missing, f"这些 Agent 模块没有 __main__ 入口，右键运行会静默退出：{missing}"


def test_every_agent_module_has_syspath_bootstrap():
    """入口之外还要有 sys.path 引导，且必须在 import 之前。"""
    files = _agent_module_files()
    bad = [p.name for p in files if not _has_syspath_bootstrap(p)]
    assert not bad, (
        f"这些模块缺 sys.path 引导（或位置在 import 之后）：{bad} —— "
        f"命令行直接运行会 ModuleNotFoundError: No module named 'ai_butler'")


def test_base_modules_are_excluded_on_purpose():
    """基类模块不该有入口——防止将来有人把排除名单当成漏加。"""
    for name in BASE_MODULES:
        assert not _has_main_guard(AGENTS_DIR / name), (
            f"{name} 是基类模块，加了命令行入口反而误导；"
            f"若它已变成具体 Agent，请从 BASE_MODULES 移除")


# ============================== 实测守护 ============================== #

@pytest.mark.parametrize("module", RUNTIME_SAMPLES)
def test_direct_run_prints_profile_and_exits_zero(module: str):
    """直接运行必须：退出码 0 + 有输出 + 输出里带中文名。

    这一条守的是用户实际遇到的问题：静默退出。
    退出码 0 而零输出是**最坏的失败形态**——它看起来像成功。
    """
    proc = subprocess.run(
        [PY, str(AGENTS_DIR / module)],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        f"{module} 直接运行失败：\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    assert proc.stdout.strip(), f"{module} 直接运行零输出（静默退出）"
    assert "中文名" in proc.stdout, f"{module} 的档案没有中文名，只有类名等于没说"
    assert "不碰数据库" in proc.stdout, f"{module} 没有声明自己不碰外部依赖"


def test_direct_run_works_via_dash_m():
    """`python -m ai_butler.agents.<模块>` 也要能跑（另一种常见跑法）。"""
    proc = subprocess.run(
        [PY, "-m", "ai_butler.agents.p1_data_platform"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    assert "数据中台" in proc.stdout


def test_partial_identity_is_rejected():
    """只给 --user-id 不给 --session-id 必须报错，不许默认填个假值。

    没有用户上下文的写入无法做行级隔离（计划书三、P0-1）——
    命令行不能成为绕过隔离的捷径（U48 的边界在命令行的延续）。

    判据必须是 cli **自己**的用法错误「必须同时给出」：下游
    `StandaloneContext` 也会因缺 session_id 抛错，但那条错误信息里
    同样含 session_id 字样 —— 只断言含 session_id 会分不清是谁拦的，
    cli 层悄悄放行、让下游兜底的守护等于没守（E-27 同族：判据抓不住真问题）。
    """
    proc = subprocess.run(
        [PY, str(AGENTS_DIR / "p1_data_platform.py"), "--user-id", "u1"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0, "缺 session_id 也放行了，等于允许无审计的写入"
    assert "必须同时给出" in (proc.stdout + proc.stderr), (
        "放行者不是 cli 的用法校验，而是下游兜底 —— cli 层已经悄悄放行了")


def test_no_identity_prints_profile_without_touching_db():
    """不带身份信息时只打印档案，不尝试连数据库。

    判据：连一个不存在的库也能退出码 0 —— 档案路径只读 ClassVar，
    没有任何存储/网络调用。这也是「右键运行必有输出」的前提。
    """
    import os

    env = dict(os.environ)
    env["AIBUTLER_ENV"] = "nonexistent-env-file"   # 指向不存在的配置文件
    proc = subprocess.run(
        [PY, str(AGENTS_DIR / "p1_data_platform.py")],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        cwd=str(REPO_ROOT), env=env,
    )
    assert proc.returncode == 0, (
        f"档案打印居然依赖外部配置/数据库：\n{proc.stdout}\n{proc.stderr}")


def test_cluster_module_refuses_business_run():
    """集群模块（mops.py）不接受 --user-id 跑业务，成员由 P4 统一调度。"""
    proc = subprocess.run(
        [PY, str(AGENTS_DIR / "mops.py"),
         "--user-id", "u1", "--session-id", "s1"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode != 0
    assert "p4_mops" in (proc.stdout + proc.stderr)
