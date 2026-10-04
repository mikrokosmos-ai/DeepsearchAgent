"""
通道级超时工具（

"""

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Tuple

from app.core.logger import logger


def run_with_timeout(
    fn: Callable[[], Any],
    timeout_s: float,
    channel: str,
) -> Tuple[Any, str, float]:
    """
    在独立线程中执行 `fn`，超过 `timeout_s` 则放弃等待。

    :param fn: 无参可调用对象（已绑定好参数的检索调用）
    :param timeout_s: 耗时预算（秒）
    :param channel: 通道名（仅用于日志，如 "embedding" / "kg"）
    :return: (结果, 状态, 真实耗时秒)
             状态取值： "ok" = 正常返回；"timeout" = 超预算放弃；"error" = 抛异常
             超时/异常时结果为 None，调用方按空降级处理。
    """
    started = time.perf_counter()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ch_{channel}")
    try:
        future = executor.submit(fn)
        try:
            result = future.result(timeout=timeout_s)
            return result, "ok", time.perf_counter() - started
        except FuturesTimeoutError:
            elapsed = time.perf_counter() - started
            # 注意：此处不 cancel。底层调用仍在跑 —— 语义是"放弃等待"，不是"中止"。
            logger.warning(
                f"通道[{channel}] 超出耗时预算 {timeout_s}s，按空结果降级"
                f"（真实耗时 {elapsed:.2f}s；底层调用仍在后台跑完，结果丢弃）"
            )
            return None, "timeout", elapsed
        except Exception as e:  # noqa: BLE001
            elapsed = time.perf_counter() - started
            logger.opt(exception=True).warning(
                f"通道[{channel}] 执行异常，按空结果降级（真实耗时 {elapsed:.2f}s）：{e}"
            )
            return None, "error", elapsed
    finally:
        # wait=False：不等底层跑完，否则"放弃等待"会退化成"必须等完"
        # （那就等于超时机制无效 —— 这是最容易写错的一处）
        executor.shutdown(wait=False)
