"""P0-1 测试夹具。

会话参数不再由测试覆盖：其取值已由人工授权并写入 .env
（授权日期 2026-10-16，理由见 app/core/config.py 的 PARAM_PROVENANCE）。
时间敏感的用例各自构造独立阈值，不依赖业务配置值。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env.test"   # 四环境隔离：测试只连 test 环境
os.environ["AIBUTLER_ENV"] = str(ENV_PATH)   # 强制，避免继承到别的环境


# --------------------------------------------------------------------------- #
# 临时目录自愈（与冒烟脚本共用同一个实现）
# --------------------------------------------------------------------------- #
# 系统 %TEMP% 不可写时，Python 会静默降级到当前目录，chromadb 的 rust 绑定
# 随之报「unable to open database file」—— 与业务代码无关，却会让整片 RAG 用例失败。
# 实现与原因见 ai_butler/devkit/tempdir.py 与 MEMORY E-14。
sys.path.insert(0, str(ROOT))
from ai_butler.devkit.tempdir import ensure_writable_temp  # noqa: E402

ensure_writable_temp(ROOT)


# --------------------------------------------------------------------------- #
# 模拟 CI 环境：屏蔽重型 ML 依赖
# --------------------------------------------------------------------------- #
# CI 只装 requirements.txt（不含约 2GB 的 torch 系依赖）。
# 设 AIBUTLER_BLOCK_ML=1 即可在本机复现 CI 的依赖面：
# 凡在模块顶层 import torch / sentence_transformers / transformers 的代码会当场暴露。
if os.environ.get("AIBUTLER_BLOCK_ML") == "1":       # pragma: no cover - 环境开关
    import sys
    from importlib.abc import MetaPathFinder

    _BLOCKED = {"torch", "sentence_transformers", "transformers"}

    class _BlockML(MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in _BLOCKED:
                raise ImportError(
                    f"[CI 模拟] {fullname} 不可用：CI 不安装 requirements-ml.txt。"
                    f"重型依赖必须延迟导入。")
            return None

    sys.meta_path.insert(0, _BlockML())


def _read_env() -> dict[str, str]:
    kv: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip()
    return kv


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    return _read_env()


@pytest.fixture(scope="session")
def settings():
    from ai_butler.core.config import load_settings

    return load_settings()


@pytest.fixture()
def redis_client():
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=True)


#: 清理失败的临时目录 —— 不再静默吞掉，由 session 结束时报出
_LEFTOVER_DIRS: list[str] = []


def pytest_sessionstart(session):
    """会话开始前，清掉上一次遗留的临时目录。

    这些目录是**上次运行**的残渣（进程被杀 / 文件句柄未释放）。
    留着只会越积越多，而它们对本次运行毫无用处。
    """
    import shutil

    base = ROOT / ".test-work"
    if not base.exists():
        return
    for child in base.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    """会话结束时报告清理失败的目录。

    不抛异常（那会把一次正常的测试运行判成失败），
    但**必须让人看见** —— 否则磁盘会被悄悄吃掉。
    """
    if _LEFTOVER_DIRS:
        print(
            f"\n[conftest] 警告：{len(_LEFTOVER_DIRS)} 个临时目录未能清理，"
            "通常是文件句柄未释放（chromadb / sqlite）。"
            "下次运行会在 sessionstart 时清掉。"
        )


@pytest.fixture()
def workdir():
    """临时工作目录。

    注意：**不能用 pytest 的 tmp_path / tmp_path_factory** —— 它们基于 tempfile.mkdtemp，
    而受限沙箱下 mkdtemp 生成的目录不可写也不可删（MEMORY.md E-03）。
    因此这里用 os.makedirs 语义自建，并在用例结束后清理。
    """
    import shutil
    import uuid

    base = ROOT / ".test-work" / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    yield base

    # 清理必须**真的**发生，且失败不能静默吞掉。
    # 真事：Windows 上 chromadb 仍占着 chroma.sqlite3，rmtree 删不掉，
    # 而 ignore_errors=True 把失败完全吞掉 —— 两天堆了 4577 个目录、823 MB。
    import gc

    gc.collect()                      # 先让打开的文件句柄有机会释放
    shutil.rmtree(base, ignore_errors=True)
    if base.exists():
        # 再重试一次；仍失败就**记下来**，由 session 结束时报出
        shutil.rmtree(base, ignore_errors=True)
    if base.exists():
        _LEFTOVER_DIRS.append(str(base))
