"""
统一会话消息读写层

把「会话 → 消息」收敛成一份数据，供三条消费方共用，取代原先互不相通的两条历史链
（chat_message 只服务 RAG、agent_message 只服务前端回读）：

    主智能体      layer=agent   跨进程 / 降级时补齐上下文
    RAG 子链路    layer=rag     多轮指代消解的最近若干轮
    /api/history  layer=agent   前端刷新、断线后的回读
"""

from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.core.logger import logger
from app.core.memory.config import memory_config
from app.rag.clients.mongo_client import get_history_mongo_tool

LAYER_AGENT = "agent"
LAYER_RAG = "rag"
VALID_LAYERS = (LAYER_AGENT, LAYER_RAG)

# 热窗口一次缓存多少条原始消息。取 50 是为了覆盖前端回读（上限 50 条）与内部窗口读取，
# 同时把单个 key 的体积限制在可预期范围内。
_WINDOW_MAX_MESSAGES = 50

# 会话标题取首条用户消息的前若干字符，仅供后续列表页使用
_TITLE_MAX_CHARS = 40

_MESSAGE_PROJECTION = {
    "_id": 0,
    "session_id": 1,
    "user_id": 1,
    "layer": 1,
    "role": 1,
    "text": 1,
    "rewritten_query": 1,
    "item_names": 1,
    "image_urls": 1,
    "ts": 1,
}


# ======================================================================
# 归一化：新集合文档 / 旧集合文档 都收敛成同一结构
# ======================================================================
def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _normalize(doc: Dict[str, Any], layer: str) -> Dict[str, Any]:
    """
    把一条消息文档补全成统一结构（缺字段补默认值）

    旧集合写入的文档字段不全（chat_message 没有 user_id，agent_message 没有 layer），
    这里统一补齐，使上层无需关心数据来自哪个集合。
    """
    return {
        "session_id": doc.get("session_id"),
        "user_id": doc.get("user_id"),
        "layer": doc.get("layer") or layer,
        "role": doc.get("role") or "",
        "text": doc.get("text") or "",
        "rewritten_query": doc.get("rewritten_query") or "",
        "item_names": _as_str_list(doc.get("item_names")),
        "image_urls": _as_str_list(doc.get("image_urls")),
        "ts": doc.get("ts") or 0.0,
    }


# ======================================================================
# 热窗口（Redis）
# ======================================================================
def _cache_key(session_id: str, layer: str) -> str:
    return f"{memory_config.conv_key_prefix}{quote(str(session_id), safe='')}:{layer}"


def _cache_get(session_id: str, layer: str) -> Optional[List[Dict[str, Any]]]:
    """读热窗口；未启用 / 未命中 / Redis 不可用一律返回 None（由调用方回源）"""
    if not memory_config.conv_cache_enabled:
        return None
    try:
        import json

        from app.rag.clients.manager.redis_client_manager import redis_client_manager

        if redis_client_manager.client is None:
            redis_client_manager.init()
        client = redis_client_manager.client
        key = _cache_key(session_id, layer)
        raw = client.get(key)
        if raw is None:
            return None
        # 读时续期：热窗口服务的是「连续几轮」的活跃会话，只读不续会把正在进行的会话读丢
        if memory_config.conv_ttl_s > 0:
            client.expire(key, memory_config.conv_ttl_s)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001  热层任何异常都不得影响读取
        logger.warning(f"[Memory] 会话热窗口读取失败，转为回源：{e}")
        return None


def _cache_put(session_id: str, layer: str, messages: List[Dict[str, Any]]) -> None:
    if not memory_config.conv_cache_enabled:
        return
    try:
        import json

        from app.rag.clients.manager.redis_client_manager import redis_client_manager

        if redis_client_manager.client is None:
            redis_client_manager.init()
        client = redis_client_manager.client
        key = _cache_key(session_id, layer)
        payload = json.dumps(messages, ensure_ascii=False)
        if memory_config.conv_ttl_s > 0:
            client.set(key, payload.encode("utf-8"), ex=memory_config.conv_ttl_s)
        else:
            client.set(key, payload.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 会话热窗口回填失败（不影响读取结果）：{e}")


def _cache_invalidate(session_id: str, layer: str) -> None:
    """
    失效热窗口

    写入后选择「删除」而不是「就地更新」：并发写入时，就地更新会把各自基于旧值算出的窗口
    互相覆盖（后写者胜），删除则让下一次读自然回源拿到权威数据。
    """
    if not memory_config.conv_cache_enabled:
        return
    try:
        from app.rag.clients.manager.redis_client_manager import redis_client_manager

        if redis_client_manager.client is None:
            redis_client_manager.init()
        redis_client_manager.client.delete(_cache_key(session_id, layer))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 会话热窗口失效失败（TTL 会兜底）：{e}")


# ======================================================================
# 回源：新集合优先，旧集合只读兜底
# ======================================================================
def _legacy_collection(layer: str):
    """旧集合与新集合的对应关系；旧集合是只读兜底，绝不写入"""
    mongo_tool = get_history_mongo_tool()
    return mongo_tool.agent_message if layer == LAYER_AGENT else mongo_tool.chat_message


def _query(cursor_source, query: Dict[str, Any], limit: int, layer: str) -> List[Dict[str, Any]]:
    """按 (ts, _id) 倒序取最近 limit 条再反转成正序 —— 与旧 get_recent_messages 的读法一致"""
    cursor = (
        cursor_source.find(query, _MESSAGE_PROJECTION)
        .sort([("ts", -1), ("_id", -1)])
        .limit(limit)
    )
    messages = [_normalize(doc, layer) for doc in cursor]
    messages.reverse()
    return messages


def _query_new(session_id: str, layer: str, limit: int) -> List[Dict[str, Any]]:
    """新集合按「会话 + 层」精确过滤。两层的消息共存于同一集合，漏掉 layer 条件就会串味"""
    return _query(
        get_history_mongo_tool().message,
        {"session_id": session_id, "layer": layer},
        limit,
        layer,
    )


def _query_legacy(session_id: str, layer: str, limit: int) -> List[Dict[str, Any]]:
    """旧集合没有 layer 字段（一个集合即一层），因此只按 session_id 过滤"""
    return _query(_legacy_collection(layer), {"session_id": session_id}, limit, layer)


def _load_from_mongo(session_id: str, layer: str) -> List[Dict[str, Any]]:
    """回源取该层最近 _WINDOW_MAX_MESSAGES 条；新集合为空时兜底读旧集合"""
    messages = _query_new(session_id, layer, _WINDOW_MAX_MESSAGES)
    if messages:
        return messages
    return _query_legacy(session_id, layer, _WINDOW_MAX_MESSAGES)


def _load_window(session_id: str, layer: str) -> List[Dict[str, Any]]:
    """读窗口：先热窗口，miss 回源 Mongo 并回填"""
    cached = _cache_get(session_id, layer)
    if cached is not None:
        return cached
    messages = _load_from_mongo(session_id, layer)
    # 回源日志是 S2.3 的观测关键字：只有真的打了 Mongo 才允许出现
    logger.info(f"[Memory] 会话热窗口回源：session={session_id}，layer={layer}，条数={len(messages)}")
    _cache_put(session_id, layer, messages)
    return messages


# ======================================================================
# 写入
# ======================================================================
def _resolve_user_id(user_id: Optional[str]) -> Optional[str]:
    """
    未显式传 user_id 时，从请求上下文里取

    采用延迟导入：仓储层不该硬依赖 API 层，但身份是「随请求走」的横切信息，
    放在这里统一解析，四个写点就都不必各自记得透传。
    """
    if user_id:
        return user_id
    try:
        from app.api.context import get_user_context

        return get_user_context()
    except Exception:  # noqa: BLE001  非请求链路（脚本、离线任务）取不到属正常
        return None


def append_message(
    session_id: str,
    layer: str,
    role: str,
    text: str,
    *,
    user_id: Optional[str] = None,
    rewritten_query: str = "",
    item_names: Optional[List[str]] = None,
    image_urls: Optional[List[str]] = None,
) -> bool:
    """
    追加一条会话消息（新集合为唯一写入目标）

    :return: 是否写入成功；失败只记 warning，绝不上抛
    """
    if not session_id or not str(session_id).strip():
        return False
    if not str(text or "").strip():
        return False
    if layer not in VALID_LAYERS:
        logger.warning(f"[Memory] 未知的消息 layer={layer}，拒绝写入")
        return False

    from datetime import datetime

    user_id = _resolve_user_id(user_id)
    ts = datetime.now().timestamp()
    document = {
        "session_id": session_id,
        "user_id": user_id or None,
        "layer": layer,
        "role": role,
        "text": text,
        "rewritten_query": rewritten_query or "",
        "item_names": _as_str_list(item_names),
        "image_urls": _as_str_list(image_urls),
        "ts": ts,
    }

    try:
        mongo_tool = get_history_mongo_tool()
        mongo_tool.message.insert_one(document)

        update: Dict[str, Any] = {
            "$set": {"updated_at": ts},
            "$setOnInsert": {"created_at": ts, "title": str(text)[:_TITLE_MAX_CHARS]},
            "$inc": {"message_count": 1},
        }
        # user_id 只在真的带了值时才覆盖：否则后续不带 user_id 的写入会把已知身份抹掉；
        # 与 $setOnInsert 同字段会引发 Mongo 的路径冲突，故二者互斥。
        if user_id:
            update["$set"]["user_id"] = user_id
        else:
            update["$setOnInsert"]["user_id"] = None
        mongo_tool.conversation.update_one({"session_id": session_id}, update, upsert=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 写入会话消息失败（不影响任务本身）：session={session_id}，原因：{e}")
        return False

    _cache_invalidate(session_id, layer)
    return True


# ======================================================================
# 读取
# ======================================================================
def _trim_by_budget(messages: List[Dict[str, Any]], budget_chars: int) -> List[Dict[str, Any]]:
    """
    从最新一条往回按字符预算裁剪

    至少保留最新一条：预算极小、或单条消息本身就超预算时，宁可略超预算也不返回空历史
    （返回空历史会让「跨进程补齐上下文」这件事静默失效，比超一点预算危险得多）。
    """
    if budget_chars <= 0:
        return messages[-1:] if messages else []
    kept: List[Dict[str, Any]] = []
    used = 0
    for message in reversed(messages):
        size = len(message.get("text") or "")
        if kept and used + size > budget_chars:
            break
        kept.append(message)
        used += size
    kept.reverse()
    return kept


def get_window(
    session_id: str,
    layer: str = LAYER_RAG,
    *,
    budget_chars: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    读取某层的会话窗口（先热窗口、miss 回源），再按预算 / 条数上限裁剪

    这是 RAG 子链路指代消解与主智能体补齐上下文的统一入口，取代原先的固定 10 条硬截断。
    """
    if layer not in VALID_LAYERS:
        return []
    try:
        messages = _load_window(session_id, layer)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 读取会话窗口失败：session={session_id}，原因：{e}")
        return []

    budget = memory_config.history_budget_chars if budget_chars is None else budget_chars
    messages = _trim_by_budget(messages, budget)
    if limit is not None and limit >= 0:
        messages = messages[-limit:] if limit else []
    return messages


def get_agent_history(session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    读取主智能体层历史（前端刷新 / 断线回读用）

    与 get_window 分开：这里的语义是「最多 N 条、按条数而非预算」，与内部窗口不同，
    故不共用热窗口（共用会让按预算裁过的窗口被当成完整历史返回）。
    """
    try:
        messages = _query_new(session_id, LAYER_AGENT, limit)
        if not messages:
            messages = _query_legacy(session_id, LAYER_AGENT, limit)
        return messages
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 读取主智能体历史失败：session={session_id}，原因：{e}")
        return []


def build_context_messages(
    session_id: str, *, budget_chars: Optional[int] = None
) -> List[Dict[str, str]]:
    """
    把主智能体层历史转成可直接拼进模型输入的消息列表

    用于「L0 无历史时补上下文」：跨进程重启、Redis 降级到内存实现等场景下，
    图状态是空的，此时用 L1 把最近若干轮补回来。
    """
    window = get_window(session_id, LAYER_AGENT, budget_chars=budget_chars)
    return [
        {"role": message["role"], "content": message["text"]}
        for message in window
        if message.get("role") in ("user", "assistant") and message.get("text")
    ]


def count_messages(session_id: str, layer: Optional[str] = None) -> int:
    """统计某会话（可限定层）的消息条数，供验证脚本与运维核对落库情况"""
    try:
        query: Dict[str, Any] = {"session_id": session_id}
        if layer:
            query["layer"] = layer
        return get_history_mongo_tool().message.count_documents(query)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 统计会话消息失败：session={session_id}，原因：{e}")
        return 0

# ======================================================================
# 会话索引（列表页）与按会话清理
# ======================================================================
_CONVERSATION_PROJECTION = {
    "_id": 0,
    "session_id": 1,
    "user_id": 1,
    "title": 1,
    "message_count": 1,
    "created_at": 1,
    "updated_at": 1,
}


def list_conversations(
    user_id: Optional[str] = None, limit: int = 100
) -> List[Dict[str, Any]]:
    """
    列出会话索引，按更新时间倒序

    索引由 append_message 的 upsert 维护（首条消息定标题、每条累加计数、每次刷新
    updated_at），因此列表页不必扫消息集合。索引集合上已有 (user_id, updated_at) 索引。

    user_id 过滤刻意带上「无归属」的历史行：本项目没有登录体系，user_id 由浏览器生成并
    持久化，未带 user_id 的行只可能是同一台机器在身份透传补全之前写入的，把它们挡在
    列表外会让老会话凭空消失。
    """
    try:
        query: Dict[str, Any] = {}
        if user_id:
            query["$or"] = [
                {"user_id": user_id},
                {"user_id": None},
                {"user_id": {"$exists": False}},
            ]
        cursor = (
            get_history_mongo_tool()
            .conversation.find(query, _CONVERSATION_PROJECTION)
            .sort([("updated_at", -1)])
            .limit(max(1, limit))
        )
        sessions: List[Dict[str, Any]] = []
        for doc in cursor:
            session_id = doc.get("session_id")
            if not session_id:
                continue
            sessions.append(
                {
                    "session_id": session_id,
                    "user_id": doc.get("user_id"),
                    "title": doc.get("title") or "",
                    "message_count": int(doc.get("message_count") or 0),
                    "created_at": float(doc.get("created_at") or 0.0),
                    "updated_at": float(doc.get("updated_at") or 0.0),
                }
            )
        return sessions
    except Exception as e:
        logger.warning(f"[Memory] 列出会话失败（按空列表处理）：{e}")
        return []


def delete_session(session_id: str) -> Dict[str, int]:
    """
    删除该会话的对话数据：新集合的消息行与会话行 + 旧集合兜底行 + 热窗口

    旧集合必须一起删：回源逻辑在「新集合为空」时会兜底读旧集合，
    留下旧行等于「删了还在」。热窗口的失效不能省 —— 否则下一次读取命中缓存，
    被删的消息会从缓存里复活（TTL 到期前一直可见）。

    :return: 各类被删条数；单步失败只记 warning，由调用方决定如何呈现
    """
    removed = {"messages": 0, "legacy_agent": 0, "legacy_rag": 0, "conversation": 0}
    if not session_id:
        return removed
    try:
        mongo_tool = get_history_mongo_tool()
        query = {"session_id": session_id}
        removed["messages"] = mongo_tool.message.delete_many(query).deleted_count
        removed["legacy_agent"] = mongo_tool.agent_message.delete_many(query).deleted_count
        removed["legacy_rag"] = mongo_tool.chat_message.delete_many(query).deleted_count
        removed["conversation"] = mongo_tool.conversation.delete_many(query).deleted_count
    except Exception as e:
        logger.warning(f"[Memory] 删除会话消息失败：session={session_id}，原因：{e}")

    for layer in VALID_LAYERS:
        _cache_invalidate(session_id, layer)
    return removed
