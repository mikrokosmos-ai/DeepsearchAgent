"""
MinIO 对象存储配置

承载 MinerU 解析出的图片的存放位置与访问凭据。
注意：业务 MinIO（9000/9001）与 Milvus 内部 MinIO（9002/9003）是两个独立实例，
      此处只描述业务实例，切勿混配。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class MinIOConfig:
    endpoint: str  # 服务地址（host:port，不含协议）
    access_key: str  # 访问密钥
    secret_key: str  # 密钥
    bucket_name: str  # 存储桶名（知识库文件专用）
    minio_img_dir: str  # 桶内图片目录前缀
    minio_secure: bool  # 是否启用 TLS（决定 http 还是 https）


minio_config = MinIOConfig(
    endpoint=os.getenv("MINIO_ENDPOINT"),
    access_key=os.getenv("MINIO_ACCESS_KEY"),
    secret_key=os.getenv("MINIO_SECRET_KEY"),
    bucket_name=os.getenv("MINIO_BUCKET_NAME"),
    minio_img_dir=os.getenv("MINIO_IMG_DIR"),
    # Minio SDK 只接受布尔值，字符串 "True"/"true" 一律判为启用 TLS
    minio_secure=os.getenv("MINIO_SECURE", "False") in ("1", "True", "true"),
)
