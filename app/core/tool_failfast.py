"""
工具故障短路（fail-fast）

同一任务内，某工具一旦发生技术故障，后续对该工具的调用直接短路返回，
不再重复执行整条昂贵链路。
"""

import threading
from typing import Dict, Optional

# thread_id -> {tool_name: 首次失败原因}
_failed: Dict[str, Dict[str, str]] = {}
# 保护字典操作：任务在事件循环线程重置，工具在 threadpool 线程读写，两者并发访问
_lock = threading.Lock()

# 失败原因截断长度：避免把超长堆栈塞进工具返回值（返回值会进 LLM 上下文）
_REASON_MAX_LEN = 300


def mark_tool_failed(thread_id: str, tool_name: str, reason: str) -> None:
    """
    标记某工具在本任务内已故障（绝不抛异常）。

    只记录**首次**失败原因：后续重复失败通常是同一根因，保留首个更利于排障。

    :param thread_id: 会话/线程唯一标识（必须是 thread_id，不能用每次调用唯一的 task_id）
    :param tool_name: 工具名，用于支持"只短路出故障的那个工具"
    :param reason: 失败原因（会截断到 _REASON_MAX_LEN 字符）
    """
    if not thread_id or not tool_name:
        return
    try:
        with _lock:
            bucket = _failed.setdefault(str(thread_id), {})
            bucket.setdefault(str(tool_name), str(reason)[:_REASON_MAX_LEN])
    except Exception:  # noqa: BLE001
        # 短路机制自身的失败绝不能影响主链路
        pass


def get_tool_failure(thread_id: str, tool_name: str) -> Optional[str]:
    """
    查询某工具在本任务内是否已故障（绝不抛异常）。

    :return: 已故障则返回首次失败原因；未故障 / 参数非法 / 内部异常则返回 None
    """
    if not thread_id or not tool_name:
        return None
    try:
        with _lock:
            return _failed.get(str(thread_id), {}).get(str(tool_name))
    except Exception:  # noqa: BLE001
        return None


def reset_tool_failures(thread_id: str) -> None:
    """
    清空某会话的工具故障标记（**新任务启动前必须调用**，绝不抛异常）。

    不调用会导致上一次任务的故障标记误杀本次任务的工具调用
    （thread_id 跨天复用，见模块 docstring）。
    """
    if not thread_id:
        return
    try:
        with _lock:
            _failed.pop(str(thread_id), None)
    except Exception:  # noqa: BLE001
        pass


def clear_tool_failures(thread_id: str) -> None:
    """
    任务收尾时清理该会话的标记条目（绝不抛异常）。

    与 reset_tool_failures 行为等价 —— 分开命名只为让两个调用点
    （任务启动 / 任务收尾）的意图在代码里一目了然，与 cancel.py 的
    `reset_cancel` / `clear_cancel` 命名保持对称。
    """
    reset_tool_failures(thread_id)


def get_failed_tools(thread_id: str) -> Dict[str, str]:
    """列出某会话当前所有已故障工具（供日志与调试，返回副本；绝不抛异常）。"""
    try:
        with _lock:
            return dict(_failed.get(str(thread_id), {}))
    except Exception:  # noqa: BLE001
        return {}
