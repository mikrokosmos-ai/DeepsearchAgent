"""
摘要压缩的审计与当前代缓存

只管「摘要」这一种数据，与 conversation_repo（管消息）分工：

    审计     每一代摘要追加一行到 Mongo `context_compaction`，只写不读（凭据）
    当前代   最新一代摘要缓存进 Redis，供读取入口与后续阶段复用（快路径）
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

from app.core.logger import logger
from app.core.memory.config import memory_config
from app.rag.clients.mongo_client import get_history_mongo_tool


def _cache_key(session_id: str) -> str:
    # session_id 会被当作键名片段，转义规则与热窗口保持一致（见 conversation_repo._cache_key）
    return f"{memory_config.summary_key_prefix}{quote(str(session_id), safe='')}"


def _redis_client():
    from app.rag.clients.manager.redis_client_manager import redis_client_manager

    if redis_client_manager.client is None:
        redis_client_manager.init()
    return redis_client_manager.client


def _collection():
    return get_history_mongo_tool().context_compaction


def next_generation(session_id: str) -> int:
    """
    计算本次压缩的代数

    代数取自审计集合的行数（权威），而不是 Redis 计数器：Redis 可能在无持久化的情况下
    被清空，那会让代数回退并与既有审计行撞唯一索引，等于静默丢审计。
    """
    try:
        return int(_collection().count_documents({"session_id": session_id})) + 1
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 摘要代数读取失败，按 1 处理：{e}")
        return 1


def record_compaction(
    session_id: str,
    *,
    generation: int,
    source_count: int,
    chars_before: int,
    chars_after: int,
    summary: str,
    trigger_chars: int,
) -> bool:
    """
    落一行审计，并把这一代摘要写进当前代缓存

    :return: 审计是否写入成功（缓存失败不影响该返回值）
    """
    doc: Dict[str, Any] = {
        "session_id": str(session_id),
        "generation": int(generation),
        "source_count": int(source_count),
        "chars_before": int(chars_before),
        "chars_after": int(chars_after),
        "trigger_chars": int(trigger_chars),
        "summary": summary,
        "ts": datetime.now().timestamp(),
    }

    audited = False
    try:
        _collection().insert_one(dict(doc))
        audited = True
    except Exception as e:  # noqa: BLE001  审计缺失不能影响压缩本身
        logger.warning(f"[Memory] 摘要审计落库失败（不影响压缩）：session={session_id}，原因：{e}")

    payload = {k: v for k, v in doc.items() if k != "_id"}
    try:
        client = _redis_client()
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if memory_config.summary_ttl_s > 0:
            client.set(_cache_key(session_id), blob, ex=memory_config.summary_ttl_s)
        else:
            client.set(_cache_key(session_id), blob)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 摘要当前代缓存写入失败（不影响压缩）：session={session_id}，原因：{e}")

    logger.info(
        f"[Memory] 摘要压缩：session={session_id}，代数={generation}，"
        f"素材={source_count} 条，字符 {chars_before} -> {chars_after}"
    )
    return audited


def get_current_summary(session_id: str) -> Optional[Dict[str, Any]]:
    """
    读取当前代摘要：Redis 优先，未命中回源审计集合并回填

    :return: 摘要记录字典（含 generation / summary / chars_before / chars_after 等）；两处都没有返回 None
    """
    try:
        client = _redis_client()
        key = _cache_key(session_id)
        raw = client.get(key)
        if raw is not None:
            return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001  缓存不可用即视为未命中，继续回源
        logger.warning(f"[Memory] 摘要缓存读取失败，转为回源：{e}")

    record: Optional[Dict[str, Any]] = None
    try:
        cursor = (
            _collection()
            .find({"session_id": str(session_id)}, {"_id": 0})
            .sort([("generation", -1)])
            .limit(1)
        )
        rows = list(cursor)
        record = rows[0] if rows else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 摘要回源查询失败：session={session_id}，原因：{e}")
        return None

    if record is None:
        return None

    logger.info(f"[Memory] 摘要缓存未命中，回源审计集合：session={session_id}")
    try:
        client = _redis_client()
        blob = json.dumps(record, ensure_ascii=False).encode("utf-8")
        if memory_config.summary_ttl_s > 0:
            client.set(_cache_key(session_id), blob, ex=memory_config.summary_ttl_s)
        else:
            client.set(_cache_key(session_id), blob)
    except Exception:  # noqa: BLE001  回填失败只影响下次是否再回源
        pass
    return record


def clear_summary(session_id: str) -> None:
    """清掉当前代缓存（审计行保留：它是凭据，删除属于运维动作）"""
    try:
        _redis_client().delete(_cache_key(session_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 摘要缓存清理失败（TTL 会兜底）：{e}")
