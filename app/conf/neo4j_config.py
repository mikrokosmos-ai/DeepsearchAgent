"""
Neo4j 图数据库配置

承载知识图谱（实体与关系）的连接信息。
database 默认 "neo4j"：Community 版不支持自建库，固定使用该名。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class Neo4jConfig:
    uri: str  # 连接地址（bolt://host:7687）
    username: str  # 认证用户名
    password: str  # 认证密码
    database: str  # 目标数据库名（Community 版固定为 neo4j）


neo4j_config = Neo4jConfig(
    uri=os.getenv("NEO4J_URI"),
    username=os.getenv("NEO4J_USER"),
    password=os.getenv("NEO4J_PASSWORD"),
    database=os.getenv("NEO4J_DATABASE", "neo4j"),
)
