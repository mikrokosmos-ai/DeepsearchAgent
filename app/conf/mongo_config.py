"""
MongoDB 配置

承载会话历史与商品名确认记忆的存储地址。
按 D4，商品名确认链路强依赖会话历史，因此 Mongo 是查询链路的实际必需依赖
（但按 D8，启动期初始化失败仅告警，延迟到首次使用时才显式报错）。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class MongoConfig:
    mongo_url: str  # 连接地址（含协议、主机、端口）
    db_name: str  # 业务库名


mongo_config = MongoConfig(
    mongo_url=os.getenv("MONGO_URL"),
    db_name=os.getenv("MONGO_DB_NAME"),
)
