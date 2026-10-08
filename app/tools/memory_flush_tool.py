"""
长期记忆整理工具

模型可以「要求整理」，但记什么、忘什么由服务端仲裁决定 —— 工具不带任何参数，
写不了内容（给参数等于把事实写入权交给模型，模型能凭空捏造事实）。
用户身份取自请求上下文，模型改不了自己是谁。
"""

from langchain_core.tools import tool

from app.api.context import get_user_context
from app.core.logger import logger
from app.core.memory.long_term import pipeline


@tool
async def flush_memory() -> str:
    """把本次对话中值得长期记住的用户信息整理进长期记忆。

    适用场景：用户明确说「记住这件事 / 以后都这样」，或对话里出现了稳定的用户事实
    （身份、长期偏好、固定约束）。不需要任何参数：用户身份由服务端从请求上下文取得。
    整理结果会被后续会话（含新会话）使用；若无需记录，它会自行判断为无变化。
    """
    try:
        user_id = get_user_context()
        result = await pipeline.run_extraction(user_id, force=True)
        return str(result.get("reason") or "记忆整理完成")
    except Exception as e:  # noqa: BLE001
        # 工具绝不抛：主图里一条普通异常就会毁掉整个任务
        logger.error(f"[LTM] 记忆整理工具失败（不影响本次对话）：{str(e)[:200]}")
        return "记忆整理暂时不可用（不影响本次对话）"
