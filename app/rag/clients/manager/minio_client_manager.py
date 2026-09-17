"""
MinIO 客户端管理器

统一创建和管理 MinIO 客户端，服务于知识库图片的上传与公开访问。
"""

import json
from typing import Optional

from minio import Minio

from app.rag.conf.minio_config import minio_config
from app.core.exceptions import ClientInitError
from app.core.logger import logger


class MinioClientManager:
    def __init__(self, minio_config):
        # 保存配置，init() 时按它建立连接
        self.minio_config = minio_config
        # 声明为 None，真正的连接建立放到 init() 里
        self.client: Optional[Minio] = None

    def _create_minio_client(self) -> Minio:
        return Minio(
            endpoint=self.minio_config.endpoint,
            access_key=self.minio_config.access_key,
            secret_key=self.minio_config.secret_key,
            secure=self.minio_config.minio_secure,
        )

    def _set_bucket_policy(self, bucket_name: str) -> str:
        # 允许所有用户（Principal: "*"）对桶内对象执行只读操作（s3:GetObject）
        # 目的：MinerU 解析出的图片需要在 Markdown 中以前端可直接访问的 URL 引用
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
                }
            ],
        }
        return json.dumps(policy)

    def _create_bucket_ready(self, client: Minio):
        # 桶不存在则创建并设置公开读策略；已存在则跳过，避免覆盖运维侧手动调整的策略
        bucket_name = self.minio_config.bucket_name
        if not client.bucket_exists(bucket_name):
            client.make_bucket(bucket_name)
            client.set_bucket_policy(bucket_name, self._set_bucket_policy(bucket_name))
            logger.info(f"MinIO桶 {bucket_name} 已创建，并设置访问策略")
        else:
            logger.info(f"MinIO桶 {bucket_name} 已存在，无需重复创建")

    def init(self):
        # 幂等：已初始化则直接返回
        if self.client is not None:
            return
        if not self.minio_config.endpoint:
            raise ClientInitError("MinIO 配置缺失：请在 .env 中配置 MINIO_ENDPOINT")
        try:
            client = self._create_minio_client()
            self._create_bucket_ready(client)
            self.client = client
        except Exception as e:
            raise ClientInitError(
                f"MinIO 连接或建桶失败：{self.minio_config.endpoint}", cause=e
            ) from e

    def close(self):
        # MinIO 客户端无显式连接句柄，置空即可释放引用
        self.client = None


# 全局可复用的 MinIO 客户端管理器单例
minio_client_manager = MinioClientManager(minio_config)
