"""
协作式取消（cooperative cancellation）
"""

import threading
from typing import Dict

from app.core.exceptions import AppError


class TaskCancelledError(AppError):
    """
    协作式取消信号。

    继承自 AppError，便于上层统一按业务异常处理；与 asyncio.CancelledError
    区分开 —— 后者是事件循环层的取消，前者是业务层的「用户主动停止」。
    """


# thread_id -> 取消标志（单进程内存态，与 task_utils 的边界一致）
_cancel_flags: Dict[str, threading.Event] = {}
# 保护 _cancel_flags 的字典操作：run_deep_agent 在事件循环线程置位，
# 节点在 threadpool 线程查询，两者并发访问同一字典
_flags_lock = threading.Lock()


def _get_or_create(thread_id: str) -> threading.Event:
    """取出（或新建）指定会话的取消标志。"""
    with _flags_lock:
        flag = _cancel_flags.get(thread_id)
        if flag is None:
            flag = threading.Event()
            _cancel_flags[thread_id] = flag
        return flag


def request_cancel(thread_id: str) -> bool:
    """
    置位取消标志（由 API 层的取消接口调用）。

    :param thread_id: 会话 ID
    :return: 若此前已处于取消态则返回 True（用于提示「重复取消」）
    """
    flag = _get_or_create(thread_id)
    already = flag.is_set()
    flag.set()
    return already


def is_cancelled(thread_id: str) -> bool:
    """
    查询是否已被请求取消（由执行体在节点边界调用）。

    :param thread_id: 会话 ID
    :return: 已请求取消则为 True
    """
    flag = _cancel_flags.get(thread_id)
    return bool(flag and flag.is_set())


def raise_if_cancelled(thread_id: str, where: str = "") -> None:
    """
    已请求取消则抛出 TaskCancelledError，供节点/循环直接调用。

    :param thread_id: 会话 ID
    :param where: 触发位置（写进异常消息，便于日志定位是哪个节点退出的）
    :raises TaskCancelledError: 当该会话已被请求取消
    """
    if is_cancelled(thread_id):
        location = f"（{where}）" if where else ""
        raise TaskCancelledError(f"任务已被用户取消{location}")


def clear_cancel(thread_id: str) -> None:
    """
    清理取消标志（任务结束后调用，避免内存态泄漏）。

    注意：清理后同一 thread_id 再次发起的任务不会继承上一次的取消态，
    这是符合预期的 —— 取消是「针对那一次执行」的。
    """
    with _flags_lock:
        _cancel_flags.pop(thread_id, None)


def reset_cancel(thread_id: str) -> None:
    """清除取消标志位但保留 Event 对象（新任务复用同一 thread_id 时调用）。"""
    flag = _cancel_flags.get(thread_id)
    if flag is not None:
        flag.clear()
