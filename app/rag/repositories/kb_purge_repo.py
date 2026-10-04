"""
知识库三存储按 item_name 的统一清理

三处清理逻辑原本内联分散在三个导入节点中，且表达式风格不统一
（chunks 单引号未转义、entity 双引号+转义、item_name 单引号未转义）。
本模块统一为「双引号 + escape_milvus_string 转义」，供 T10 的
reindex / disable / DELETE 复用。

绝不抛：任一存储清理失败只记 warning，继续清理其余存储，
返回逐存储结果字典，由调用方决定是否告警。
"""

from typing import Any, Dict

from app.core.logger import logger
from app.rag.clients.milvus_client import get_milvus_client
from app.rag.conf.milvus_config import milvus_config
from app.rag.repositories import graph_repo
from app.utils.escape_milvus_string_utils import escape_milvus_string


def _purge_milvus_collection(collection_name: str, item_name: str) -> bool:
    """按 item_name 清一个 Milvus 集合；集合不存在视为成功（幂等）。"""
    if not collection_name:
        return True
    try:
        client = get_milvus_client()
        if not client.has_collection(collection_name):
            return True
        client.delete(
            collection_name=collection_name,
            filter=f'item_name=="{escape_milvus_string(item_name)}"',
        )
        client.load_collection(collection_name=collection_name)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理 Milvus 集合失败：collection={collection_name}，item_name={item_name}，原因：{e}")
        return False


def purge_by_item_name(item_name: str) -> Dict[str, Any]:
    """
    按 item_name 清理三处存储：chunks / item_name / entity_name 集合 + Neo4j 图谱。

    删除语义为 delete-by-item_name，与三存储的「整体替换」语义一致 → 天然幂等。

    :param item_name: 三存储替换锚点
    :return: {"item_name": str, "milvus": {...}, "neo4j": bool, "ok": bool}
    """
    result: Dict[str, Any] = {
        "item_name": str(item_name or ""),
        "milvus": {},
        "neo4j": False,
        "ok": False,
    }
    if not item_name:
        logger.warning("按 item_name 清理被跳过：item_name 为空")
        return result

    result["milvus"]["chunks"] = _purge_milvus_collection(
        milvus_config.chunks_collection, item_name
    )
    # entity_name 集合承载图谱实体向量，必须与 Neo4j 一起清，否则会产生孤儿向量
    result["milvus"]["entity_name"] = _purge_milvus_collection(
        milvus_config.entity_name_collection, item_name
    )
    result["milvus"]["item_name"] = _purge_milvus_collection(
        milvus_config.item_name_collection, item_name
    )

    try:
        # 先删切片锚点（连带 MENTIONS 关系），再删失去引用的孤儿实体
        graph_repo.clear_by_item_name(item_name)
        result["neo4j"] = True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"清理 Neo4j 图谱失败：item_name={item_name}，原因：{e}")

    result["ok"] = all(result["milvus"].values()) and result["neo4j"]
    return result
