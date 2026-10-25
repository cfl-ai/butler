"""跨进程的账本互斥（U86）。

这把锁防的是什么
----------------
不是「两个进程把字节写串」，而是**两个写者各自基于同一份旧快照判断可以写**。

`RevisionLedger.record()` 的查重读的是进程内已加载的记录：两个进程各自
`load()` 后都只看到 `{R1}`，各自认为 R2 可用，各自 append 一行 R2。
字节一行都没交错，账本里却多了一条重号记录——而 `get()` 只返回第一条，
第二条成了**文件里有、谁读都读不到**的影子账（U82 在内存层见过同一症状）。
`RepairTrail.append()` 同理。

所以锁必须跨越「读 → 判 → 写」整段：

    with lock():
        state = load()      # 锁内重读，拿别人刚写的
        check(state)        # 基于最新状态判断
        append(...)         # 才落盘

只包住 write 的锁不解决这个问题，只会让它更难复现——窗口变窄了，
但「恰好撞上」的时候人会更倾向相信自己没看错。

为什么用操作系统锁，而不是「O_EXCL 建一个标记文件」
--------------------------------------------------
标记文件的核心缺陷是**没有自动释放**：持有者崩溃（断电、被 kill -9）后
文件留在那里，后来者永远等不到。于是每个用它的人都得再实现一套
“陈旧锁判定”（看时间戳、看 pid 还在不在、几秒算超时）——那套判定本身就是
新的竞态来源，而且判错的后果是「以为锁坏了，手工删掉，然后两个写者同时进来」。

`flock` / `msvcrt.locking` 绑在打开的文件描述符上，**进程一死内核就释放**。
没有陈旧锁，就没有「要不要手工清锁」这个决定。

边界（必须一起读）
------------------
这把锁只在**同机 + 本地文件系统**上成立：

- NFS / SMB：语义由服务端实现决定，NFSv3 上 `flock` 不可靠；
- 多机共用一份账本：需要分布式锁（Redis / DB 行锁 / 带租约的协调服务），
  不在本模块能力内；
- 容器跨挂载点：两个容器看到的是两份不同的 inode，锁互不可见。

**默认不启用**（`RevisionLedger.lock=None` 且 store 不提供锁时是空实现）。
这不是偷懒：给单实例部署装一把只挡同机并发的锁，会让后来人以为
「并发问题已经解决」，而真正的多机重复提交照样发生——**假保护比没保护更危险**，
没保护会立刻炸，假保护会安静地错。判据是「出现第二个写者」：
同机第二个 worker 进程、多机共用账本、或者后台 job 与人工撤销按钮同时在跑
（最后一种最可能先出现）。
"""

from __future__ import annotations

import os
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "FileLock",
    "LedgerLockTimeout",
    "NullLock",
    "null_lock",
]

if os.name == "nt":                       # pragma: no cover —— 平台分支
    import msvcrt

    def _try_acquire(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _release(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:                                     # pragma: no cover —— 平台分支
    import fcntl

    def _try_acquire(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


#: 持有者信息写在这个偏移**之后**，刻意避开被锁定的 `[0, 1)`。
#:
#: Windows 的 `msvcrt.locking` 是区间锁：锁住字节 0 之后，其他句柄连**读**
#: 那一个字节都会 `PermissionError`。诊断信息如果写在锁区里，就变成
#: 「锁着的时候谁都读不到」——而它唯一的用途恰恰是在锁着的时候被读到。
#: 留 8 字节空洞好像浪费，换来的是「超时那一刻能说清谁在写」。
#: POSIX 的 `flock` 是整文件锁，本来不挡读；用同一套偏移让两个平台一个实现。
_HOLDER_OFFSET = 8


class LedgerLockTimeout(RuntimeError):
    """在超时内没拿到锁。**不是「锁坏了」，是有人正在写。**

    故意不提供「强制夺锁」的入口：夺锁意味着同时有两个写者，
    而那正是这把锁存在的理由。
    """


@dataclass
class NullLock(AbstractContextManager["NullLock"]):
    """空实现。单实例场景的默认值——**如实表示「没有互斥」**，不假装。

    之所以保留这个类型而不是用 `None`：调用方可以无条件 `with`，
    不必在业务代码里散落 `if lock is not None`（E-44 的分支温床）。
    """

    def __enter__(self) -> "NullLock":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


_NULL = NullLock()


def null_lock() -> NullLock:
    return _NULL


@dataclass
class FileLock(AbstractContextManager["FileLock"]):
    """排他锁，落在独立文件上（`<账本>.lock`）。

    为什么不锁数据文件本身：数据文件以追加模式打开，各平台下
    「当前写位置」与锁区间的耦合不一致；独立锁文件的语义只有「谁在里面」，
    与账本内容无关，也不会因为账本被外部工具改写而失效。

    `purpose` 只进诊断信息——超时时告诉人「另一个持锁者在干什么」。
    拿不到这个信息时，人只能看到「等了 5 秒」，然后开始怀疑锁坏了。
    """

    path: str | Path
    timeout: float = 5.0
    poll: float = 0.02
    purpose: str = ""
    _fd: int | None = field(default=None, init=False, repr=False)

    def __enter__(self) -> "FileLock":
        if self._fd is not None:
            raise RuntimeError(
                f"{self.path} 已被本对象持有——FileLock 不可重入。"
                f"需要嵌套持有时把锁往外提一层")
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + max(0.0, float(self.timeout))
        while True:
            try:
                _try_acquire(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = _read_holder(target)
                    os.close(fd)
                    raise LedgerLockTimeout(
                        f"等 {self.timeout:g} 秒仍未拿到 {target.name} 的锁；"
                        f"持锁者：{holder or '未知（可能刚释放）'}。"
                        f"这把锁绑在持有者的进程上，**它一退出系统就会自动释放**，"
                        f"所以不需要删锁文件——要么等它写完，要么查那个进程为什么卡住。"
                        f"若确认是两台机器在用同一份账本，那不是文件锁能解决的，"
                        f"要换成分布式锁（见 core/filelock.py 的边界一节）") from None
                time.sleep(self.poll)
        self._fd = fd
        _write_holder(fd, self.purpose)
        return self

    def __exit__(self, *exc: object) -> bool:
        fd, self._fd = self._fd, None
        if fd is None:
            return False
        try:
            _clear_holder(fd)
            _release(fd)
        finally:
            os.close(fd)
        return False


def _write_holder(fd: int, purpose: str) -> None:
    """把「谁在写」写进锁文件。只用于诊断，**失败不影响持锁**。"""
    stamp = datetime.now().isoformat(timespec="seconds")
    text = f"pid {os.getpid()} 于 {stamp} 持有" + (f"（{purpose}）" if purpose else "")
    data = text.encode("utf-8")
    try:
        os.lseek(fd, _HOLDER_OFFSET, os.SEEK_SET)
        os.write(fd, data)
        os.ftruncate(fd, _HOLDER_OFFSET + len(data))
    except OSError:                       # 诊断信息写不进去，锁照旧有效
        pass


def _clear_holder(fd: int) -> None:
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass


def _read_holder(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(_HOLDER_OFFSET)
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", "replace").strip("\x00").strip()
