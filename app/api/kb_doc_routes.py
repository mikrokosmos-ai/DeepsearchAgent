"""
知识库文档管理接口
承接前端「知识库管理」第三视图：列出 / 查看 / 编辑 / 重建 / 启停 / 删除文档。

"""

import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from app.api.context import set_thread_context
from app.api.rag_event_bridge import PipelineEventBridge
from app.core import kb_doc_service
from app.core.cancel import TaskCancelledError, clear_cancel, reset_cancel
from app.core.logger import logger
from app.core.runtime_paths import kb_output_dir
from app.rag.pipelines.import_pipeline.graph import kb_import_app
from app.rag.pipelines.import_pipeline.state import create_default_state
from app.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    add_done_task,
    add_running_task,
    update_task_status,
)

router = APIRouter(prefix="/api/kb", tags=["kb-doc"])


def _require_doc(doc_id: str) -> Dict[str, Any]:
    doc = kb_doc_repo_get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


def kb_doc_repo_get(doc_id: str) -> Optional[Dict[str, Any]]:
    """薄的转发：便于测试替换与统一异常语义。"""
    from app.rag.repositories import kb_doc_repo

    return kb_doc_repo.get_document(doc_id)


# ------------------------------ 列表 / 详情 ------------------------------

@router.get("/docs")
async def list_kb_docs(
    status: Optional[str] = Query(None, description="按状态过滤"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """列出知识库文档（以 Mongo 登记为准，非内存态任务表）。"""
    result = await asyncio.to_thread(kb_doc_service.list_documents, status, limit, offset)
    return result


@router.get("/docs/{doc_id}/content")
async def get_kb_doc_content(doc_id: str) -> Dict[str, Any]:
    """读取文档正文（路径只从登记取 + 白名单校验）。"""
    result = await asyncio.to_thread(kb_doc_service.get_content, doc_id)
    if result is None:
        raise HTTPException(status_code=404, detail="文档不存在或正文不可读")
    doc = result["doc"]
    return {
        "doc_id": doc_id,
        "content": result["content"],
        "content_hash": doc.get("content_hash", ""),
        "indexed_hash": doc.get("indexed_hash", ""),
        "edited_not_reindexed": kb_doc_service.is_edited_not_reindexed(doc),
    }


@router.get("/docs/{doc_id}/chunks")
async def get_kb_doc_chunks(
    doc_id: str,
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """只读浏览某文档的 chunks（按登记 item_name 查 Milvus）。"""
    result = await asyncio.to_thread(kb_doc_service.get_chunks, doc_id, limit)
    if result is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"doc_id": doc_id, "chunks": result["chunks"]}


# ------------------------------ 编辑 ------------------------------

@router.put("/docs/{doc_id}/content")
async def update_kb_doc_content(
    doc_id: str,
    payload: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """
    保存 MD 正文（版本备份 + hash 更新）。

    请求体：`{"content": "...", "reindex": false}`
    `reindex=true` 时先保存再串联一次重建（「保存并重建」）。
    """
    content = payload.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="缺少 content 字段")

    ok, reason, result = await asyncio.to_thread(kb_doc_service.update_content, doc_id, content)
    if not ok:
        raise _status_for(reason, default=400)

    if bool(payload.get("reindex")):
        reindex_result = await _start_reindex(doc_id)
        return {"status": "saved_and_reindexing", "save": result, "reindex": reindex_result}
    return {"status": "saved", "save": result}


# ------------------------------ reindex / 启停 / 删除 ------------------------------

@router.post("/docs/{doc_id}/reindex")
async def reindex_kb_doc(
    doc_id: str,
    scope: str = Query("full", description="chunks_only | full"),
) -> Dict[str, Any]:
    """触发重建（先按登记锚点清三存储 → 走既有导入链路）。"""
    return await _start_reindex(doc_id, scope=scope)


@router.post("/docs/{doc_id}/disable")
async def disable_kb_doc(doc_id: str) -> Dict[str, Any]:
    """停用：清三存储 + status=inactive（检索不再命中，保留 MD 与登记）。"""
    ok, reason, result = await asyncio.to_thread(kb_doc_service.set_enabled, doc_id, False)
    if not ok:
        raise _status_for(reason, default=400)
    return {"status": "disabled", "doc": result.get("doc", {})}


@router.post("/docs/{doc_id}/enable")
async def enable_kb_doc(doc_id: str) -> Dict[str, Any]:
    """启用 = 一次重建（置 active 后走 reindex，确保三存储与 MD 一致）。"""
    ok, reason, result = await asyncio.to_thread(kb_doc_service.set_enabled, doc_id, True)
    if not ok:
        raise _status_for(reason, default=400)
    return await _start_reindex(doc_id)


@router.delete("/docs/{doc_id}")
async def delete_kb_doc(doc_id: str) -> Dict[str, Any]:
    """软删：清三存储 + status=deleted，保留 MD 与登记（可逆）。"""
    ok, reason, result = await asyncio.to_thread(kb_doc_service.delete_document, doc_id)
    if not ok:
        raise _status_for(reason, default=400)
    return {"status": "deleted", "doc": result.get("doc", {})}


# ------------------------------ 存量迁移 ------------------------------

@router.post("/docs/sync")
async def sync_kb_docs() -> Dict[str, Any]:
    """存量迁移：扫 output/kb/ 登记现有 MD（无命中向量库者标 inactive）。"""
    return await asyncio.to_thread(kb_doc_service.sync_existing_documents)


# ------------------------------ 内部：reindex 执行 ------------------------------

def _status_for(reason: str, default: int = 400) -> HTTPException:
    """把服务层的中文原因映射为 HTTP 状态码。"""
    if "不存在" in reason:
        return HTTPException(status_code=404, detail=reason)
    if "正在" in reason or "单飞" in reason:
        return HTTPException(status_code=409, detail=reason)
    if "越界" in reason:
        return HTTPException(status_code=403, detail=reason)
    return HTTPException(status_code=default, detail=reason)


async def _start_reindex(doc_id: str, scope: str = "full") -> Dict[str, Any]:
    """
    启动一次重建任务。

    前置（同步执行）：状态守卫 → 置 reindexing → 清三存储；失败即回滚返回错误。
    执行（后台）：以**编辑后的 MD** 走既有 kb_import_app（复用导入链路 → 向量/图谱重建）。
    """
    ok, reason, prep = await asyncio.to_thread(kb_doc_service.prepare_reindex, doc_id)
    if not ok:
        raise _status_for(reason, default=400)

    task_id = uuid.uuid4().hex
    md_path = prep["md_path"]
    thread_id = f"kb_reindex_{doc_id[:12]}"

    # 登记重建任务（复用导入进度通道）
    from app.api import kb_routes

    kb_routes._kb_tasks[task_id] = {
        "file_name": Path(md_path).name,
        "file_size": 0,
        "file_path": md_path,
        "output_dir": str(kb_output_dir(task_id)),
        "thread_id": thread_id,
        "created_at": __import__("datetime").datetime.now().timestamp(),
        "error": "",
    }
    update_task_status(task_id, TASK_STATUS_PROCESSING)
    asyncio.create_task(_run_reindex_task(task_id, doc_id, md_path, thread_id, scope))

    return {
        "status": "reindexing",
        "doc_id": doc_id,
        "task_id": task_id,
        "scope": scope,
        "purge": prep.get("purge", {}),
    }


async def _run_reindex_task(task_id: str, doc_id: str, md_path: str, thread_id: str, scope: str) -> None:
    """后台执行重建：以 MD 为输入走导入链路，收尾回写登记。"""
    if thread_id:
        set_thread_context(thread_id)

    task_output_dir = kb_output_dir(task_id)
    state = create_default_state(
        task_id=task_id,
        local_file_path=md_path,
        local_dir=str(task_output_dir),
        # MD 直读路径：跳过 PDF 转换
        is_md_read_enabled=True,
        is_pdf_read_enabled=False,
        is_stream=True,
    )
    reset_cancel(task_id)

    try:
        add_running_task(task_id, "upload_file", True)
        add_done_task(task_id, "upload_file", True)

        with PipelineEventBridge(task_id, event_prefix="kb", topic="知识库重建"):
            final_state = await asyncio.to_thread(kb_import_app.invoke, state)

        kb_doc_service.finalize_reindex(doc_id, final_state, task_id)
        add_done_task(task_id, "__end__", True)
        update_task_status(task_id, TASK_STATUS_COMPLETED)
        logger.info(f"知识库重建完成：doc_id={doc_id}，task_id={task_id}，scope={scope}")
    except TaskCancelledError as e:
        kb_doc_service.mark_reindex_failed(doc_id, reason=str(e))
        update_task_status(task_id, TASK_STATUS_FAILED)
        _set_task_error(task_id, str(e))
        logger.info(f"知识库重建已按用户请求取消：task_id={task_id}，{e}")
    except Exception as e:  # noqa: BLE001
        kb_doc_service.mark_reindex_failed(doc_id, reason=str(e))
        update_task_status(task_id, TASK_STATUS_FAILED)
        _set_task_error(task_id, str(e))
        logger.exception(f"知识库重建失败：doc_id={doc_id}，task_id={task_id}，原因：{e}")
    finally:
        clear_cancel(task_id)


def _set_task_error(task_id: str, message: str) -> None:
    from app.api import kb_routes

    meta = kb_routes._kb_tasks.get(task_id)
    if meta is not None:
        meta["error"] = str(message)
