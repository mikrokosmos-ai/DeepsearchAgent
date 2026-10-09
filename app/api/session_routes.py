"""
会话（页面）管理接口
"""

import asyncio
import shutil
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query

from app.api.monitor import monitor
from app.core.logger import logger
from app.core.memory import conversation_repo
from app.core.runtime_paths import OUTPUT_DIR, SESSIONS_DIR, UPDATED_SESSIONS_DIR

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

# 与 app/api/server.py 的 _UNSAFE_NAME_CHARS 同源。两边都做路径拼接，各自兜一层：
# 本模块被单独导入时不依赖 server（那会触发整条主智能体装配）。
_UNSAFE_NAME_CHARS = set('\\/:*?"<>|')


def _is_unsafe_identifier(value: str) -> bool:
    """
    路径分隔符 / `..` 穿越片段 / 控制字符 / 首尾空白 一律视为非法标识。

    多层防御的第一层：会话标识先过这一关，后面的路径拼接才不可能越出会话根。
    """
    if not value or value != value.strip():
        return True
    if ".." in value:
        return True
    return any(ch in _UNSAFE_NAME_CHARS or ord(ch) < 32 for ch in value)


def _session_running(session_id: str) -> bool:
    """
    该会话当前是否有活跃任务。

    延迟导入 server：本模块由 server 在 include_router 时导入，模块级导入会成环；
    而请求到达时 server 早已加载完毕，字典读的是同一份内存对象。
    """
    try:
        from app.api import server as server_module

        task = server_module.active_tasks.get(session_id)
        return bool(task and not task.done())
    except Exception:  # noqa: BLE001  读不到活跃任务表时按「没有在跑」处理
        return False


def _remove_session_dir(root: Path, session_id: str) -> str:
    """
    删除 `<root>/session_{id}`，只允许删 root 的直接子目录。

    :return: removed / absent / not_a_dir / out_of_root / failed: ...（供调用方观测）
    """
    try:
        resolved_root = Path(root).resolve()
        target = (resolved_root / f"session_{session_id}").resolve()
    except Exception as e:  # noqa: BLE001
        return f"invalid: {e}"

    if target.parent != resolved_root:
        logger.warning(f"[session] 拒绝删除越界目录：{target}")
        return "out_of_root"
    if not target.exists():
        return "absent"
    if not target.is_dir():
        return "not_a_dir"

    try:
        shutil.rmtree(target)
        return "removed"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[session] 删除目录失败：{target}，原因：{e}")
        return f"failed: {e}"


async def _adelete_checkpoints(session_id: str) -> str:
    """
    删除会话级检查点（短期记忆）。

    用官方 saver 的 `adelete_thread` 而不是自己拼 Redis key：key 结构由 saver 决定，
    手拼 pattern 会在升级时静默失效（删不干净还看不出来）。
    :return: removed / no_deleter / import_failed: ... / failed: ...
    """
    try:
        from app.agent.main_agent import main_agent
    except Exception as e:  # noqa: BLE001
        return f"import_failed: {e}"

    saver = getattr(main_agent, "checkpointer", None)
    deleter = getattr(saver, "adelete_thread", None)
    if deleter is None:
        return "no_deleter"
    try:
        await deleter(session_id)
        return "removed"
    except Exception as e:  # noqa: BLE001  检查点删不干净不该阻断其它三类数据的清理
        logger.warning(f"[session] 删除会话检查点失败：session={session_id}，原因：{e}")
        return f"failed: {e}"


@router.get("")
def list_sessions(
    user_id: str = Query("", max_length=128),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """列出会话索引（按更新时间倒序），侧栏据此渲染「一个会话一条」"""
    sessions = conversation_repo.list_conversations(user_id or None, limit=limit)
    return {"sessions": sessions, "total": len(sessions)}


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    user_id: str = Query("", max_length=128),
) -> Dict[str, Any]:
    """
    按会话删除：一次清掉消息 / 图状态 / 上传附件 / 输出产物四类数据。

    正在执行的会话直接拒绝（409）：任务还在往会话目录与检查点里写，此时删除会留下
    半截状态；先停止再删是唯一可解释的顺序。
    """
    if _is_unsafe_identifier(session_id):
        raise HTTPException(status_code=400, detail="非法会话标识")
    if _session_running(session_id):
        raise HTTPException(status_code=409, detail="该会话正在执行，请先停止再删除")

    # Mongo 与文件系统都是阻塞调用，放线程里跑，避免卡住事件循环
    messages = await asyncio.to_thread(conversation_repo.delete_session, session_id)
    checkpoint = await _adelete_checkpoints(session_id)
    uploads = await asyncio.to_thread(_remove_session_dir, UPDATED_SESSIONS_DIR, session_id)
    output_new = await asyncio.to_thread(_remove_session_dir, SESSIONS_DIR, session_id)
    output_legacy = await asyncio.to_thread(_remove_session_dir, OUTPUT_DIR, session_id)

    logger.warning(
        f"[session] 已删除会话：session={session_id}，user={user_id or '-'}，"
        f"消息={messages}，检查点={checkpoint}，上传附件={uploads}，"
        f"输出目录={output_new}/旧路径={output_legacy}"
    )
    # 让前端实时时间线也能看到这次删除（同一套出口：终端与页面一致）
    monitor.report_custom(
        "session_deleted",
        f"会话已删除：{session_id}",
        {"session_id": session_id, "removed": messages},
    )

    return {
        "status": "deleted",
        "session_id": session_id,
        "removed": {
            **messages,
            "checkpoint": checkpoint,
            "uploads": uploads,
            "output": {"sessions": output_new, "legacy": output_legacy},
        },
    }
