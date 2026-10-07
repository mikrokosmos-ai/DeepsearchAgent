"""
FastAPI 应用生命周期管理

负责在服务启动时初始化外部客户端与事件循环绑定，在服务关闭时释放连接资源。

启动阶段做三件事：
    1. 把当前事件循环绑定到 WebSocket 管理器（原 server.py 内联逻辑，迁到此处统一管理）；
    2. 预热外部客户端；
    3. 在事件循环里为记忆层 checkpointer 建 RediSearch 索引（官方 saver 要求首次使用前 setup）。

"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.monitor import manager
from app.rag.clients.manager.embedding_client_manager import embedding_client_manager
from app.rag.clients.manager.milvus_client_manager import milvus_client_manager
from app.rag.clients.manager.minio_client_manager import minio_client_manager
from app.rag.clients.manager.mongo_client_manager import mongo_client_manager
from app.rag.clients.manager.neo4j_client_manager import neo4j_client_manager
from app.rag.clients.manager.redis_client_manager import redis_client_manager
from app.rag.clients.manager.reranker_client_manager import reranker_client_manager
from app.core.logger import logger
from app.core.memory.checkpointer import aclose_checkpointer, asetup_checkpointer

# 本地模型预热开关：默认关闭，保持与懒加载行为一致
WARMUP_ENABLE = os.getenv("WARMUP_ENABLE", "false").lower() == "true"

# 启动期「仅告警」的客户端：失败不阻断服务，延迟到首次使用时由门面函数兜底建连
_OPTIONAL_MANAGERS = (
    ("milvus", milvus_client_manager),
    ("mongo", mongo_client_manager),
    ("minio", minio_client_manager),
    ("neo4j", neo4j_client_manager),
    # Redis 是记忆层的热层（checkpointer + 后续的缓存/锁）：它挂掉只应退化成
    # 「记忆不跨进程」，绝不该让服务起不来
    ("redis", redis_client_manager),
)


def _warmup_optional_clients():
    """
    预热可降级的外部客户端

    逐个 try/except 而非批量抛出：任一依赖挂掉都不应影响其余依赖的初始化，
    尽可能让服务以"部分能力可用"的状态启动。
    """
    # 注意：此处不能用 manager 作循环变量名 —— 函数体内任何对 manager 的赋值
    # 都会使 Python 把它判定为局部名称，导致 L78 的模块级 manager 引用抛
    # UnboundLocalError（服务无法启动）。故统一用 _mgr。
    for name, _mgr in _OPTIONAL_MANAGERS:
        try:
            _mgr.init()
            logger.info(f"[{name}] 客户端预热完成")
        except Exception as e:
            logger.warning(f"[{name}] 客户端预热失败，相关功能将在首次使用时重试：{e}")


def _active_checkpointer():
    """
    取主智能体当前挂的 checkpointer（取不到返回 None）

    延迟 import：main_agent 在 import 期就完成装配（含 Redis 能力探针），
    与本模块「只做资源生命周期管理」的定位解耦，也避免模块级 import 的顺序耦合。
    """
    try:
        from app.agent.main_agent import main_agent

        return getattr(main_agent, "checkpointer", None)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 取主智能体 checkpointer 失败，跳过索引初始化：{e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用启动与关闭两个阶段的外部资源"""

    # 绑定当前事件循环到 WebSocket 管理器，确保后台 Agent 任务能把 monitor
    # 事件投递回 FastAPI 所在的 loop（多线程/多 loop 下不绑定会推送到错误的循环）
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    logger.debug(f"[Server] WebSocket Manager bound to loop: {id(loop)}")

    # 启动阶段：可降级依赖仅告警，保证基础设施未就绪时主链路仍可服务
    _warmup_optional_clients()

    # checkpointer 的 RediSearch 索引只能在事件循环里建：官方 asetup 会捕获当前循环，
    # 既用于建索引，也供其同步包装方法回调。失败只告警并降级，不阻断启动。
    checkpointer = _active_checkpointer()
    await asetup_checkpointer(checkpointer)

    if WARMUP_ENABLE:
        # 模型加载是 CPU/GPU 密集型阻塞操作，放到线程池执行，避免卡住事件循环
        for name, _mgr in (
            ("embedding", embedding_client_manager),
            ("reranker", reranker_client_manager),
        ):
            try:
                await asyncio.to_thread(_mgr.init)
                logger.info(f"[{name}] 模型预热完成")
            except Exception as e:
                logger.warning(f"[{name}] 模型预热失败，将在首次使用时重试：{e}")

    # yield 之前是启动逻辑，yield 之后是关闭逻辑；中间阶段由 FastAPI 正常处理请求
    yield

    # 关闭阶段：先释放 checkpointer 自建的异步连接池，再统一释放其余客户端，
    # 避免进程退出前留下未关闭的网络连接
    await aclose_checkpointer(checkpointer)
    for name, _mgr in _OPTIONAL_MANAGERS:
        try:
            _mgr.close()
        except Exception as e:
            logger.warning(f"[{name}] 客户端释放时出现异常：{e}")
