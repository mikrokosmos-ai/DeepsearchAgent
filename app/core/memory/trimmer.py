"""
工具结果裁剪（无损手段：只把历史工具结果换成占位文本，不删消息、不调模型）
"""

from typing import List, Sequence, Set, Tuple

from langchain_core.messages import ToolMessage

from app.core.memory.chars import count_message_chars, message_text

# 与官方保持一致的幂等标记 key：已清过的块不再重复替换，也不会被重复计数
CLEARED_KEY = "context_editing"

PLACEHOLDER_TEMPLATE = (
    "[历史工具结果已归档：{tool}（原 {chars} 字）。需要该内容时请重新调用该工具。]"
)

# 太短的块不值得替换（占位文本本身也有长度，替换反而可能变长）
MIN_TRIM_CHARS = 200


def _already_cleared(message: ToolMessage) -> bool:
    meta = getattr(message, "response_metadata", None) or {}
    return bool((meta.get(CLEARED_KEY) or {}).get("cleared"))


def protected_indices(messages: Sequence, protect_loops: int) -> Set[int]:
    """
    保护窗口内的下标集合
    """
    protected: Set[int] = set()

    tool_indices = [idx for idx, msg in enumerate(messages) if isinstance(msg, ToolMessage)]
    if protect_loops > 0:
        protected.update(tool_indices[-protect_loops:])

    answered = {
        getattr(msg, "tool_call_id", None)
        for msg in messages
        if isinstance(msg, ToolMessage)
    }
    pending = set()
    for msg in messages:
        for call in getattr(msg, "tool_calls", None) or []:
            call_id = call.get("id") if isinstance(call, dict) else None
            if call_id and call_id not in answered:
                pending.add(call_id)
    if pending:
        for idx, msg in enumerate(messages):
            if isinstance(msg, ToolMessage) and getattr(msg, "tool_call_id", None) in pending:
                protected.add(idx)
    return protected


def _replace_with_placeholder(message: ToolMessage, chars: int) -> ToolMessage:
    """生成占位版消息；任何异常都退回原消息（裁剪失败比不裁更糟）"""
    try:
        replaced = message.model_copy(deep=True)
        replaced.content = PLACEHOLDER_TEMPLATE.format(
            tool=getattr(message, "name", None) or "未知工具", chars=chars
        )
        meta = dict(replaced.response_metadata or {})
        meta[CLEARED_KEY] = {"cleared": True}
        replaced.response_metadata = meta
        return replaced
    except Exception:  # noqa: BLE001
        return message


def trim_tool_results(
    messages: List,
    *,
    gate_chars: int,
    whitelist: Sequence[str],
    protect_loops: int,
    tools=None,
) -> Tuple[int, int, int]:
    """
    就地裁剪：把白名单内、保护窗口外的历史工具结果换成占位文本

    :return: (裁剪前字符数, 裁剪后字符数, 被裁剪的块数)
    """
    before = count_message_chars(messages, tools=tools)
    if gate_chars <= 0 or before <= gate_chars:
        return before, before, 0

    allowed = set(whitelist or ())
    protected = protected_indices(messages, protect_loops)
    trimmed = 0

    for idx, message in enumerate(messages):
        if not isinstance(message, ToolMessage) or idx in protected:
            continue
        if allowed and getattr(message, "name", None) not in allowed:
            continue
        if _already_cleared(message):
            continue
        size = len(message_text(message))
        if size < MIN_TRIM_CHARS:
            continue
        messages[idx] = _replace_with_placeholder(message, size)
        trimmed += 1

    after = count_message_chars(messages, tools=tools)
    return before, after, trimmed
