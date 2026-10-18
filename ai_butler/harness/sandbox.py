"""P10 沙箱执行（裁定 U34）。

计划书依据
----------
- 三、P10 步骤4「对接 AI Infra 隔离沙箱，支持代码修改、命令执行、单元测试；
  **单元测试包含用户隔离、Jev、Agent-Wiki 本体校验用例**」
- 三、P10 触发时机「7×24 小时后台离线自动运行，**不占用业务算力**」

裁定 U34
--------
沙箱**是 AI Infra 的能力**（P0-1 已定义 edge/internal/sandbox 三层网络），
P10 只以**端口**对接，不自建隔离机制。本机用进程级受限适配器验证。

**执行前仍必须过 Hook** —— 沙箱控制的是「谁来执行」，不是「假定安全」。
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

#: 允许在沙箱内执行的命令白名单（**未列入即拒绝**）
ALLOWED_COMMANDS: tuple[str, ...] = (
    "python", "python3", "pytest", "git", "rg", "ls", "cat", "echo",
)

#: 默认超时（秒；实现参数）
DEFAULT_TIMEOUT = 120

#: 计划书点名要包含的三类校验用例
REQUIRED_TEST_KINDS: tuple[str, ...] = ("user_isolation", "jev", "agent_wiki_ontology")


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxResult:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict[str, Any]:
        return {"command": self.command, "exit_code": self.exit_code,
                "stdout": self.stdout, "stderr": self.stderr,
                "timed_out": self.timed_out, "ok": self.ok}


class SandboxPort(Protocol):
    def run(self, command: str, *, workdir: str | None = None,
            timeout: int = DEFAULT_TIMEOUT) -> SandboxResult: ...


class ProcessSandbox:
    """本机（进程级）受限适配器。

    限制：命令白名单、超时、工作目录限定。
    **没有容器级隔离** —— 它在部署环境由 AI Infra 提供，这里不假装有。
    """

    def __init__(self, *, root: str, allowed: Sequence[str] = ALLOWED_COMMANDS,
                 timeout: int = DEFAULT_TIMEOUT) -> None:
        self.root = root
        self.allowed = tuple(allowed)
        self.timeout = timeout

    def check_command(self, command: str) -> str:
        parts = shlex.split(command)
        if not parts:
            raise SandboxError("空命令")
        if parts[0] not in self.allowed:
            raise SandboxError(
                f"命令 {parts[0]!r} 不在沙箱白名单 {list(self.allowed)} 内，拒绝执行")
        return parts[0]

    def run(self, command: str, *, workdir: str | None = None,
            timeout: int | None = None) -> SandboxResult:
        self.check_command(command)
        try:
            done = subprocess.run(command, shell=True, cwd=workdir or self.root,
                                  capture_output=True, text=True,
                                  timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            return SandboxResult(command, -1, timed_out=True)
        return SandboxResult(command, done.returncode,
                             (done.stdout or "")[-4000:], (done.stderr or "")[-4000:])


class FakeSandbox:
    """测试用：记录命令，返回预设结果。"""

    def __init__(self, results: Sequence[SandboxResult] | None = None) -> None:
        self.commands: list[str] = []
        self._results = list(results or [])
        self.default = SandboxResult("(fake)", 0, stdout="ok")

    def run(self, command: str, *, workdir: str | None = None,
            timeout: int = DEFAULT_TIMEOUT) -> SandboxResult:
        self.commands.append(command)
        if self._results:
            return self._results.pop(0)
        # **回显真实命令**：返回固定串会让「沙箱到底跑了什么」无法断言
        return SandboxResult(command, 0, stdout="ok")


@dataclass(frozen=True)
class TestPlan:
    """沙箱内要跑的校验用例（计划书点名三类必须包含）。"""

    #: 告诉 pytest 这不是测试类（名字以 Test 开头会被误收集）
    __test__ = False

    kinds: tuple[str, ...] = REQUIRED_TEST_KINDS
    node_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        missing = [k for k in REQUIRED_TEST_KINDS if k not in self.kinds]
        if missing:
            raise SandboxError(
                f"沙箱自测缺少计划书点名要求的用例类别：{missing}")

    def command(self) -> str:
        if self.node_ids:
            return "pytest -q " + " ".join(self.node_ids)
        return "pytest -q"
