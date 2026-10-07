"""
Redis 连接配置
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；缺失 / 空白 / 非法一律回退默认（不抛）"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量；缺失 / 空白 / 非法一律回退默认（不抛）"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class RedisConfig:
    redis_url: str  # 连接地址（含协议、主机、端口、库号）
    max_connections: int  # 连接池上限
    socket_timeout_s: float  # 单次命令读写超时
    connect_timeout_s: float  # 建连超时（取小值以便快速降级）
    health_check_interval_s: int  # 空闲连接探活间隔，防止拿到被服务端关闭的连接


redis_config = RedisConfig(
    redis_url=(os.getenv("REDIS_URL") or "").strip() or DEFAULT_REDIS_URL,
    max_connections=_env_int("REDIS_MAX_CONNECTIONS", 16),
    socket_timeout_s=_env_float("REDIS_SOCKET_TIMEOUT_S", 5.0),
    connect_timeout_s=_env_float("REDIS_CONNECT_TIMEOUT_S", 2.0),
    health_check_interval_s=_env_int("REDIS_HEALTH_CHECK_INTERVAL_S", 30),
)
