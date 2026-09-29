"""
工具调用次数预算中间件（会话级硬护栏的接线层）
"""

from typing import Optional

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.api.context import get_thread_context
from app.core.logger import logger
from app.core.tool_budget import (
    TASK_TOOL_NAME,
    BudgetDecision,
    consume_task_call,
    consume_tool_call,
)

# 会话 key 的来源优先级说明：
# 1) ContextVar `thread_id` —— `run_deep_agent` 在任务开始时写入，子智能体链路里同样可见
#    （`local_rag_tool` 的故障短路就是靠它取 session_id，已被 19 条断言的真实验证覆盖）；
# 2) 运行时 config 的 `configurable.thread_id` —— 兜底，覆盖 ContextVar 未能传播的场景。


def _session_key(request) -> Optional[str]:
    """取当前调用的会话 key；取不到返回 None（调用方据此 fail-open）。"""
    try:
        tid = get_thread_context()
        if tid:
            return str(tid)
    except Exception:  # noqa: BLE001
        pass
    try:
        config = getattr(getattr(request, "runtime", None), "config", None) or {}
        tid = (config.get("configurable") or {}).get("thread_id")
        return str(tid) if tid else None
    except Exception:  # noqa: BLE001
        return None


def _blocked_text(tool_name: str, decision: BudgetDecision) -> str:
    """
    生成超限提示。

    文案刻意复用 `agents.yml` 里已有的术语（【失败降级】【缺失项】、【拆解粒度】），
    以及子智能体返回契约的字段名（`truncated_by_limit`），让软/硬两条路径给模型的信号一致。
    """
    if tool_name == TASK_TOOL_NAME and decision.scope.startswith(f"{TASK_TOOL_NAME}::"):
        agent = decision.scope.split("::", 1)[1]
        return (
            f"【派发次数已达上限】子智能体「{agent}」在本任务内已被派发 {decision.used} 次"
            f"（上限 {decision.limit} 次），**本次派发未执行**。\n"
            f"请不要重复派发同一类任务；需要更多信息时换一个**更具体的新角度**，"
            f"或直接用已收集到的信息汇总。\n"
            f"若该路信息确实不完整，请在最终结论里按【失败降级】要求注明「哪一路缺失 + 原因」，"
            f"不要编造来源。"
        )
    if tool_name == TASK_TOOL_NAME:
        return (
            f"【子智能体派发总次数已达上限】本任务已派发 {decision.used} 次"
            f"（上限 {decision.limit} 次），**本次派发未执行**。\n"
            f"请立即基于已收集到的信息产出最终结果，并在结尾按【缺失项】要求列出"
            f"仍未获取的信息及其对结论的影响。"
        )
    return (
        f"【调用次数已达上限】`{tool_name}` 在本任务内已调用 {decision.used} 次"
        f"（上限 {decision.limit} 次），**本次调用未执行**。\n"
        f"请立即停止对该工具的继续调用，改用已获得的信息作答。\n"
        f"如果确实还缺关键信息：请在结论里说明「哪部分信息因次数上限未能获取」"
        f"（生成返回契约时把 truncated_by_limit 置为 true），不要反复重试同一工具。"
    )


class ToolBudgetMiddleware(AgentMiddleware):
    """会话级工具调用次数护栏。用法：挂到 `create_deep_agent(middleware=[...])` 或子智能体 spec 的 `middleware`。"""

    def wrap_tool_call(self, request, handler):
        blocked = self._check(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        blocked = self._check(request)
        return blocked if blocked is not None else await handler(request)

    def _check(self, request) -> Optional[ToolMessage]:
        """消费预算；超限时返回要短路返回的 ToolMessage，放行则返回 None。绝不抛异常。"""
        try:
            tool_call = getattr(request, "tool_call", None) or {}
            tool_name = tool_call.get("name")
            if not tool_name:
                return None

            thread_id = _session_key(request)
            if not thread_id:
                # 拿不到会话：只放行不计数（见模块 docstring 的取舍说明）
                return None

            if tool_name == TASK_TOOL_NAME:
                args = tool_call.get("args") or {}
                decision = consume_task_call(thread_id, str(args.get("subagent_type") or ""))
            else:
                decision = consume_tool_call(thread_id, tool_name)

            if decision is None or decision.allowed:
                return None

            logger.warning(
                f"[ToolBudget] 拦截超限调用：session={thread_id}，tool={tool_name}，"
                f"scope={decision.scope}，used={decision.used}/{decision.limit}"
            )
            return ToolMessage(
                content=_blocked_text(tool_name, decision),
                tool_call_id=tool_call.get("id"),
                name=tool_name,
            )
        except Exception as e:  # noqa: BLE001
            # 护栏自身故障必须放行，绝不能影响主链路
            logger.opt(exception=True).warning(f"[ToolBudget] 预算检查异常，本次放行：{e}")
            return None
