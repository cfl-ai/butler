"""临时目录自愈（测试与冒烟脚本共用）。

为什么需要
----------
某些沙箱模式下系统临时目录（~{C:/Users/<用户>/AppData/Local/Temp}~）**不可写**。
Python 会**静默降级**把临时目录落到当前目录，而 chromadb 的 rust 绑定要自己在临时目录建库 ——
建不了就报 ~{unable to open database file}~，**与业务代码毫无关系**，
却会让一整片 RAG 用例（以及端到端冒烟）集体失败。

一个容易写错的地方
------------------
~{tempfile.gettempdir()}~ 在该目录不可用时**会返回当前目录**，
所以「探一下 gettempdir() 能不能写」这种自愈逻辑**必然通过、等于没探**。
必须直接探**环境变量指向的路径**。（见 MEMORY E-14）
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: 工作区内的兜底临时目录名
FALLBACK_DIR_NAME = ".pytest_tmp"


def _is_writable(path: str) -> bool:
    probe = Path(path) / ".aibutler_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def ensure_writable_temp(root: str | Path | None = None) -> str:
    """确保临时目录可写；不可写就改用工作区内的目录。

    返回最终生效的临时目录路径。**必须在导入 chromadb 之前调用。**
    """
    for name in ("TMP", "TEMP", "TMPDIR"):
        value = os.environ.get(name)
        if value and _is_writable(value):
            return value

    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    fallback = base / FALLBACK_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(fallback)
    tempfile.tempdir = str(fallback)
    return str(fallback)
