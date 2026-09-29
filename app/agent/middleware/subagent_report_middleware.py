"""
子智能体返回契约的消费端中间件（纯观察者）

"""

import asyncio
from typing import Optional

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.agent.subagent_contract import SubAgentReport, parse_report_from_text
from app.api.context import get_thread_context
from app.api.monitor import monitor
from app.core.logger import logger
from app.core.subagent_reports import record_report
from app.core.tool_budget import TASK_TOOL_NAME


def _content_to_text(content) -> str:
    """把 ToolMessage.content 归一成文本（可能是 str，也可能是 content blocks 列表）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _extract_returned_text(result) -> str:
    """从 handler 的返回值里取出子智能体最后一条消息的文本。"""
    try:
        if isinstance(result, ToolMessage):
            return _content_to_text(result.content)
        # DeepAgents 的 task 工具返回 Command（update 里带一条 ToolMessage）
        update = getattr(result, "update", None) or {}
        messages = update.get("messages") or []
        if messages:
            return _content_to_text(getattr(messages[-1], "content", ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


def _session_key(request) -> Optional[str]:
    """会话 key：优先 ContextVar，其次运行时 config（与 ToolBudgetMiddleware 同一策略）。"""
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


def _notice_text(payload: dict) -> str:
    """生成面向前端的一句话说明（区分「正常/降级/故障/截断」四种情形）。"""
    name = payload.get("subagent") or "未知助手"
    if not payload.get("parsed"):
        return f"{name} 未给出返回契约代码块，已按正文内容降级理解"
    if payload.get("error"):
        return f"{name} 报告检索链路故障：{str(payload['error'])[:80]}"
    if payload.get("truncated_by_limit"):
        return f"{name} 报告信息因上限被截断（已标注来源 {payload.get('sources', 0)} 条）"
    return f"{name} 返回契约正常（来源 {payload.get('sources', 0)} 条）"


class SubAgentReportMiddleware(AgentMiddleware):
    """观察 `task` 返回、解析并上报子智能体返回契约。用法：挂到主智能体的 `middleware`。"""

    def wrap_tool_call(self, request, handler):
        result = handler(request)
        self._observe(request, result)
        return result

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        self._observe(request, result)
        return result

    # ------------------------------------------------------------------ 内部
    def _observe(self, request, result) -> None:
        """解析并上报（绝不抛、绝不改返回值）。"""
        try:
            tool_call = getattr(request, "tool_call", None) or {}
            if tool_call.get("name") != TASK_TOOL_NAME:
                return

            thread_id = _session_key(request)
            if not thread_id:
                return

            args = tool_call.get("args") or {}
            subagent = str(args.get("subagent_type") or "未知助手")
            text = _extract_returned_text(result)

            report: Optional[SubAgentReport] = parse_report_from_text(text)
            payload = record_report(thread_id, subagent, report, result_chars=len(text))
            if payload is None:
                return

            monitor.report_custom("subagent_report", _notice_text(payload), payload)
            if not payload["parsed"] or payload["error"] or payload["truncated_by_limit"]:
                logger.info(
                    f"[SubAgentReport] 非理想返回：session={thread_id}，subagent={subagent}，"
                    f"parsed={payload['parsed']}，truncated={payload['truncated_by_limit']}，"
                    f"error={payload['error']}"
                )
        except asyncio.CancelledError:
            # 取消语义必须原样向上传播，不能被本中间件吞掉
            raise
        except Exception as e:  # noqa: BLE001
            # 纯观察者：自身故障绝不影响主链路
            logger.opt(exception=True).warning(f"[SubAgentReport] 契约解析/上报异常，已忽略：{e}")
