"""
长期记忆注入中间件（只改上行请求，不落库）
"""

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage

from app.api.context import get_memory_block_context

# 记忆块消息的标记：既是「本请求是否已注入过」的幂等判据，也是「记忆块没落库」的检索依据
MEMORY_BLOCK_SOURCE = "long_term_memory"


class UserMemoryMiddleware(AgentMiddleware):
    """把长期记忆块插入上行请求最前。用法：挂到主智能体的 `middleware` 列表。"""

    def wrap_model_call(self, request, handler):
        return handler(self._inject(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._inject(request))

    @staticmethod
    def _inject(request):
        """返回（可能被改写的）request；任何异常都退回原请求，绝不打断模型调用"""
        try:
            block = get_memory_block_context()
            if not block:
                return request
            messages = list(request.messages or [])
            if messages and _is_memory_block(messages[0]):
                # 同一请求被内外两层中间件各包一次时不重复插入
                return request
            message = HumanMessage(
                content=block, additional_kwargs={"lc_source": MEMORY_BLOCK_SOURCE}
            )
            return request.override(messages=[message, *messages])
        except Exception:  # noqa: BLE001  注入失败退化成「本次不注入」
            return request


def _is_memory_block(message) -> bool:
    if not isinstance(message, HumanMessage):
        return False
    source = (getattr(message, "additional_kwargs", None) or {}).get("lc_source")
    return source == MEMORY_BLOCK_SOURCE
