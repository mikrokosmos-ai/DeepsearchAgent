"""
FastAPI 应用生命周期管理

负责在服务启动时初始化外部客户端与事件循环绑定，在服务关闭时释放连接资源。

启动阶段做两件事：
    1. 把当前事件循环绑定到 WebSocket 管理器（原 server.py 内联逻辑，迁到此处统一管理）；
    2. 预热外部客户端。

分级策略（D8 核心）：
    本项目原有的 lifespan 只做 WebSocket loop 绑定，不涉及外部依赖。
    引入 RAG 能力后，新增 Milvus / MongoDB / Neo4j / MinIO 四类外部依赖，
    以及 Embedding / Reranker 两个本地大模型。按 D8 决策：

    - **Milvus / MongoDB 降级为「启动期仅告警」**：原实现（EcomKbAgent）把它们
      当必需依赖、失败即阻断启动；但本项目的主链路（DeepAgents + MySQL + Tavily）
      与 RAG 能力是**可分离**的——基础设施没起来时，用户仍应能正常提问与联网检索。
      因此这里改为仅告警，把失败暴露推迟到**首次真正使用 RAG 的那一刻**
      （由各 manager 的 init() 抛 ClientInitError，门面函数触发懒加载兜底）。
      这样既不让服务整体起不来，也不会出现"启动成功但功能静默失效"。

    - **Neo4j / MinIO 本就是可选依赖**：知识图谱/图片上传不可用时，其余检索路
      （Milvus / HyDE / 联网）仍可工作，失败仅告警。

    - **Embedding / Reranker 由 WARMUP_ENABLE 控制**：本地模型加载会占用显存
      且耗时数十秒，默认关闭以保持"首次使用时懒加载"的行为，避免拖慢服务启动。
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
from app.rag.clients.manager.reranker_client_manager import reranker_client_manager
from app.core.logger import logger

# 本地模型预热开关：默认关闭，保持与懒加载行为一致
WARMUP_ENABLE = os.getenv("WARMUP_ENABLE", "false").lower() == "true"

# 启动期「仅告警」的客户端：失败不阻断服务，延迟到首次使用时由门面函数兜底建连
_OPTIONAL_MANAGERS = (
    ("milvus", milvus_client_manager),
    ("mongo", mongo_client_manager),
    ("minio", minio_client_manager),
    ("neo4j", neo4j_client_manager),
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

    # 关闭阶段：统一释放外部连接，避免进程退出前留下未关闭的网络连接
    for name, _mgr in _OPTIONAL_MANAGERS:
        try:
            _mgr.close()
        except Exception as e:
            logger.warning(f"[{name}] 客户端释放时出现异常：{e}")
