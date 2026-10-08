"""
中期摘要压缩中间件（接管 deepagents 底座自带的那个）
"""

import json
from typing import Any, List

from deepagents.backends import StateBackend
from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware

from app.core.logger import logger
from app.core.memory import summary_store
from app.core.memory.config import memory_config
from app.prompts.loader import load_prompt


def _message_text(message) -> str:
    """把一条消息拍成文本（content 可能是 str，也可能是 content blocks 列表）"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text")
                parts.append(value if isinstance(value, str) else str(item))
        text = "".join(parts)
    else:
        text = "" if content is None else str(content)

    calls = getattr(message, "tool_calls", None)
    if calls:
        # 工具调用的参数同样占上下文；漏算会让工具密集的会话被低估，阈值形同虚设
        try:
            text += json.dumps(calls, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text += str(calls)
    return text


def count_message_chars(messages, tools=None, **kwargs) -> int:
    """
    把「消息 + 工具 schema」折算成字符数，作为触发与保留段的计量口径
    签名必须兼容底座的两处调用：带 tools 关键字的一处，以及内部偏函数的单参调用。
    """
    total = 0
    for message in messages or ():
        total += len(_message_text(message))
    for tool in tools or ():
        if isinstance(tool, dict):
            schema: Any = tool
        else:
            schema = {
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", ""),
            }
        try:
            total += len(json.dumps(schema, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            total += len(str(schema))
    return total


class MemoryCompactionMiddleware(_DeepAgentsSummarizationMiddleware):
    """以本项目派生阈值为准、并把每一代摘要留痕的压缩中间件"""

    def __init__(self, model, backend=None):
        super().__init__(
            model=model,
            backend=backend if backend is not None else StateBackend(),
            trigger=("tokens", memory_config.compact_gate_chars),
            keep=("tokens", memory_config.compact_keep_chars),
            token_counter=count_message_chars,
            summary_prompt=load_prompt("memory_summary"),
            # 素材不预截断：底座默认只把最后 4000 个计数单位喂给摘要模型，
            # 对本项目这种「早期检索证据很关键」的会话会丢信息，故放开
            trim_tokens_to_summarize=None,
        )

    # 摘要生成是压缩里唯一「有损」的一步，也是唯一需要留凭据的一步
    def _create_summary(self, messages_to_summarize):
        summary = super()._create_summary(messages_to_summarize)
        self._audit(messages_to_summarize, summary)
        return summary

    async def _acreate_summary(self, messages_to_summarize):
        summary = await super()._acreate_summary(messages_to_summarize)
        self._audit(messages_to_summarize, summary)
        return summary

    def _audit(self, messages_to_summarize, summary: str) -> None:
        """
        落一代审计 + 覆盖当前代缓存

        只记 warning 不上抛：压缩此刻已经作用在上行请求上，审计失败不该把一次成功的压缩变成失败。
        """
        try:
            session_id = self._get_thread_id()
            summary_store.record_compaction(
                session_id,
                generation=summary_store.next_generation(session_id),
                source_count=len(messages_to_summarize),
                chars_before=count_message_chars(messages_to_summarize),
                chars_after=len(summary or ""),
                summary=summary or "",
                trigger_chars=memory_config.compact_gate_chars,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Memory] 摘要审计异常（不影响压缩本身）：{e}")


def build_compaction_middleware() -> List[Any]:
    """
    `HarnessProfile.extra_middleware` 的工厂（协议要求无参 callable）

    每次物化返回新实例：主栈与各子智能体栈各持一份，内部状态不共享。
    开关关闭时返回空序列 —— 此时 main_agent 也不排除底座实例，等于整体回退到底座默认行为。
    """
    if not memory_config.compact_enabled:
        return []

    from app.agent.llm import model

    return [MemoryCompactionMiddleware(model)]
