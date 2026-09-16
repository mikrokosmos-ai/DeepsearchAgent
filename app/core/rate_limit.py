"""
通用滑动窗口限流器

供图片摘要（VLM 调用）等有平台侧频率限制的场景使用：在客户端主动节流，
避免触发第三方限流导致请求失败。

注意：队列是模块级全局的，即限流粒度是「进程内所有调用方共享」而非「每个调用方独立」。
这是刻意的——第三方限流按账号统计，共享一个窗口才能真实反映配额占用。
"""

import time
from collections import deque
from typing import Deque

from app.core.logger import logger

# 全局请求时间戳队列：滑动窗口内保留最近 N 次请求的时间点
_GLOBAL_REQUEST_TIMES: Deque[float] = deque()


def apply_api_rate_limit(max_requests: int = 9, window_seconds: int = 60) -> None:
    """
    滑动窗口速率限制

    核心逻辑：维护请求时间戳双端队列，窗口内请求数达上限则阻塞等待到最早一次请求滑出窗口。
    :param max_requests: 窗口内的最大允许请求次数
    :param window_seconds: 滑动窗口时长（秒）
    :return: None，超限时以 sleep 阻塞等待
    """
    current_time = time.time()
    # 1. 清理滑出窗口的过期时间戳，保证队列只反映窗口内的请求数
    while _GLOBAL_REQUEST_TIMES and current_time - _GLOBAL_REQUEST_TIMES[0] >= window_seconds:
        _GLOBAL_REQUEST_TIMES.popleft()

    # 2. 已达上限：等待最早一次请求滑出窗口后再放行
    if len(_GLOBAL_REQUEST_TIMES) >= max_requests:
        sleep_duration = window_seconds - (current_time - _GLOBAL_REQUEST_TIMES[0])
        if sleep_duration > 0:
            logger.debug(
                f"触发API速率限制，窗口{window_seconds}秒内最多{max_requests}次，"
                f"需等待：{sleep_duration:.2f} 秒"
            )
            time.sleep(sleep_duration)
            # 等待期间可能有请求过期，放行前重新清理一次
            current_time = time.time()
            while _GLOBAL_REQUEST_TIMES and current_time - _GLOBAL_REQUEST_TIMES[0] >= window_seconds:
                _GLOBAL_REQUEST_TIMES.popleft()

    # 3. 登记本次请求时间戳
    _GLOBAL_REQUEST_TIMES.append(current_time)
    logger.debug(
        f"API请求时间戳已记录，当前{window_seconds}秒窗口内请求数：{len(_GLOBAL_REQUEST_TIMES)}"
    )
