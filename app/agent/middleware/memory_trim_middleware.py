"""
工具结果裁剪中间件（只改上行请求，不落库）
"""

from langchain.agents.middleware.types import AgentMiddleware

from app.core.logger import logger
from app.core.memory.config import memory_config
from app.core.memory.trimmer import trim_tool_results


class MemoryTrimMiddleware(AgentMiddleware):
    """把白名单内、保护窗口外的历史工具结果换成占位文本。挂到主智能体的中间件栈。"""

    def wrap_model_call(self, request, handler):
        return handler(self._trim(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._trim(request))

    @staticmethod
    def _trim(request):
        """任何异常都退回原请求：裁剪失败只该少省一点上下文，不该影响这次调用"""
        try:
            if not memory_config.trim_enabled:
                return request
            messages = list(request.messages or [])
            before, after, count = trim_tool_results(
                messages,
                gate_chars=memory_config.trim_gate_chars,
                whitelist=memory_config.trim_tool_whitelist,
                protect_loops=memory_config.protect_tool_loops,
                tools=request.tools,
            )
            if count:
                logger.info(
                    f"[Memory] 工具结果裁剪：before={before} -> after={after}，共 {count} 块"
                )
            return request.override(messages=messages)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Memory] 工具结果裁剪异常，本轮回退原文：{str(e)[:160]}")
            return request
