"""
会话历史数据访问层

只承载「纯 Mongo 集合读写」：会话历史按 session_id 聚合成一条条消息文档。
与 vector_search_repo / graph_repo 同属 repositories 层，不含业务编排。
"""
from datetime import datetime
from typing import Any, Dict, List

from bson import ObjectId

from app.rag.clients.mongo_client import get_history_mongo_tool
from app.core.logger import logger


def clear_history(session_id: str) -> int:
    """
    清空指定会话的所有历史对话记录

    :param session_id: 会话唯一标识，用于筛选要删除的记录
    :return: 实际删除的文档数量，删除失败返回 0
    """
    mongo_tool = get_history_mongo_tool()
    try:
        result = mongo_tool.chat_message.delete_many({"session_id": session_id})
        logger.info(f"已清空会话历史：session={session_id}，删除 {result.deleted_count} 条")
        return result.deleted_count
    except Exception as e:
        logger.error(f"清空会话历史失败：session={session_id}，原因：{e}", exc_info=True)
        return 0


def save_chat_message(
        session_id: str,
        role: str,
        text: str,
        rewritten_query: str = "",
        item_names: List[str] = None,
        image_urls: List[str] = None,
        message_id: str = None
) -> str:
    """
    写入/更新单条会话记录到 MongoDB

    支持两种模式：无 message_id 时新增记录，有 message_id 时更新已有记录。

    :param session_id: 会话唯一标识，关联对话所属的会话
    :param role: 消息角色，固定值：user（用户）/ assistant（助手）
    :param text: 对话核心内容，用户的提问或助手的回答
    :param rewritten_query: 重写后的查询语句（可选，默认空字符串）
    :param item_names: 关联的商品名称列表（可选，默认 None）
    :param image_urls: 关联的图片 URL 列表（可选，默认 None）
    :param message_id: 记录主键 ID（可选，有值则更新，无值则新增）
    :return: 插入/更新的记录唯一标识（新增返回 ObjectId 字符串，更新返回传入的 message_id）
    """
    ts = datetime.now().timestamp()

    document = {
        "session_id": session_id,
        "role": role,
        "text": text,
        "rewritten_query": rewritten_query or "",
        "item_names": item_names,
        "image_urls": image_urls,
        "ts": ts,
    }

    mongo_tool = get_history_mongo_tool()
    if message_id:
        mongo_tool.chat_message.update_one(
            {"_id": ObjectId(message_id)},
            {"$set": document}
        )
        return message_id

    result = mongo_tool.chat_message.insert_one(document)
    return str(result.inserted_id)


def update_message_item_names(ids: List[str], item_names: List[str]) -> int:
    """
    批量更新历史会话记录的关联商品名称

    仅更新满足条件的记录：主键在指定列表中，且 item_names 为空 / 不存在 / None。

    :param ids: 要更新的记录主键 ID 列表（字符串类型）
    :param item_names: 要设置的新商品名称列表
    :return: 实际更新的文档数量，更新失败返回 0
    """
    mongo_tool = get_history_mongo_tool()
    try:
        object_ids = [ObjectId(i) for i in ids]
        result = mongo_tool.chat_message.update_many(
            {
                "_id": {"$in": object_ids},
                "$or": [
                    {"item_names": {"$exists": False}},
                    {"item_names": []},
                    {"item_names": None},
                ],
            },
            {"$set": {"item_names": item_names}}
        )
        logger.info(f"已回填历史记录商品名：更新 {result.modified_count} 条，item_names={item_names}")
        return result.modified_count
    except Exception as e:
        logger.error(f"批量更新历史记录商品名失败：{e}", exc_info=True)
        return 0


def get_recent_messages(session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    查询指定会话的最近 N 条对话记录，返回原始字典格式

    结果按时间正序排列，可直接喂给 LLM 作为上下文。

    :param session_id: 会话唯一标识，用于筛选指定会话的记录
    :param limit: 条数限制，默认返回最近 10 条
    :return: 对话记录列表（字典格式），查询失败返回空列表
    """
    mongo_tool = get_history_mongo_tool()
    try:
        query = {"session_id": session_id}
        # 按时间戳降序取最近 limit 条，再反转为正序
        cursor = mongo_tool.chat_message.find(query).sort("ts", -1).limit(limit)
        messages = list(cursor)
        messages.reverse()
        return messages
    except Exception as e:
        logger.error(f"查询会话历史失败：session={session_id}，原因：{e}", exc_info=True)
        return []
