"""
知识库文档登记访问层

MD 是唯一事实源；本层只读写登记元数据（`kb_document` 集合）。
所有函数绝不抛：失败只记 warning 并返回 False / 空结构。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.rag.clients.mongo_client import get_history_mongo_tool

# 文档状态取值
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_PROCESSING = "processing"
STATUS_REINDEXING = "reindexing"
STATUS_FAILED = "failed"
STATUS_DELETED = "deleted"

# 处于「飞行中」的状态：此时禁止编辑 / 重建 / 启停 / 删除（每 doc 单飞）
BUSY_STATUSES = (STATUS_PROCESSING, STATUS_REINDEXING)

# 单次列表返回上限
DEFAULT_DOCS_LIMIT = 200

# 登记表字段（供 service 层构造 doc 时对齐）
DOC_FIELDS = (
    "doc_id",
    "item_name",
    "file_title",
    "md_path",
    "output_dir",
    "status",
    "chunk_count",
    "content_hash",
    "indexed_hash",
    "edit_log",
    "origin_task_id",
    "last_reindex_task_id",
)


def _now() -> float:
    # 与 chat_message / agent_message / agent_run 同一时间基准
    return datetime.now().timestamp()


def _collection():
    return get_history_mongo_tool().kb_document


def upsert_document(doc: Dict[str, Any]) -> bool:
    """
    以 doc_id（缺失则 item_name）为锚点 upsert 一条登记。

    :param doc: 登记字段字典（见 DOC_FIELDS）
    :return: 是否写入成功；任何异常都返回 False 且不抛出
    """
    try:
        if not isinstance(doc, dict):
            return False
        payload = {k: doc.get(k) for k in DOC_FIELDS if k in doc}
        if not payload.get("doc_id") and not payload.get("item_name"):
            logger.warning("知识库文档登记失败：doc_id 与 item_name 均缺失")
            return False
        payload["ts"] = float(doc.get("ts") or _now())
        payload.setdefault("edit_log", [])

        col = _collection()
        if payload.get("doc_id"):
            anchor = {"doc_id": str(payload["doc_id"])}
        else:
            anchor = {"item_name": str(payload["item_name"])}
        col.update_one(anchor, {"$set": payload}, upsert=True)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"知识库文档登记失败（不影响导入本身）：原因：{e}")
        return False


def get_document(doc_id: str) -> Optional[Dict[str, Any]]:
    """
    按 doc_id 取一条登记（含已软删记录，由调用方判 status）。

    :return: 登记字典；不存在或查询失败返回 None
    """
    try:
        doc = _collection().find_one({"doc_id": str(doc_id)})
        if not doc:
            return None
        doc.pop("_id", None)
        return doc
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取知识库文档登记失败：doc_id={doc_id}，原因：{e}")
        return None


def find_by_item_name(item_name: str) -> Optional[Dict[str, Any]]:
    """按 item_name 取一条登记（存量迁移 / 查重用）。"""
    try:
        doc = _collection().find_one({"item_name": str(item_name)})
        if not doc:
            return None
        doc.pop("_id", None)
        return doc
    except Exception as e:  # noqa: BLE001
        logger.warning(f"按 item_name 读取登记失败：item_name={item_name}，原因：{e}")
        return None


def list_documents(
    status: Optional[str] = None,
    limit: int = DEFAULT_DOCS_LIMIT,
    offset: int = 0,
    include_deleted: bool = False,
) -> Dict[str, Any]:
    """
    分页列出登记（以 Mongo 登记为准，不依赖内存态 `_kb_tasks`）。

    :param status: 可选状态过滤
    :param limit: 每页条数
    :param offset: 跳过条数
    :param include_deleted: 是否包含软删记录（默认不含）
    :return: {"total": n, "items": [...]}；失败返回 {"total": 0, "items": []}
    """
    empty: Dict[str, Any] = {"total": 0, "items": []}
    try:
        query: Dict[str, Any] = {}
        if status:
            query["status"] = str(status)
        elif not include_deleted:
            query["status"] = {"$ne": STATUS_DELETED}

        col = _collection()
        total = int(col.count_documents(query))
        cursor = (
            col.find(query)
            .sort([("ts", -1), ("_id", -1)])
            .skip(max(0, int(offset)))
            .limit(max(1, int(limit)))
        )
        items = []
        for doc in cursor:
            doc.pop("_id", None)
            items.append(doc)
        return {"total": total, "items": items}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"列出知识库文档失败：原因：{e}")
        return empty


def update_document(doc_id: str, patch: Dict[str, Any], log_entry: Optional[Dict[str, Any]] = None) -> bool:
    """
    局部更新登记字段；可选追加一条 edit_log。

    :param doc_id: 文档锚点
    :param patch: 待更新字段（自动过滤为 DOC_FIELDS 内的键）
    :param log_entry: 追加到 edit_log 的记录（{action, note, ts}）
    :return: 是否更新成功；异常返回 False 且不抛出
    """
    try:
        update: Dict[str, Any] = {
            "$set": {k: v for k, v in (patch or {}).items() if k in DOC_FIELDS}
        }
        update["$set"]["ts"] = _now()
        if log_entry:
            entry = {
                "ts": float(log_entry.get("ts") or _now()),
                "action": str(log_entry.get("action") or ""),
                "note": str(log_entry.get("note") or ""),
            }
            update["$push"] = {"edit_log": entry}
        result = _collection().update_one({"doc_id": str(doc_id)}, update)
        return bool(result.matched_count)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"更新知识库文档登记失败：doc_id={doc_id}，原因：{e}")
        return False


def mark_deleted(doc_id: str, note: str = "") -> bool:
    """软删：只改状态，保留 MD 与登记（可逆）。三存储清理由 service 层负责。"""
    return update_document(
        doc_id,
        {"status": STATUS_DELETED},
        log_entry={"action": "delete", "note": note},
    )


def normalize_stale_reindexing(note: str = "启动时归一：疑似上次进程中断") -> int:
    """
    启动归一化：把残留的 processing / reindexing 记录置为 failed。

    此时 `indexed_hash` 未被改写 → 重新触发 reindex 安全幂等。

    :return: 被归一化的记录数；异常返回 0 且不抛出
    """
    try:
        result = _collection().update_many(
            {"status": {"$in": list(BUSY_STATUSES)}},
            {
                "$set": {"status": STATUS_FAILED, "ts": _now()},
                "$push": {"edit_log": {"ts": _now(), "action": "normalize", "note": str(note)}},
            },
        )
        n = int(getattr(result, "modified_count", 0) or 0)
        if n:
            logger.info(f"知识库文档启动归一化：{n} 条残留飞行态记录已置为 failed")
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning(f"知识库文档启动归一化失败：原因：{e}")
        return 0
