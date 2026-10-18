"""子 Agent 基类。

计划书依据
----------
- 三、P4「所有子 Agent 只读全局状态、仅写入自有字段、无跨模块越权调用」
- 三、P4「隐私约束：读写私有数据必须从 LangGraph State 获取 user_id，
  不允许硬编码、不能为空；禁止读取其他 user_id 数据」
- 三、P4「触发时机：全局状态变更自动触发对应子任务执行；**会话失效则跳过执行**」
- 三、P4「Wiki 约束：子 Agent 产出事实优先写入对应用户私有 Vault；
  输出事实绑定 Claim 声明 ID 与 Evidence 证据 ID」
- 五、5「旅游 Agent 仅负责行程业务逻辑，无权修改路由规则、无权分发任务给其他 Agent」
- 三、P3「开发多子 Agent 自动分发路由；下发任务携带 user_id、wiki_digest」

本基类把上述约束变为**代码级强制**，而不是靠开发者自觉：
1. user_id 只能从 State 取，不允许构造参数注入、不允许硬编码；
2. 写入字段必须在 writes 白名单内（仅写入自有字段）；
3. 会话失效直接跳过执行；
4. 不存在任何「把任务转交给另一个 Agent」的入口。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from ai_butler.agents.state import StateContractError, validate_state


async def resolve(result: Any) -> dict[str, Any]:
    """把 Agent 的返回值统一成 dict：同步 run 给 dict，异步 run 给 awaitable。"""
    if inspect.isawaitable(result):
        return await result
    return result


class WriteScopeError(RuntimeError):
    """Agent 试图写入自有字段之外的 State 字段。计划书：仅写入自有字段。"""


class SubAgent(ABC):
    #: Agent 唯一标识
    name: ClassVar[str] = ""
    #: 允许写入的 State 字段（仅自有字段）
    writes: ClassVar[tuple[str, ...]] = ()
    #: 允许读取的 State 字段（必填字段始终可读）
    reads: ClassVar[tuple[str, ...]] = ()
    #: 计划书三、P4「无跨模块越权调用」：本 Agent 需要的外部能力。
    #: 装配层按此声明裁剪工具面，未声明的能力**根本不会发到手上**。
    capabilities: ClassVar[tuple[str, ...]] = ()

    #: 是否允许持有支付执行能力。
    #: 统一架构规范 10.11 禁止**垂类 Agent**直接调用支付执行能力，必须经支付 Agent 中转——
    #: 因此默认 False；只有 P8 的支付 Agent 自身才显式声明 True。
    #: 用显式开关而不是「一律禁止」，是因为一律禁止会让支付 Agent 自己也拿不到能力。
    may_hold_payment: ClassVar[bool] = False
    #: 权责说明，供注册表台账与人工审阅
    responsibilities: ClassVar[str] = ""

    # ---------------------------- 命名 ---------------------------- #
    @property
    def display_name(self) -> str:
        """人读中文名 = 计划书里那个项目名（P1 就叫「数据中台」）。

        **派生而非手写**：`name` 是机读 key（registry 字典键、State 字段前缀），
        必须是 ASCII；中文名统一到 `core.naming` 查表，任何 Agent 都不许自己
        另起一个中文名——否则名册、日志、文档会各自叫一套（E-44）。

        未登记的 key 退回英文名，由 `tests/test_module_naming.py`
        断言「一个都不许漏」，而不是等运行时才炸。
        """
        from ai_butler.core.naming import agent_title
        try:
            return agent_title(self.name)
        except KeyError:
            return self.name

    # ------------------------- 对外唯一入口 ------------------------- #
    def __call__(self, state: dict[str, Any]) -> Any:
        """执行本 Agent。

        返回值**可能是 dict，也可能是 awaitable** —— 取决于 run 是同步还是异步。
        异步 run 是必需的：asyncpg 连接与事件循环绑定，同步 Agent 里换线程跑新循环
        会得到「Future attached to a different loop」。
        调用方请用 ~{resolve()}~ 统一取值（P3 调度器即如此）。
        """
        validate_state(state)                      # 必填契约 + user_id 非空
        if not state.get("login_status"):
            # 计划书三、P4：会话失效则跳过执行（不抛错，由调度层按 P3 终止 Graph）
            return {}
        outcome = self.run(state)
        if inspect.isawaitable(outcome):
            return self._complete_async(outcome)
        self._validate_write_scope(outcome)
        return outcome

    async def _complete_async(self, awaitable: Any) -> dict[str, Any]:
        updates = await awaitable
        self._validate_write_scope(updates)
        return updates

    # ------------------------- 受控上下文 ------------------------- #
    @staticmethod
    def require_user_id(state: dict[str, Any]) -> str:
        """唯一合法的 user_id 获取方式：从 State 读取。禁止硬编码、禁止入参注入。"""
        uid = state.get("user_id")
        if not isinstance(uid, str) or not uid.strip():
            raise StateContractError("user_id 必须来自 State 且非空（计划书三、P4）")
        return uid

    @classmethod
    def wiki_digest(cls, state: dict[str, Any]) -> Any:
        """计划书三、P3：下发任务携带 wiki_digest。"""
        return state.get("wiki_digest")

    # ------------------------- 写作用域 ------------------------- #
    @classmethod
    def _validate_write_scope(cls, updates: dict[str, Any]) -> None:
        if not isinstance(updates, dict):
            raise WriteScopeError(f"{cls.name} 的 run() 必须返回 dict，实际 {type(updates).__name__}")
        illegal = [k for k in updates if k not in cls.writes]
        if illegal:
            raise WriteScopeError(
                f"{cls.name} 试图写入非自有字段 {illegal}。"
                f"计划书三、P4：所有子 Agent 只读全局状态、仅写入自有字段。"
                f"允许写入：{list(cls.writes)}"
            )

    # ------------------------- 子类实现 ------------------------- #
    @abstractmethod
    def run(self, state: dict[str, Any]) -> dict[str, Any]:
        """执行业务逻辑，返回本 Agent 自有字段的更新。"""
