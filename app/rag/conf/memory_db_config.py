"""
长期记忆独立库连接配置（L3）

只负责「读环境变量并给出连接参数」，不做连接。与业务库刻意分开
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT

DEFAULT_DB_NAME = "deepsearch_memory"
DEFAULT_PORT = 3306
DEFAULT_CHARSET = "utf8mb4"
# 建连超时取小值：MySQL 不可用时快速失败，让主链路立刻降级而不是卡住
DEFAULT_CONNECT_TIMEOUT_S = 5


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；缺失 / 空白 / 非法一律回退默认（不抛）"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MemoryDbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str
    connect_timeout_s: int

    @property
    def configured(self) -> bool:
        """必填项是否齐全 —— 缺配置时调用方应直接跳过长期记忆，而不是抛异常"""
        return all(
            str(value or "").strip()
            for value in (self.host, self.user, self.password, self.database)
        )

    def connect_kwargs(self) -> dict:
        """`mysql.connector.connect` 可直接使用的参数

        刻意**不设 autocommit**：长期记忆的提交依赖短事务与行锁，
        与业务库工具（只读、autocommit=True）的语义相反。
        """
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": self.charset,
            "connection_timeout": self.connect_timeout_s,
        }


def _build() -> MemoryDbConfig:
    return MemoryDbConfig(
        host=(os.getenv("MEMORY_DB_HOST") or "").strip(),
        port=_env_int("MEMORY_DB_PORT", DEFAULT_PORT),
        user=(os.getenv("MEMORY_DB_USER") or "").strip(),
        password=os.getenv("MEMORY_DB_PASSWORD") or "",
        database=(os.getenv("MEMORY_DB_NAME") or "").strip() or DEFAULT_DB_NAME,
        charset=(os.getenv("MEMORY_DB_CHARSET") or "").strip() or DEFAULT_CHARSET,
        connect_timeout_s=_env_int("MEMORY_DB_CONNECT_TIMEOUT_S", DEFAULT_CONNECT_TIMEOUT_S),
    )


memory_db_config = _build()


def build_memory_db_config() -> MemoryDbConfig:
    """按当前环境重建配置（验证脚本用于证明「改一处环境变量，连接参数随之变化」）"""
    return _build()
