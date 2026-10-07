"""
Redis 客户端管理器

decode_responses 必须保持 False：checkpointer 存取的是 msgpack 字节流，
一旦开启自动解码，二进制数据会被 utf-8 解码破坏。
"""

from typing import Optional

import redis

from app.core.exceptions import ClientInitError
from app.core.logger import logger
from app.rag.conf.redis_config import RedisConfig, redis_config


class RedisClientManager:
    def __init__(self, config: RedisConfig):
        # 保存配置，真正的建连推迟到 init()
        self.redis_config = config
        self.client: Optional[redis.Redis] = None

    def init(self):
        """幂等建连；连不上抛 ClientInitError（是否降级由调用方决定）"""
        if self.client is not None:
            return

        cfg = self.redis_config
        if not cfg.redis_url:
            raise ClientInitError("Redis 配置缺失：请在 .env 中配置 REDIS_URL")

        try:
            client = redis.Redis.from_url(
                cfg.redis_url,
                max_connections=cfg.max_connections,
                socket_timeout=cfg.socket_timeout_s,
                socket_connect_timeout=cfg.connect_timeout_s,
                health_check_interval=cfg.health_check_interval_s,
                decode_responses=False,
            )
            # 主动 ping 一次：只建连不探活的话，"端口开着但服务没就绪"会被误判为可用
            client.ping()
        except Exception as e:
            raise ClientInitError("Redis 建连失败", cause=e) from e

        self.client = client
        logger.info(f"Redis 连接成功：{cfg.redis_url}")

    def close(self):
        """进程退出前释放连接池，避免留下未关闭的网络连接"""
        if self.client is not None:
            try:
                self.client.close()
            except Exception as e:
                # 关闭失败不应影响进程退出流程，仅记录
                logger.warning(f"关闭 Redis 连接时出现异常：{e}")
            self.client = None


# 全局可复用的 Redis 客户端管理器单例
redis_client_manager = RedisClientManager(redis_config)
