"""
知识库配图的会话级收集器
"""

import threading
from typing import Any, Dict, List, Optional

# 单轮上限：与前端展示上限一致，避免一轮检索记下过多图片
MAX_IMAGES = 12

# thread_id -> 本轮命中的图片地址（按出现顺序、已去重）
_images: Dict[str, List[str]] = {}
_lock = threading.Lock()


def _dedupe_key(url: str) -> str:
    """去重基准：忽略空白与大小写（与前端 mergeImageUrls 同一口径）"""
    return "".join(url.split()).lower()


def record_images(session_id: Optional[str], urls: Any = None) -> bool:
    """
    追加本轮命中的图片地址（去重、截断到上限；绝不抛）。

    :param session_id: 会话 id（thread_id）；为空则不记录
    :param urls: 图片地址列表；非列表或全为空则不记录
    :return: 是否记录成功
    """
    if not session_id or not isinstance(urls, list):
        return False
    try:
        incoming = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
        if not incoming:
            return False
        with _lock:
            current = _images.setdefault(str(session_id), [])
            seen = {_dedupe_key(u) for u in current}
            for url in incoming:
                key = _dedupe_key(url)
                if key in seen:
                    continue
                seen.add(key)
                current.append(url)
                if len(current) >= MAX_IMAGES:
                    break
        return True
    except Exception:  # noqa: BLE001
        return False


def get_images(session_id: Optional[str]) -> List[str]:
    """读取本轮图片地址（返回副本；绝不抛）。"""
    if not session_id:
        return []
    try:
        with _lock:
            return list(_images.get(str(session_id), []))
    except Exception:  # noqa: BLE001
        return []


def reset_images(session_id: Optional[str]) -> None:
    """
    清空某会话的图片（新任务启动前必须调用，绝不抛）。

    不清理会让上一次任务的图片挂到本轮答案上 —— 这种失真没有任何报错提示，
    是最难排查的一类问题（与检索漏斗收集器同理）。
    """
    if not session_id:
        return
    try:
        with _lock:
            _images.pop(str(session_id), None)
    except Exception:  # noqa: BLE001
        pass


def clear_images(session_id: Optional[str]) -> None:
    """任务收尾时清理（与 reset 等价，分开命名以对齐既有模块的调用点意图）。"""
    reset_images(session_id)
