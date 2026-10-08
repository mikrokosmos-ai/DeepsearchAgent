"""
记忆层中间件栈的组装

顺序即优先级：列表靠前 = 更外层 = 先看到（也先改写）上行请求。

    [工具结果裁剪, 摘要压缩]

裁剪是无损手段（50% 预算门），必须排在摘要（80% 门、有损）**外层**：
否则上下文一到 80% 就先做有损压缩，裁剪永远轮不到 —— 与「低破坏手段优先」的设计相反。

底座自带的摘要实例由 `main_agent` 的 `excluded_middleware` 排除，本栈是它的替代。
"""

from typing import List

from app.core.memory.config import memory_config


def build_memory_middleware_stack() -> List:
    """
    `HarnessProfile.extra_middleware` 的工厂（协议要求无参 callable）

    每次物化返回新实例：主栈与各子智能体栈各持一份，内部状态不共享。
    两个开关都关时返回空序列 —— 此时 main_agent 也不排除底座实例，整体回退到底座默认行为。
    """
    stack: List = []

    if memory_config.trim_enabled:
        from app.agent.middleware.memory_trim_middleware import MemoryTrimMiddleware

        stack.append(MemoryTrimMiddleware())

    if memory_config.compact_enabled:
        from app.agent.middleware.memory_compaction_middleware import MemoryCompactionMiddleware

        from app.agent.llm import model

        stack.append(MemoryCompactionMiddleware(model))

    return stack
