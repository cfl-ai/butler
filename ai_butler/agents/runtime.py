"""子 Agent 运行时小工具。

背景
----
子 Agent 的调用契约允许**同步与异步两种 ~{run}~**：
- 纯计算类 Agent（时间进度、代码迭代提案）用同步 ~{run}~ 即可；
- 需要读写数据中台、RAG 的 Agent 必须用 **异步** ~{run}~。

为什么不能用「同步 run + 换线程跑新事件循环」这条捷径
--------------------------------------------------
asyncpg 的连接与创建它的事件循环**强绑定**。在主循环之外新起一个循环去复用同一连接池，
会得到 ~{got Future attached to a different loop}~。这不是偶发问题，而是必然失败。

因此 ~{run_sync}~ 只解决「当前根本没有事件循环」的情形；一旦已在循环内，
它**明确报错并指向正确做法**（把该 Agent 的 run 声明为 async），
而不是换线程硬跑、留下难以排查的故障。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class AgentRuntimeError(RuntimeError):
    pass


def running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    """在**没有**事件循环的同步上下文里执行协程。"""
    if running_loop():
        coro.close()                      # 避免 "coroutine was never awaited" 警告
        raise AgentRuntimeError(
            "当前已在事件循环内，同步 run 不能调用异步依赖：asyncpg 连接与事件循环绑定，"
            "换线程会得到「Future attached to a different loop」。"
            "请把该 Agent 的 run 声明为 async —— 调度层（ai_butler.scheduler.graph）"
            "已经支持异步 Agent，会用 resolve() 统一取值。")
    return asyncio.run(coro)
