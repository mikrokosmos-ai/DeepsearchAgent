"""
项目日志模块

基于 loguru 提供统一日志出口，支持通过 .env 控制控制台/文件双输出。
同时提供 node_log / node_guard 两个节点装饰器，为后续 LangGraph 节点提供
统一的「执行耗时记录」与「异常归一化」能力。

设计说明：
    1. 直接使用 loguru 原生的 {name}:{function}:{line} 占位符定位调用点。
       loguru 内部已做了调用栈处理，无需自行遍历 inspect.stack()——后者在每次
       日志写入时都会构建完整帧列表，高频日志下开销显著。
    2. node_guard 与 node_log 的**书写顺序为 node_guard 在外、node_log 在内**：
       Python 装饰器自下而上应用，因此 node_log 先包裹原函数（记录原始堆栈后原样
       re-raise），node_guard 再包裹一层做归一化。顺序写反会导致日志里只看到
       归一化后的 AppError 堆栈，反而丢失根因信息。
"""

import inspect
import os
import sys
import time
from functools import wraps
from typing import Mapping

from dotenv import find_dotenv, load_dotenv
from loguru import logger

# 领域异常基类：node_guard 用它对节点异常做统一归一化。
# 依赖方向：core.exceptions 只依赖标准库，此处不会形成循环导入。
from app.core.exceptions import AppError
from app.core.paths import PROJECT_ROOT

# 显式加载 .env：日志模块可能在应用启动最早期被导入，此时其它模块尚未加载环境变量
load_dotenv(find_dotenv())

LOG_CONSOLE_ENABLE = os.getenv("LOG_CONSOLE_ENABLE", "True").lower() == "true"
LOG_CONSOLE_LEVEL = os.getenv("LOG_CONSOLE_LEVEL", "INFO").upper()
LOG_FILE_ENABLE = os.getenv("LOG_FILE_ENABLE", "True").lower() == "true"
LOG_FILE_LEVEL = os.getenv("LOG_FILE_LEVEL", "INFO").upper()
LOG_FILE_RETENTION = os.getenv("LOG_FILE_RETENTION", "7 days")

LOG_DIR = PROJECT_ROOT / "logs"

# 统一格式：时间 | 级别 | 调用位置 | 消息。name 定宽便于日志列对齐阅读
LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name: <20}</cyan>:<cyan>{function: <15}</cyan>:<cyan>{line: <4}</cyan> - "
    "<level>{message}</level>"
)


def _init_logger():
    """
    初始化全局日志配置

    移除 loguru 默认控制台输出后按 .env 开关重新挂载 sink，
    避免默认 handler 与自定义 handler 重复打印同一行日志。
    :return: 配置完成的 loguru logger 实例
    """
    logger.remove()

    if LOG_CONSOLE_ENABLE:
        logger.add(
            sink=sys.stdout,
            level=LOG_CONSOLE_LEVEL,
            format=LOG_FORMAT,
            colorize=True,
            enqueue=True,
        )

    if LOG_FILE_ENABLE:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger.add(
            sink=str(LOG_DIR / "app_{time:YYYYMMDD}.log"),
            level=LOG_FILE_LEVEL,
            format=LOG_FORMAT,
            # 按自然日切分，便于按天排查问题
            rotation="00:00",
            retention=LOG_FILE_RETENTION,
            encoding="utf-8",
            enqueue=True,
            backtrace=True,
            diagnose=True,
        )

    return logger


logger = _init_logger()


def _trace_id(state) -> str:
    """从 state 中提取本次链路的追踪ID，优先 session_id，其次 task_id"""
    if isinstance(state, Mapping):
        return str(state.get("session_id") or state.get("task_id") or "-")
    return "-"


def _cancel_checkpoint(state, node_name: str) -> None:
    """
    协作式取消检查点
    """
    if not isinstance(state, Mapping):
        return

    from app.core.cancel import is_cancelled

    for key in ("session_id", "task_id"):
        trace = state.get(key)
        if trace and is_cancelled(str(trace)):
            from app.core.cancel import TaskCancelledError

            raise TaskCancelledError(f"任务已被用户取消（在节点 {node_name} 入口检出）")


def node_log(node_name: str):
    """
    节点执行日志装饰器

    自动打印节点的「开始 / 完成（含耗时）/ 异常（含堆栈）」，并按追踪ID聚合，
    让一次请求的节点执行链路在日志里连续可读。不吞异常，保持原有业务语义。

    同步 / 异步双分支（重要）：
        若被装饰函数是协程函数，必须走 async 分支。否则同步 wrapper 调用协程函数
        只会拿到一个 coroutine 对象——既不真正执行，也不会暴露内部异常，
        日志会错误地打印"节点完成"，异常则推迟到调用方 await 时才抛出。
        当前项目节点均为同步函数，此分支为保证后续引入异步节点时不出现静默失效。

    :param node_name: 节点名，用于日志前缀
    :return: 装饰器
    """
    def deco(func):
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(state, *args, **kwargs):
                trace_id = _trace_id(state)
                _cancel_checkpoint(state, node_name)
                start_ts = time.time()
                logger.info(f"[{node_name}] 节点开始，追踪ID={trace_id}")
                try:
                    result = await func(state, *args, **kwargs)
                    cost_ms = int((time.time() - start_ts) * 1000)
                    logger.info(f"[{node_name}] 节点完成，追踪ID={trace_id}，耗时={cost_ms}ms")
                    return result
                except Exception:
                    logger.exception(f"[{node_name}] 节点异常，追踪ID={trace_id}")
                    raise
            return async_wrapper

        @wraps(func)
        def wrapper(state, *args, **kwargs):
            trace_id = _trace_id(state)
            _cancel_checkpoint(state, node_name)
            start_ts = time.time()
            logger.info(f"[{node_name}] 节点开始，追踪ID={trace_id}")
            try:
                result = func(state, *args, **kwargs)
                cost_ms = int((time.time() - start_ts) * 1000)
                logger.info(f"[{node_name}] 节点完成，追踪ID={trace_id}，耗时={cost_ms}ms")
                return result
            except Exception:
                logger.exception(f"[{node_name}] 节点异常，追踪ID={trace_id}")
                raise
        return wrapper
    return deco


def step_log(step_name: str):
    """
    步骤日志装饰器

    与 node_log 的区别：node_log 装饰「节点」入口（LangGraph 的 add_node 注册的
    函数），step_log 装饰节点内部的「步骤」函数（step_1_xxx / step_2_xxx ...），
    让一次节点执行内部的多个步骤在日志里也有清晰的分段。

    自动打印 步骤开始 / 步骤完成（含耗时）/ 步骤异常（含堆栈），不吞异常。
    """
    def deco(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_ts = time.time()
            logger.info(f"[{step_name}] 步骤开始")
            try:
                result = func(*args, **kwargs)
                cost_ms = int((time.time() - start_ts) * 1000)
                logger.info(f"[{step_name}] 步骤完成，耗时={cost_ms}ms")
                return result
            except Exception:
                logger.exception(f"[{step_name}] 步骤异常")
                raise
        return wrapper
    return deco


def node_guard(node_name: str):
    """
    节点异常归一化装饰器

    把节点内抛出的任意异常统一包装为 AppError，并自动带上节点名与根因，
    使上层可以按异常类型分支处理（如 except MilvusError 重试），
    而不必再去匹配错误字符串；同时让"是哪个节点挂的"直接体现在异常对象上。

    幂等：若节点内部已抛出 AppError 则直接透传，不会被二次包装覆盖最初的 node_name。

    使用示例:
        @node_guard("node_rerank")
        @node_log("node_rerank")
        def node_rerank(state):
            ...

    :param node_name: 节点名，会写入被包装异常的 node_name 字段
    :return: 装饰器
    """
    def deco(func):
        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                try:
                    return await func(*args, **kwargs)
                except AppError:
                    raise
                except Exception as e:
                    raise AppError.wrap(e, node_name=node_name) from e
            return async_wrapper

        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except AppError:
                raise
            except Exception as e:
                raise AppError.wrap(e, node_name=node_name) from e
        return wrapper
    return deco
