"""
字符计数口径（记忆层阈值的唯一单位）
"""

import json

# 工具 schema 参与计数：它同样占上下文，且本项目工具不多、schema 占比不可忽略
_TOOL_SCHEMA_FIELDS = ("name", "description")


def message_text(message) -> str:
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
    把「消息 + 工具 schema」折算成字符数

    签名刻意与底座的 token_counter 协议兼容（可能带 tools 关键字、也可能单参调用），
    这样同一个函数既能喂给本项目自己的裁剪判断，也能喂给摘要中间件替掉底座的 token 计数。
    """
    total = 0
    for message in messages or ():
        total += len(message_text(message))
    for tool in tools or ():
        if isinstance(tool, dict):
            schema = tool
        else:
            schema = {field: getattr(tool, field, "") for field in _TOOL_SCHEMA_FIELDS}
        try:
            total += len(json.dumps(schema, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            total += len(str(schema))
    return total
