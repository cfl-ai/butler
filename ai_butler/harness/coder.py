"""P10 代码创作层 —— 让大模型写补丁，但**不给它改错的机会**。

计划书依据
----------
- 三、P10 步骤2「实现完整 Claude Code 风格 Agentic Loop 主循环」
- 三、P10 步骤1「搭建基础代码修改沙箱，支持代码修改」
- 三、P10 离线链路「**代码迭代 Agent 分析问题、生成优化方案** →
  Hook + 质检风险校验 → 沙箱执行代码修改与自测」

在这里的分工
------------
上一轮的闭环只到「变更单」，`diff` 恒为空。本模块补的是**谁写 diff**：

    评测中台发现的缺陷 → 本模块向大模型要一份补丁 → patch.py 机械校验
    → Hook 风险分级 → 沙箱应用并跑测试 → 评测中台复评

本模块**不做**校验、不落盘、不宣布成功。它只产出一份**候选补丁**，
能不能用由 `patch.PatchGuard` 与 `PatchApplier` 判。

三个刻意的设计
--------------
1. **给模型真实文件片段，不让它凭空写。**
   大模型对没见过的代码会「合理推测」出一个不存在的函数。
   推测出来的 diff 位置必然错，而错位的 diff 最难查——
   所以上下文里放的是磁盘上真实的行（带行号），并要求它逐字复制。

2. **失败信息回灌重试一次，然后放弃。**
   第一次失败多半是格式（围栏、缺 `---`），把**具体报错**告诉它就能改对；
   第二次还失败说明它没理解任务，继续重试只是烧钱和烧时间。
   **不设第三次**——这是有意的上限，不是省事。

3. **`ok=True` 只表示「格式与范围都过了」，不代表修好了。**
   `CodeProposal` 里没有任何「已修复」字段。修没修好只能由
   沙箱测试与评测复评回答，本模块不越权。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from ai_butler.harness.patch import (
    ALLOWED_SCOPE_BY_KIND,
    ParsedPatch,
    PatchError,
    PatchGuard,
    parse_unified_diff,
)

__all__ = [
    "CodeSnippet",
    "CodeProposal",
    "CodeWriter",
    "SnippetReader",
    "LLMCoder",
    "CODER_SYSTEM_PROMPT",
    "extract_diff",
]

#: 离线链路调用模型时使用的身份（不是真实用户）。
#:
#: 网关要求 `user_id`（P3 鉴权铁律：未登录不得执行）。离线链路没有登录用户，
#: 用固定系统身份而不是借用某个真实用户——借用会把离线话务算进那个人的配额，
#: 也让「谁在改代码」这件事变得不可追溯。
CODER_USER_ID = "system:p10-harness"

#: 单次给的源码行数上限（实现参数）。超了按关键字切片。
MAX_SNIPPET_LINES = 240
#: 切片时每个命中点前后保留的行数（实现参数）。
SLICE_CONTEXT = 45
#: 文件不超过这个行数就**整篇给**（实现参数）。
#:
#: 起因是一次真实失败：`planner.py` 529 行，切片只给了前 240 行，
#: 正好漏掉第 369 行的兜底分支。模型于是输出
#: `SKIP: 提供的源码片段在 PERSONAL_RECALL 信号处截断……无法在不臆造上下文
#: 的前提下产出可被接受的统一 diff`——它做对了，是我的上下文给少了。
#: 切片省下的 token，抵不过「模型看不到关键分支 → 弃权或猜错」的代价。
FULL_FILE_CAP = 700


class CoderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 上下文：真实文件片段
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CodeSnippet:
    """一段**真实存在于磁盘**的源码。带起始行号，方便模型写准 `@@`。"""

    path: str
    start_line: int
    text: str

    def render(self) -> str:
        numbered = "\n".join(
            f"{self.start_line + i:>5}  {line}"
            for i, line in enumerate(self.text.splitlines()))
        return f"=== FILE: {self.path} (从第 {self.start_line} 行开始) ===\n{numbered}"


@dataclass
class SnippetReader:
    """按路径读源码片段。**只读**，且只读白名单内的路径。"""

    root: Path
    max_lines: int = MAX_SNIPPET_LINES
    context: int = SLICE_CONTEXT
    #: 行数不超过它就整篇给（见 `FULL_FILE_CAP` 的说明）
    full_file_cap: int = FULL_FILE_CAP

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    def allowed(self, path: str, kind: str) -> bool:
        prefixes = ALLOWED_SCOPE_BY_KIND.get(kind, ())
        return any(path == p or path.startswith(p) for p in prefixes)

    def read(self, paths: Sequence[str], *, kind: str,
             keywords: Sequence[str] = ()) -> tuple[CodeSnippet, ...]:
        """读入允许范围内的文件。越界路径**静默跳过**（不是报错）。

        跳过而非报错：`target_hint` 是收敛提示，常混入非代码路径
        （「检索候选数与提示词长度」）。为一句提示而整轮失败不划算；
        真正越界的补丁由 `PatchGuard` 在下一个环节拦。
        """
        out: list[CodeSnippet] = []
        for rel in paths:
            if not self.allowed(rel, kind):
                continue
            target = (self.root / rel).resolve()
            try:
                target.relative_to(self.root)
            except ValueError:
                continue
            if not target.is_file():
                continue
            try:
                text = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            out.extend(self._slice(rel, text, keywords))
        return tuple(out)

    def _slice(self, rel: str, text: str,
               keywords: Sequence[str]) -> list[CodeSnippet]:
        lines = text.splitlines()
        # 小文件整篇给：模型能自己找到相关分支，比我们猜它要看哪里可靠
        if len(lines) <= self.full_file_cap:
            return [CodeSnippet(rel, 1, text)]

        hits: list[int] = []
        for i, line in enumerate(lines):
            if any(k and k in line for k in keywords):
                hits.append(i)
        if not hits:
            return [CodeSnippet(rel, 1, "\n".join(lines[:self.max_lines]))]

        windows: list[tuple[int, int]] = []
        for hit in hits:
            lo = max(0, hit - self.context)
            hi = min(len(lines), hit + self.context + 1)
            if windows and lo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
            else:
                windows.append((lo, hi))

        # 预算**均分**给各窗口。均分而非「先到先得」：关键分支常在文件后半段，
        # 让第一个窗口吃掉全部预算，等于仍然只给了开头。
        budget = self.max_lines
        share = max(20, budget // max(1, len(windows)))
        out: list[CodeSnippet] = []
        for lo, hi in windows:
            if budget <= 0:
                break
            take = min(hi, lo + min(share, budget))
            out.append(CodeSnippet(rel, lo + 1, "\n".join(lines[lo:take])))
            budget -= (take - lo)
        return out


# --------------------------------------------------------------------------- #
# 提案
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CodeProposal:
    """一次代码创作的结果。

    `ok=True` **只**表示：产出了一份格式合法、范围合规、**确有增删行**的补丁。
    它没有、也不会有「已修复」的字段——那是沙箱与复评的结论。

    `skipped=True` 是另一种合法结局：**模型主动判断不该改代码**。
    这不是失败，也不该被当成「产出了空补丁」。
    模型说「你给我的上下文不够，我不在臆造的前提下写 diff」，
    是这条链路里最值得奖励的行为，必须与「写了但写错」分开记录。
    """

    ok: bool
    diff: str = ""
    rationale: str = ""
    patches: ParsedPatch | None = None
    attempts: int = 0
    #: 模型主动弃权（输出 `SKIP: <原因>`），而非产出失败
    skipped: bool = False
    #: 最近一次的拒绝理由（范围违规 / 解析失败），供决策层与落账查看
    rejected: tuple[str, ...] = ()
    error: str = ""

    @property
    def paths(self) -> tuple[str, ...]:
        return self.patches.paths if self.patches else ()

    def as_dict(self, *, redactor: Any = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok, "skipped": self.skipped, "attempts": self.attempts,
            "paths": list(self.paths),
            "touched": self.patches.touched if self.patches else 0,
            "rationale": self.rationale or "",
            "rejected": list(self.rejected), "error": self.error,
        }
        if redactor is not None:
            payload = redactor.redact_deep(payload)
        return payload


class CodeWriter(Protocol):
    """代码创作端口。决策层可替换成任意实现（本地小模型、IDE 插件、人工填单）。"""

    async def propose(self, *, defect: Any, snippets: Sequence[CodeSnippet],
                      verify_target: str, feedback: str = "") -> str: ...


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
CODER_SYSTEM_PROMPT = """你是离线修复链路上的代码修改节点。你只输出统一 diff，不输出任何解释。

硬性约束（违反任意一条，整个补丁作废）：
1. 只输出一个 ```diff 代码块，里面是标准统一 diff。
   - 每个文件三段头：`--- a/<路径>`、`+++ b/<路径>`，然后是 `@@ -旧起始,行数 +新起始,行数 @@`
   - 上下文行以一个空格开头，删除行以 `-` 开头，新增行以 `+` 开头
2. **上下文行必须逐字复制**用户消息里给出的源码（去掉左侧行号与两个空格）。
   不要凭记忆或推测补全任何一行——行内对不上，补丁就会被拒绝。
3. 只允许修改用户消息中「允许修改范围」列出的文件。**不得新增文件**。
4. **不得修改测试文件、评测代码、权限与门禁代码**。删掉断言会让测试变绿而缺陷仍在，
   这类改动会被直接判定为作弊并作废。
5. 改动要最小：只改与缺陷直接相关的那几行。不要顺手重排、重命名、加注释、加日志。
6. 如果你判断缺陷不在代码里（例如是数据问题、配置问题、评测用例本身配错），
   输出 ```diff 空块并只用一行说明 `SKIP: <原因>`，**不要强行改代码**。
"""


_FENCE_RE = re.compile(r"```(?:diff|patch)?[ \t]*\r?\n(.*?)```", re.S)


def extract_diff(text: str) -> tuple[str, str]:
    """从模型输出里取出 diff 与一句理由。返回 `(diff, rationale)`。

    模型几乎不会干净地只输出 diff——常见是围栏、前言、末尾「以上」。
    这里做**最小必要**的提取，而不做「修复式解析」：
    提取失败就走重试，让模型自己改，比我们猜它的意图安全。
    """
    rationale = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SKIP:"):
            rationale = stripped
            break

    blocks = _FENCE_RE.findall(text)
    if blocks:
        diff = max(blocks, key=len).strip()
        return (diff, rationale)

    idx = text.find("--- ")
    if idx >= 0:
        return (text[idx:].strip(), rationale)
    return ("", rationale)


# --------------------------------------------------------------------------- #
# 默认实现
# --------------------------------------------------------------------------- #
@dataclass
class LLMCoder:
    """走 `LLMGateway` 的默认代码创作实现。

    `gateway` 是端口注入，不是在本模块里 new 一个——离线链路与在线链路
    必须共用同一套鉴权、限流、熔断（P0-1），另起一套等于绕过它们。

    `model` 显式指定创作用的模型（U89：本机入口按 `ollama_model` 回退
    `ollama_vision_model` 取值，与在线链路同一个表达式）。不给时由供应商
    按自己的默认走——而 Ollama 的默认在 `ollama_model` 为空串时会发出
    一个空模型名，那是「装配时没人决定用哪个模型」，不是供应商的默认。
    """

    gateway: Any
    rejector: PatchGuard | None = None
    #: 重试上限。**固定 1 次**：第二次失败说明它没理解任务，再试只是烧钱。
    max_attempts: int = 2
    max_tokens: int = 2048
    #: 创作类调用用非零温度：不同候选补丁之间需要有差异，否则重试等于复读
    temperature: float = 0.2
    #: 创作用的模型。None = 交给供应商默认（生产入口会显式给，见模块 docstring）。
    model: str | None = None

    async def propose(self, *, defect: Any, snippets: Sequence[CodeSnippet],
                      verify_target: str, feedback: str = "") -> CodeProposal:
        kind = getattr(getattr(defect, "kind", None), "value", "")
        guard = self.rejector or PatchGuard.for_kind(kind)
        allowed = guard.allowed_prefixes

        base = _render_user_prompt(defect=defect, snippets=snippets,
                                   allowed=allowed, verify_target=verify_target)
        rejected: list[str] = []
        error = ""
        last_diff = ""
        rationale = ""

        for attempt in range(1, self.max_attempts + 1):
            user = base
            if feedback:
                user += f"\n\n上次尝试被拒绝，原因如下（请修正后重发完整 diff）：\n{feedback}"
            if rejected:
                user += ("\n\n上一次的输出被校验拒绝：" + "；".join(rejected[-2:]))

            try:
                diff, rationale, text = await self._call(user)
            except Exception as exc:                     # noqa: BLE001
                # 模型不可用：如实返回失败，不编造补丁
                return CodeProposal(False, attempts=attempt,
                                    error=f"模型调用失败：{type(exc).__name__}: {exc}")

            last_diff = diff
            # 模型主动弃权：**这是正确行为，不是失败**。实录并返回，
            # 不重试——它已经说清了为什么不该改，再问一遍只会得到同样的答案。
            if rationale.upper().startswith("SKIP:"):
                return CodeProposal(
                    False, diff=diff, rationale=rationale, attempts=attempt,
                    skipped=True,
                    error=f"模型判断不该改代码：{rationale}",
                    rejected=tuple(rejected))

            if not diff:
                rejected.append("输出中没有可识别的 diff（缺少 ```diff 代码块或 --- 头）")
                feedback = ("必须输出一个 ```diff 代码块，内含 ---/+++/@@ 三段结构。"
                            "若要放弃，输出 `SKIP: <原因>`。")
                continue

            try:
                patches = parse_unified_diff(diff)
            except PatchError as exc:
                rejected.append(f"diff 无法解析：{exc}")
                feedback = f"diff 解析失败：{exc}。请检查 @@ 头与上下文行的前缀。"
                continue

            # **空补丁不算产出**：只有上下文行、没有 +/- 行的 diff 是合法的
            # 统一 diff 语法，但它什么都没改。解析成功不等于改动了代码，
            # 这一步不拦，「应用空补丁」会一路走到 apply 才失败，
            # 中间还会白占一次 Hook 与一次沙箱预算。
            if patches.touched == 0:
                rejected.append("补丁只有上下文行，没有任何 + / - 行（等于没改动）")
                feedback = ("你的 diff 里没有 + 或 - 行，因此没有任何改动。"
                            "要么给出真正的增删行，要么输出 `SKIP: <原因>`。")
                continue

            bad = guard.violations(patches)
            if bad:
                rejected.append("超出允许范围：" + "；".join(bad))
                feedback = ("补丁超出允许范围：" + "；".join(bad)
                            + "。只能改 " + str(list(allowed)) + " 下的文件。")
                continue

            return CodeProposal(True, diff=diff, rationale=rationale,
                                patches=patches, attempts=attempt,
                                rejected=tuple(rejected))

        return CodeProposal(False, diff=last_diff, rationale=rationale,
                            attempts=self.max_attempts, rejected=tuple(rejected),
                            error=error or "未能在重试上限内产出合规补丁")

    # ------------------------------------------------------------------ #
    async def _call(self, user: str) -> tuple[str, str, str]:
        from ai_butler.infra.llm_gateway import ChatMessage

        result = await self.gateway.chat(
            user_id=CODER_USER_ID,
            messages=(ChatMessage("system", CODER_SYSTEM_PROMPT),
                      ChatMessage("user", user)),
            max_tokens=self.max_tokens,
            rate_bucket="harness",
            temperature=self.temperature,
            model=self.model)
        text = getattr(result, "text", "") or ""
        diff, rationale = extract_diff(text)
        return diff, rationale, text


def _render_user_prompt(*, defect: Any, snippets: Sequence[CodeSnippet],
                        allowed: Sequence[str], verify_target: str) -> str:
    """组装给模型的用户消息。**信息给全，但不给无关上下文**。

    只放缺陷本身与允许范围：把整个仓库塞进去会让模型「顺便发现别的问题」，
    而离线链路的任务边界就是这一条缺陷。
    """
    title = getattr(defect, "title", "") or "（无标题）"
    evidence = getattr(defect, "evidence", "") or ""
    hint = getattr(defect, "target_hint", "") or ""
    repro = getattr(defect, "repro", "") or ""
    kind = getattr(getattr(defect, "kind", None), "value", "")

    files = ", ".join(sorted({s.path for s in snippets})) or "（未读到文件）"
    body = "\n\n".join(s.render() for s in snippets) or "（无可读源码）"

    return f"""## 缺陷
类别：{kind}
标题：{title}
证据：{evidence}
收敛提示（不是根因断言，需你自行判断）：{hint}
复现要点：{repro}

## 允许修改范围
{chr(10).join('- ' + p for p in allowed)}
（这些是路径前缀，只能改其下的已有文件，不得新增文件）

## 验证方式
修复后会在沙箱里运行：`{verify_target}`

## 已读入的源码（上下文行必须逐字复制，左侧数字是真实行号，不要写进 diff）
涉及文件：{files}

{body}

## 输出
只输出一个 ```diff 代码块。若判断不该改代码，只输出 `SKIP: <原因>`。
"""
