"""
知识库导入接口（D9：入库能力对外暴露）

承接前端「知识导入」页：接收 PDF / Markdown 文件 → 落盘 → 后台执行入库链路
（`kb_import_app`）→ 前端通过轮询与 WebSocket 双通道观察节点进度。

设计说明：
    1. **复用既有通道**：进度走现有 `WS /ws/{thread_id}`（经由 `PipelineEventBridge`
       把入库链路的 SSE 进度转成 `kb_progress` 事件），不新开推送通道；
    2. **状态复用** `app/utils/task_utils.py` 的内存态登记，与对话链路共用同一套
       查询函数（`get_task_status` / `get_done_task_list` / `get_running_task_list`）；
    3. **不阻塞事件循环**：`kb_import_app.invoke` 是同步阻塞调用（MinerU / LLM /
       向量化），放到线程池执行，否则会连带卡住 WebSocket 推送；
    4. **内存态边界**：任务元数据保存在模块级字典中，与 `task_utils` 一致 ——
       仅适用于单进程部署（本项目 uvicorn 默认单 worker），多 worker 需换 Redis/DB。
"""

import asyncio
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api.context import set_thread_context
from app.api.rag_event_bridge import PipelineEventBridge
from app.core.cancel import TaskCancelledError, clear_cancel, request_cancel, reset_cancel
from app.core.logger import logger
from app.core.runtime_paths import KB_IMPORT_DIR, kb_output_dir
from app.rag.pipelines.import_pipeline.graph import kb_import_app
from app.rag.pipelines.import_pipeline.state import create_default_state
from app.utils.task_utils import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_PROCESSING,
    TASK_STATUS_COMPLETED,
    add_done_task,
    add_running_task,
    get_done_task_list,
    get_running_task_list,
    get_task_status,
    update_task_status,
)

# 导入文件独立目录（仓库根下）：与对话链路的 updated/session_* 隔离，便于清理与排查
# 路径统一由 app.core.runtime_paths 提供，业务代码不再自行拼接 PROJECT_ROOT/output
_kb_import_dir = KB_IMPORT_DIR

# 允许的导入文件类型（与前端 UploadDropzone 的校验保持一致）
_ALLOWED_SUFFIXES = {".pdf", ".md", ".markdown"}
# 单文件大小上限（100MB）：超过则拒绝，避免超大文件拖垮解析链路
_MAX_FILE_BYTES = 100 * 1024 * 1024

# task_id -> 任务元数据（单进程内存态，见模块 docstring 第 4 条）
_kb_tasks: Dict[str, Dict[str, Any]] = {}

router = APIRouter(prefix="/api/kb", tags=["kb"])


def _task_payload(task_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """把「task_utils 的实时进度」与「模块内的任务元数据」拼成前端需要的结构"""
    return {
        "task_id": task_id,
        "status": get_task_status(task_id) or TASK_STATUS_PENDING,
        # 中文节点名列表：与前端 lib/nodes.ts 的 IMPORT_STEPS.key 逐字对应
        "done_list": get_done_task_list(task_id),
        "running_list": get_running_task_list(task_id),
        "file_name": meta.get("file_name", ""),
        "file_size": meta.get("file_size", 0),
        "output_dir": meta.get("output_dir", ""),
        "thread_id": meta.get("thread_id", ""),
        "created_at": meta.get("created_at", 0.0),
        "error": meta.get("error", ""),
    }


async def _run_import_task(task_id: str, file_path: Path, thread_id: str) -> None:
    """
    后台执行一次入库链路。

    :param task_id: 任务唯一标识（同时是 SSE 队列 key 与进度登记 key）
    :param file_path: 已落盘的待导入文件绝对路径
    :param thread_id: 发起导入的前端会话 ID，用于把进度事件定向推送到对应 WebSocket
    """
    # 让桥接能定位推送目标：monitor 按 thread_id 定向发送
    if thread_id:
        set_thread_context(thread_id)

    # 统一 local_dir 语义为「本次导入的任务产物目录」output/kb/{task_id}：
    task_output_dir = kb_output_dir(task_id)

    state = create_default_state(
        task_id=task_id,
        local_file_path=str(file_path),
        local_dir=str(task_output_dir),
        # is_stream=True 是 task_utils 推送进度事件的前置开关
        is_stream=True,
    )

    # 清除上一次执行可能残留的取消标志：同一 task_id 不会复用，但保持与对话链路一致
    reset_cancel(task_id)

    try:
        add_running_task(task_id, "upload_file", True)
        add_done_task(task_id, "upload_file", True)

        with PipelineEventBridge(task_id, event_prefix="kb", topic="知识库导入"):
            # 同步阻塞调用放入线程池：不阻塞事件循环，WebSocket 进度才能实时出去
            await asyncio.to_thread(kb_import_app.invoke, state)


        add_done_task(task_id, "__end__", True)
        update_task_status(task_id, TASK_STATUS_COMPLETED)
        logger.info(f"知识库导入完成：task_id={task_id}，file={file_path.name}")
    except TaskCancelledError as e:
        # 协作式取消（用户主动停止）：属正常收尾，不记为失败堆栈
        update_task_status(task_id, TASK_STATUS_FAILED)
        meta = _kb_tasks.get(task_id)
        if meta is not None:
            meta["error"] = str(e)
        logger.info(f"知识库导入已按用户请求取消：task_id={task_id}，{e}")
    except Exception as e:  # noqa: BLE001
        update_task_status(task_id, TASK_STATUS_FAILED)
        meta = _kb_tasks.get(task_id)
        if meta is not None:
            meta["error"] = str(e)
        logger.exception(f"知识库导入失败：task_id={task_id}，file={file_path.name}，原因：{e}")
    finally:
        # 清理取消标志（与 main_agent 的收尾一致，避免内存态累积）
        clear_cancel(task_id)


@router.post("/import")
async def import_kb_files(
    files: List[UploadFile] = File(...),
    thread_id: str = Form(""),
) -> Dict[str, Any]:
    """
    上传文件并触发知识库导入任务。

    每个文件对应一个独立任务：接口只负责落盘与启动后台任务并立即返回，
    真正的解析 / 切分 / 向量化 / 图谱抽取在后台执行。
    """
    if not files:
        raise HTTPException(status_code=400, detail="未收到任何文件")

    accepted: List[Dict[str, Any]] = []
    for upload in files:
        # 只取文件名部分，防止 `../` 之类的路径穿越
        safe_name = Path(upload.filename or "").name
        if not safe_name:
            raise HTTPException(status_code=400, detail="文件名为空")
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型：{safe_name}（仅支持 PDF / Markdown）",
            )

        task_id = uuid.uuid4().hex
        task_dir = _kb_import_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        target_path = task_dir / safe_name

        with target_path.open("wb") as buffer:
            shutil.copyfileobj(upload.file, buffer)

        size = target_path.stat().st_size
        if size > _MAX_FILE_BYTES:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"{safe_name} 超过 {_MAX_FILE_BYTES // (1024 * 1024)}MB 上限",
            )

        _kb_tasks[task_id] = {
            "file_name": safe_name,
            "file_size": size,
            "file_path": str(target_path),
            "output_dir": str(kb_output_dir(task_id)),
            "thread_id": thread_id,
            "created_at": datetime.now().timestamp(),
            "error": "",
        }
        update_task_status(task_id, TASK_STATUS_PROCESSING)
        # 后台任务：不 await，接口立即返回 task_id 供前端跟踪
        asyncio.create_task(_run_import_task(task_id, target_path, thread_id))

        logger.info(f"已受理知识库导入任务：task_id={task_id}，file={safe_name}（{size} bytes）")
        accepted.append(
            {
                "task_id": task_id,
                "file_name": safe_name,
                "file_size": size,
                # 受理即回传「开始上传文件」，前端无需等到第一次轮询就能点亮第一格
                "done_list": ["开始上传文件"],
            }
        )

    return {"status": "accepted", "tasks": accepted}


@router.post("/task/{task_id}/cancel")
async def cancel_kb_task(task_id: str) -> Dict[str, Any]:
    """
    请求取消一个正在进行的知识库导入任务
    """
    meta = _kb_tasks.get(task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    if get_task_status(task_id) in (TASK_STATUS_COMPLETED, TASK_STATUS_FAILED):
        raise HTTPException(status_code=409, detail="任务已结束，无法取消")

    already = request_cancel(task_id)
    update_task_status(task_id, TASK_STATUS_FAILED)
    meta["error"] = meta.get("error") or "用户已取消该导入任务"
    logger.info(f"已请求取消知识库导入任务：task_id={task_id}（重复取消={already}）")
    return {
        "status": "cancelling",
        "task_id": task_id,
        "message": "已请求取消，最多等当前节点执行完毕后停止",
    }


@router.get("/task/{task_id}")
async def get_kb_task(task_id: str) -> Dict[str, Any]:
    """查询单个导入任务的进度（前端轮询入口）"""
    meta = _kb_tasks.get(task_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _task_payload(task_id, meta)


@router.get("/tasks")
async def list_kb_tasks() -> Dict[str, Any]:
    """列出全部导入任务（按创建时间倒序，最新的在前）"""
    items = [_task_payload(task_id, meta) for task_id, meta in _kb_tasks.items()]
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {"tasks": items}


def clear_kb_tasks() -> None:
    """清空任务登记（仅供测试与运维使用，不对外暴露接口）"""
    _kb_tasks.clear()


def get_kb_task_meta(task_id: str) -> Optional[Dict[str, Any]]:
    """读取任务元数据（仅供测试与运维使用）"""
    return _kb_tasks.get(task_id)
