"""
确权反问的短路登记（两层，都是"任务内"语义）

"""

import re
import threading
from typing import Dict, Optional

# thread_id -> {归一化问题: 反问原文}
_clarify: Dict[str, Dict[str, str]] = {}
# thread_id -> 反问原文
_unresolved: Dict[str, str] = {}
# thread_id -> 占位记录
_inflight: Dict[str, tuple] = {}
# 保护字典操作：任务在事件循环线程重置，工具在 threadpool 线程读写，两者并发访问
_lock = threading.Lock()

# 等待占位者的上限：超时后等待方按「占位者异常/未回填」处理 —— 宁可放行去跑自己的链路，
INFLIGHT_WAIT_TIMEOUT = 60.0

_WS_RE = re.compile(r"\s+")


def normalize_question(question: Optional[str]) -> str:
    """归一化问题文本：去首尾空白 + 连续空白折叠为单个空格（绝不抛）。"""
    try:
        return _WS_RE.sub(" ", str(question or "").strip())
    except Exception:  # noqa: BLE001
        return ""


def mark_short_circuit(
    session_id: Optional[str], question: Optional[str], answer: Optional[str]
) -> bool:
    """
    登记「该问题已被判定为确权反问」，供同问题短路使用（绝不抛）。
    :return: 是否登记成功（参数非法 / 内部异常一律 False）
    """
    key = normalize_question(question)
    text = str(answer or "").strip()
    if not session_id or not key or not text:
        return False
    try:
        with _lock:
            _clarify.setdefault(str(session_id), {})[key] = text
        return True
    except Exception:  # noqa: BLE001
        return False


def get_short_circuit(session_id: Optional[str], question: Optional[str]) -> Optional[str]:
    """查询该问题在本会话内是否已有确权反问（绝不抛；无记录返回 None）。"""
    key = normalize_question(question)
    if not session_id or not key:
        return None
    try:
        with _lock:
            return _clarify.get(str(session_id), {}).get(key)
    except Exception:  # noqa: BLE001
        return None


def mark_unresolved(session_id: Optional[str], answer: Optional[str]) -> bool:
    """
    登记「本任务已发生确权未完成」并保存那条反问，供任务内闸门使用（绝不抛）。

    只保留**首次**的反问原文：后续调用一律返回它，保证用户看到的是同一条问句。
    """
    text = str(answer or "").strip()
    if not session_id or not text:
        return False
    try:
        with _lock:
            _unresolved.setdefault(str(session_id), text)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_unresolved(session_id: Optional[str]) -> Optional[str]:
    """查询本任务是否已发生确权未完成（是则返回当时那条反问原文，绝不抛）。"""
    if not session_id:
        return None
    try:
        with _lock:
            return _unresolved.get(str(session_id))
    except Exception:  # noqa: BLE001
        return None


def acquire_inflight(session_id: Optional[str], wait_timeout: Optional[float] = None):
    """
    闸门占位：返回 ``(是否放行, 反问原文_or_None)``（绝不抛）。
    """
    if not session_id:
        return True, None
    key = str(session_id)
    # 是否由本调用登记占位（True=我是占位者；False=已有占位者，我应当等待）
    is_holder = False
    try:
        with _lock:
            # 已判确权未完成：最常见也最省事的一条，直接复用反问
            done = _unresolved.get(key)
            if done:
                return False, done
            if _inflight.get(key) is None:
                # 第一个到场者：登记占位，随后持锁外去跑链路
                evt = threading.Event()
                slot: Dict[str, object] = {}
                _inflight[key] = (evt, slot)
                is_holder = True
    except Exception:  # noqa: BLE001
        return True, None

    if is_holder:
        # 成功占位：返回放行标志，调用方执行完必须 finish_inflight 回填
        return True, None

    # 已有占位者：等它出结果（超时则放行，宁可多跑一次也不能让整个任务卡死）
    try:
        with _lock:
            record = _inflight.get(key)
            evt = record[0] if record else None
        timeout = INFLIGHT_WAIT_TIMEOUT if wait_timeout is None else float(wait_timeout)
        if evt is not None:
            evt.wait(timeout)
        with _lock:
            resolved = _unresolved.get(key)
        if resolved:
            return False, resolved
        return True, None
    except Exception:  # noqa: BLE001
        return True, None


def finish_inflight(session_id: Optional[str], clarify: Optional[str] = None) -> None:
    """
    回填占位结果并唤醒等待者（绝不抛）。

    :param clarify: 确权未完成的反问原文；传 None / 空串表示「本次正常完成、未开闸」。
    """
    if not session_id:
        return
    key = str(session_id)
    text = str(clarify or "").strip()
    try:
        with _lock:
            record = _inflight.get(key)
            if record is None:
                return
            evt, slot = record
            slot["clarify"] = text or None
            if text:
                # 只保留首次反问，与 mark_unresolved 的 setdefault 语义一致
                _unresolved.setdefault(key, text)
            del _inflight[key]
        evt.set()
    except Exception:  # noqa: BLE001
        pass


def reset_short_circuits(session_id: Optional[str]) -> None:
    """
    清空某会话的短路记录。
    """
    if not session_id:
        return
    try:
        with _lock:
            _clarify.pop(str(session_id), None)
            _unresolved.pop(str(session_id), None)
            # 占位必须一起清：任务收尾时若不唤醒等待者，它们会一直等到超时才放行
            record = _inflight.pop(str(session_id), None)
        if record is not None:
            record[0].set()
    except Exception:  # noqa: BLE001
        pass


def clear_short_circuits(session_id: Optional[str]) -> None:
    """
    任务收尾时清理该会话的短路记录
    """
    reset_short_circuits(session_id)
