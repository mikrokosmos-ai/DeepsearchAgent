"""
Milvus 向量库配置

承载向量库连接地址与三个集合名（D10 沿用 EcomKbAgent 的 kd_db_* 约定）：
    - chunks_collection       ：文档分块向量，导入链路写入、查询链路检索
    - item_name_collection    ：商品主体名向量，商品名确认/识别用
    - entity_name_collection  ：知识图谱实体名向量，图谱实体对齐用
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class MilvusConfig:
    milvus_url: str  # Milvus 连接地址，必须带 http:// 前缀（pymilvus 3.x 要求）
    chunks_collection: str  # 文档分块集合名
    entity_name_collection: str  # 图谱实体名集合名
    item_name_collection: str  # 商品主体集合名


milvus_config = MilvusConfig(
    milvus_url=os.getenv("MILVUS_URL"),
    chunks_collection=os.getenv("CHUNKS_COLLECTION"),
    entity_name_collection=os.getenv("ENTITY_NAME_COLLECTION"),
    item_name_collection=os.getenv("ITEM_NAME_COLLECTION"),
)
