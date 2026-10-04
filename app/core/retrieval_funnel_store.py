"""
检索漏斗指标的会话级收集器（

"""

import threading
from typing import Any, Dict, Optional

# thread_id -> 最后一次检索的漏斗指标
_funnel: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def record_funnel(session_id: Optional[str], funnel: Any = None) -> bool:
    """
    记录某会话最后一次检索的漏斗指标。

    :param session_id: 会话 id（thread_id）；为空则不记录
    :param funnel: `retrieval_funnel` dict（含 rrf / rerank 两段）；非 dict 或空则忽略
    :return: 是否记录成功（绝不抛）
    """
    if not session_id or not isinstance(funnel, dict) or not funnel:
        return False
    try:
        with _lock:
            _funnel[str(session_id)] = dict(funnel)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_funnel(session_id: Optional[str]) -> Dict[str, Any]:
    """读取某会话最后一次检索的漏斗指标（返回副本；绝不抛）。"""
    if not session_id:
        return {}
    try:
        with _lock:
            return dict(_funnel.get(str(session_id), {}))
    except Exception:  # noqa: BLE001
        return {}


def reset_funnel(session_id: Optional[str]) -> None:
    """
    清空某会话的漏斗指标（新任务启动前必须调用，绝不抛）。

    不清理会让上一次任务的漏斗残留到新任务 —— 而收尾读到的是一份"看似正常"的旧指标，
    这种失真没有任何报错提示，是最难排查的一类问题。
    """
    if not session_id:
        return
    try:
        with _lock:
            _funnel.pop(str(session_id), None)
    except Exception:  # noqa: BLE001
        pass


def clear_funnel(session_id: Optional[str]) -> None:
    """任务收尾时清理该会话漏斗（与 reset 等价，分开命名以对齐既有模块的调用点意图）。"""
    reset_funnel(session_id)
