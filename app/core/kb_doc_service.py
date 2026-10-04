"""
知识库文档管理业务层

职责：把「文档级管理」的业务规则从路由层剥离，便于用假 Mongo 做守卫测试。
- 登记（新导入 / 存量 sync）
- 编辑 MD（版本备份 + hash 更新）
- reindex 前置清理（按**登记锚点** item_name，防重识别漂移）
- 状态守卫（每 doc 单飞）
- 路径安全（只信登记 md_path + resolve 白名单）

所有对外的「查询类」函数返回 None / 空结构表达未命中；
「变更类」函数返回 (ok: bool, payload: dict)，由路由层转 HTTP 状态码。
"""

import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.logger import logger
from app.core.runtime_paths import KB_OUTPUT_DIR
from app.rag.repositories import kb_doc_repo
from app.rag.repositories.kb_purge_repo import purge_by_item_name

# 版本备份目录名（落在文档目录下：output/kb/{task_id}/{stem}/versions/）
VERSIONS_DIRNAME = "versions"
# 单文件大小上限（与 kb_routes 一致）
MAX_MD_BYTES = 100 * 1024 * 1024


# ------------------------------ 通用工具 ------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _resolve_in_kb_root(path_value: str) -> Optional[Path]:
    """
    把登记的 md_path 解析为绝对路径，并强制落在 output/kb/ 白名单内。

    :return: 合法则返回 resolve 后的 Path；越界 / 非法返回 None
    """
    if not path_value:
        return None
    try:
        candidate = Path(str(path_value)).resolve()
        root = KB_OUTPUT_DIR.resolve()
        if not candidate.is_relative_to(root):
            logger.warning(f"路径越界被拒：{candidate} 不在 {root} 内")
            return None
        return candidate
    except Exception as e:  # noqa: BLE001
        logger.warning(f"路径解析失败：{path_value}，原因：{e}")
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return default


# ------------------------------ 登记 ------------------------------

def register_from_import(final_state: Optional[Dict[str, Any]], origin_task_id: str) -> Optional[str]:
    """
    新导入成功后登记一条 kb_document（由 kb_routes._run_import_task 调用）。

    非侵入：不修改导入图任何节点，只在图跑完后消费其返回 state。
    字段缺失时只 warning 不抛，尽量登记可用部分。

    :param final_state: `kb_import_app.invoke` 的返回 state（可能为 None）
    :param origin_task_id: 本次导入的 task_id
    :return: 登记的 doc_id；无法登记返回 None
    """
    try:
        state = final_state if isinstance(final_state, dict) else {}
        item_name = str(state.get("item_name") or "").strip()
        md_path = str(state.get("md_path") or "").strip()
        chunks = state.get("chunks") or []

        if not item_name or not md_path:
            logger.warning(
                f"知识库文档登记跳过：关键字段缺失（item_name={item_name!r}，md_path={md_path!r}），"
                f"task_id={origin_task_id}"
            )
            return None

        resolved = _resolve_in_kb_root(md_path)
        if resolved is None:
            logger.warning(f"知识库文档登记跳过：md_path 越界或非法（{md_path}）")
            return None

        content_hash = _sha256(_read_text(resolved) or "")
        # md_path 形如 output/kb/{task_id}/{stem}/{stem}.md → output_dir 取其父的父
        output_dir = str(resolved.parent.parent) if resolved.parent.parent else ""

        # 同一 item_name 重复导入 → 复用既有 doc_id（登记表以 item_name 为业务锚点）
        existing = kb_doc_repo.find_by_item_name(item_name)
        doc_id = str(existing.get("doc_id")) if existing and existing.get("doc_id") else uuid.uuid4().hex

        doc = {
            "doc_id": doc_id,
            "item_name": item_name,
            "file_title": str(state.get("file_title") or resolved.stem),
            "md_path": str(resolved),
            "output_dir": output_dir,
            "status": kb_doc_repo.STATUS_ACTIVE,
            "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
            "content_hash": content_hash,
            # 刚导入完成 → 已索引内容与当前 MD 一致
            "indexed_hash": content_hash,
            "origin_task_id": str(origin_task_id or ""),
            "last_reindex_task_id": "",
            "edit_log": list(existing.get("edit_log") or []) if existing else [],
        }
        ok = kb_doc_repo.upsert_document(doc)
        if ok:
            logger.info(
                f"知识库文档已登记：doc_id={doc_id}，item_name={item_name}，"
                f"chunks={doc['chunk_count']}，task_id={origin_task_id}"
            )
            return doc_id
        return None
    except Exception as e:  # noqa: BLE001
        # 登记失败绝不能影响导入本身的成败判定
        logger.warning(f"知识库文档登记异常（不影响导入）：task_id={origin_task_id}，原因：{e}")
        return None


def _read_text(path: Path) -> Optional[str]:
    """读取 MD 文本；失败返回 None（绝不抛）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取 MD 失败：{path}，原因：{e}")
        return None


# ------------------------------ 查询 ------------------------------

def get_content(doc_id: str) -> Optional[Dict[str, Any]]:
    """
    读取文档正文（路径只从登记取，越界即拒）。

    :return: {"doc": {...}, "content": str}；未命中 / 越界返回 None
    """
    doc = kb_doc_repo.get_document(doc_id)
    if not doc:
        return None
    resolved = _resolve_in_kb_root(doc.get("md_path"))
    if resolved is None or not resolved.exists():
        logger.warning(f"文档正文不可读：doc_id={doc_id}，md_path={doc.get('md_path')}")
        return None
    content = _read_text(resolved) or ""
    return {"doc": doc, "content": content}


def list_documents(status: Optional[str] = None, limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    """列表（透传 repo，附「已编辑未重建」派生标记）。"""
    result = kb_doc_repo.list_documents(status=status, limit=limit, offset=offset)
    for item in result.get("items", []):
        item["edited_not_reindexed"] = is_edited_not_reindexed(item)
    return result


def is_edited_not_reindexed(doc: Dict[str, Any]) -> bool:
    """「已编辑未重建」信号：content_hash 与 indexed_hash 不一致。"""
    content_hash = str(doc.get("content_hash") or "")
    indexed_hash = str(doc.get("indexed_hash") or "")
    return bool(content_hash) and content_hash != indexed_hash


def get_chunks(doc_id: str, limit: int = 200) -> Optional[Dict[str, Any]]:
    """
    只读浏览某文档的 chunks（按登记 item_name 查 Milvus）。

    :return: {"doc": {...}, "chunks": [...]}；未命中返回 None
    """
    doc = kb_doc_repo.get_document(doc_id)
    if not doc:
        return None
    chunks = _query_chunks_by_item_name(str(doc.get("item_name") or ""), limit=limit)
    return {"doc": doc, "chunks": chunks}


def _query_chunks_by_item_name(item_name: str, limit: int = 200) -> list:
    """按 item_name 只读查询 chunks 集合（绝不抛）。"""
    if not item_name:
        return []
    try:
        from app.rag.clients.milvus_client import get_milvus_client
        from app.rag.conf.milvus_config import milvus_config
        from app.utils.escape_milvus_string_utils import escape_milvus_string

        collection = milvus_config.chunks_collection
        client = get_milvus_client()
        if not collection or not client.has_collection(collection):
            return []
        client.load_collection(collection_name=collection)
        rows = client.query(
            collection_name=collection,
            filter=f'item_name=="{escape_milvus_string(item_name)}"',
            output_fields=["chunk_id", "file_title", "title", "parent_title", "part", "content"],
            limit=max(1, _safe_int(limit, 200)),
        )
        return list(rows or [])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取 chunks 失败：item_name={item_name}，原因：{e}")
        return []


# ------------------------------ 状态守卫 ------------------------------

def check_mutable(doc_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    校验文档当前是否允许编辑 / 重建 / 启停 / 删除（每 doc 单飞）。

    :return: (allowed, reason, doc)；doc 为 None 表示未登记
    """
    doc = kb_doc_repo.get_document(doc_id)
    if not doc:
        return False, "文档不存在", None
    status = str(doc.get("status") or "")
    if status in kb_doc_repo.BUSY_STATUSES:
        return False, f"文档正在{status}，请等待当前任务结束后重试", doc
    return True, "", doc


# ------------------------------ 编辑 ------------------------------

def update_content(doc_id: str, content: str) -> Tuple[bool, str, Dict[str, Any]]:
    """
    保存编辑后的 MD：版本备份 + 更新 content_hash/edit_log。

    只改 MD，不触发重建（重建走 reindex；「保存并重建」由路由串联两调用）。

    :return: (ok, reason, payload)
    """
    allowed, reason, doc = check_mutable(doc_id)
    if not allowed:
        return False, reason, {}
    if not isinstance(content, str):
        return False, "内容格式非法", {}
    if len(content.encode("utf-8")) > MAX_MD_BYTES:
        return False, "内容超过大小上限", {}

    resolved = _resolve_in_kb_root(doc.get("md_path"))
    if resolved is None:
        return False, "文档路径越界，拒绝写入", {}

    try:
        # 版本备份：写前先把当前内容存一份，便于人工回滚
        if resolved.exists():
            backup_dir = resolved.parent / VERSIONS_DIRNAME
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(resolved, backup_dir / f"{stamp}.md")

        resolved.write_text(content, encoding="utf-8")
        new_hash = _sha256(content)
        kb_doc_repo.update_document(
            doc_id,
            {"content_hash": new_hash},
            log_entry={"action": "edit", "note": "MD 已编辑（未重建）"},
        )
        fresh = kb_doc_repo.get_document(doc_id) or doc
        return True, "", {"doc": fresh, "edited_not_reindexed": is_edited_not_reindexed(fresh)}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"保存 MD 失败：doc_id={doc_id}，原因：{e}")
        return False, f"保存失败：{e}", {}


# ------------------------------ reindex ------------------------------

def prepare_reindex(doc_id: str) -> Tuple[bool, str, Dict[str, Any]]:
    """
    reindex 前置：状态守卫 → 置 reindexing → **按登记锚点 item_name 清三存储**。

    返回的 payload 供路由层构造导入 state：
      {"doc": {...}, "item_name": str, "md_path": str, "purge": {...}}

    用**登记时的 item_name**（而非重新识别结果）作清理锚点，防重识别漂移。
    """
    allowed, reason, doc = check_mutable(doc_id)
    if not allowed:
        return False, reason, {}

    resolved = _resolve_in_kb_root(doc.get("md_path"))
    if resolved is None or not resolved.exists():
        return False, "文档正文不存在或路径越界", {}

    anchor_item_name = str(doc.get("item_name") or "")
    if not anchor_item_name:
        return False, "文档缺少 item_name 锚点，无法重建", {}

    # 先置飞行态，防并发二次触发
    kb_doc_repo.update_document(
        doc_id,
        {"status": kb_doc_repo.STATUS_REINDEXING},
        log_entry={"action": "reindex_start", "note": f"锚点 item_name={anchor_item_name}"},
    )

    purge = purge_by_item_name(anchor_item_name)
    if not purge.get("ok"):
        # 清理未完全成功 → 回滚状态，避免留下"半清空"的活跃文档
        kb_doc_repo.update_document(
            doc_id,
            {"status": kb_doc_repo.STATUS_FAILED},
            log_entry={"action": "reindex_failed", "note": "三存储清理未全部成功，已中止"},
        )
        return False, "三存储清理未全部成功，已中止重建（避免脏数据）", {"purge": purge}

    return True, "", {
        "doc": doc,
        "item_name": anchor_item_name,
        "md_path": str(resolved),
        "purge": purge,
    }


def finalize_reindex(doc_id: str, final_state: Optional[Dict[str, Any]], task_id: str) -> bool:
    """
    reindex 收尾：以新 state 回写 indexed_hash / chunk_count / item_name。

    :return: 是否回写成功
    """
    try:
        state = final_state if isinstance(final_state, dict) else {}
        chunks = state.get("chunks") or []
        resolved = _resolve_in_kb_root(str(state.get("md_path") or ""))
        content = _read_text(resolved) if resolved else None
        content_hash = _sha256(content or "")

        patch = {
            "status": kb_doc_repo.STATUS_ACTIVE,
            "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
            # 重建成功 → 当前 MD 内容即为已索引内容
            "indexed_hash": content_hash,
            "content_hash": content_hash,
            "last_reindex_task_id": str(task_id or ""),
        }
        # 重识别可能给出更准的 item_name → 同步登记（后续清理仍以新锚点为准）
        new_item_name = str(state.get("item_name") or "").strip()
        if new_item_name:
            patch["item_name"] = new_item_name

        return kb_doc_repo.update_document(
            doc_id, patch, log_entry={"action": "reindex_done", "note": f"task_id={task_id}"}
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"reindex 收尾回写失败：doc_id={doc_id}，原因：{e}")
        return False


def mark_reindex_failed(doc_id: str, reason: str = "") -> bool:
    """reindex 失败：状态置 failed（indexed_hash 未动 → 可安全重试）。"""
    return kb_doc_repo.update_document(
        doc_id,
        {"status": kb_doc_repo.STATUS_FAILED},
        log_entry={"action": "reindex_failed", "note": str(reason or "")},
    )


# ------------------------------ 启停 / 删除 ------------------------------

def set_enabled(doc_id: str, enabled: bool) -> Tuple[bool, str, Dict[str, Any]]:
    """
    启用 / 停用。

    - disable：清三存储 + status=inactive（检索不再命中），保留 MD 与登记
    - enable：只置回 active（真正的重建由路由串联 reindex）
    """
    allowed, reason, doc = check_mutable(doc_id)
    if not allowed:
        return False, reason, {}

    if not enabled:
        purge = purge_by_item_name(str(doc.get("item_name") or ""))
        if not purge.get("ok"):
            return False, "三存储清理未全部成功，已中止停用", {"purge": purge}
        kb_doc_repo.update_document(
            doc_id,
            {"status": kb_doc_repo.STATUS_INACTIVE},
            log_entry={"action": "disable", "note": "已停用并清三存储"},
        )
        return True, "", {"doc": kb_doc_repo.get_document(doc_id) or {}}

    kb_doc_repo.update_document(
        doc_id,
        {"status": kb_doc_repo.STATUS_ACTIVE},
        log_entry={"action": "enable", "note": "已启用"},
    )
    return True, "", {"doc": kb_doc_repo.get_document(doc_id) or {}}


def delete_document(doc_id: str) -> Tuple[bool, str, Dict[str, Any]]:
    """
    软删：清三存储 + status=deleted，**保留 MD 与登记**（可逆）。
    """
    allowed, reason, doc = check_mutable(doc_id)
    if not allowed:
        return False, reason, {}

    purge = purge_by_item_name(str(doc.get("item_name") or ""))
    if not purge.get("ok"):
        return False, "三存储清理未全部成功，已中止删除", {"purge": purge}

    kb_doc_repo.mark_deleted(doc_id, note=f"软删（锚点 item_name={doc.get('item_name')}）")
    return True, "", {"doc": kb_doc_repo.get_document(doc_id) or {}, "purge": purge}


# ------------------------------ 存量迁移 ------------------------------

def sync_existing_documents() -> Dict[str, Any]:
    """
    存量迁移：扫 output/kb/*/ 下的 .md，用 file_title 反查 Milvus 滤出 item_name。

    - 命中 → 登记 active
    - 未命中 → 登记 inactive（MD 在但向量库里没有 → 待重建）

    :return: {"scanned": n, "registered": n, "inactive": n, "skipped": n, "items": [...]}
    """
    summary = {"scanned": 0, "registered": 0, "inactive": 0, "skipped": 0, "items": []}
    try:
        root = KB_OUTPUT_DIR
        if not root.exists():
            return summary

        for md_file in sorted(root.glob("*/*/*.md")):
            if md_file.parent.name == VERSIONS_DIRNAME:
                continue
            summary["scanned"] += 1
            try:
                file_title = md_file.stem
                item_name = _lookup_item_name_by_file_title(file_title)
                if not item_name:
                    _register_inactive(md_file, file_title)
                    summary["inactive"] += 1
                    summary["items"].append({"md_path": str(md_file), "status": "inactive"})
                    continue

                existing = kb_doc_repo.find_by_item_name(item_name)
                if existing and str(existing.get("md_path")) == str(md_file.resolve()):
                    summary["skipped"] += 1
                    continue

                content_hash = _sha256(_read_text(md_file) or "")
                doc_id = str(existing.get("doc_id")) if existing and existing.get("doc_id") else uuid.uuid4().hex
                kb_doc_repo.upsert_document(
                    {
                        "doc_id": doc_id,
                        "item_name": item_name,
                        "file_title": file_title,
                        "md_path": str(md_file.resolve()),
                        "output_dir": str(md_file.parent.parent),
                        "status": kb_doc_repo.STATUS_ACTIVE,
                        "chunk_count": _count_chunks_by_item_name(item_name),
                        "content_hash": content_hash,
                        "indexed_hash": content_hash,
                        "origin_task_id": md_file.parent.parent.name,
                        "last_reindex_task_id": "",
                        "edit_log": [{"ts": datetime.now().timestamp(), "action": "sync", "note": "存量迁移登记"}],
                    }
                )
                summary["registered"] += 1
                summary["items"].append({"md_path": str(md_file), "status": "active", "item_name": item_name})
            except Exception as e:  # noqa: BLE001
                summary["skipped"] += 1
                logger.warning(f"存量迁移跳过：{md_file}，原因：{e}")
        return summary
    except Exception as e:  # noqa: BLE001
        logger.warning(f"存量迁移失败：原因：{e}")
        return summary


def _register_inactive(md_file: Path, file_title: str) -> bool:
    """未命中向量库的 MD：登记 inactive（供后续手动重建）。"""
    existing = kb_doc_repo.get_document(_sha256(str(md_file))[:32])
    content_hash = _sha256(_read_text(md_file) or "")
    return kb_doc_repo.upsert_document(
        {
            "doc_id": str(existing.get("doc_id")) if existing else _sha256(str(md_file))[:32],
            "item_name": "",
            "file_title": file_title,
            "md_path": str(md_file.resolve()),
            "output_dir": str(md_file.parent.parent),
            "status": kb_doc_repo.STATUS_INACTIVE,
            "chunk_count": 0,
            "content_hash": content_hash,
            "indexed_hash": "",
            "origin_task_id": md_file.parent.parent.name,
            "last_reindex_task_id": "",
            "edit_log": [{"ts": datetime.now().timestamp(), "action": "sync", "note": "存量迁移：未命中向量库"}],
        }
    )


def _lookup_item_name_by_file_title(file_title: str) -> str:
    """用 file_title 反查 Milvus chunks 集合，取任意一条的 item_name（绝不抛）。"""
    try:
        from app.rag.clients.milvus_client import get_milvus_client
        from app.rag.conf.milvus_config import milvus_config
        from app.utils.escape_milvus_string_utils import escape_milvus_string

        collection = milvus_config.chunks_collection
        client = get_milvus_client()
        if not collection or not client.has_collection(collection):
            return ""
        client.load_collection(collection_name=collection)
        rows = client.query(
            collection_name=collection,
            filter=f'file_title=="{escape_milvus_string(file_title)}"',
            output_fields=["item_name"],
            limit=1,
        )
        if rows:
            return str(rows[0].get("item_name") or "")
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"反查 item_name 失败：file_title={file_title}，原因：{e}")
        return ""


def _count_chunks_by_item_name(item_name: str) -> int:
    """统计某 item_name 在 chunks 集合中的条数（绝不抛）。"""
    try:
        from app.rag.clients.milvus_client import get_milvus_client
        from app.rag.conf.milvus_config import milvus_config

        collection = milvus_config.chunks_collection
        client = get_milvus_client()
        if not collection or not client.has_collection(collection):
            return 0
        rows = client.query(
            collection_name=collection,
            filter=f'item_name=="{item_name}"',
            output_fields=["count(*)"],
        )
        if rows:
            return _safe_int(rows[0].get("count(*)"), 0)
        return 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"统计 chunks 失败：item_name={item_name}，原因：{e}")
        return 0


def dumps(obj: Any) -> str:
    """调试用 JSON 序列化（保证中文可读）。"""
    return json.dumps(obj, ensure_ascii=False, default=str)
