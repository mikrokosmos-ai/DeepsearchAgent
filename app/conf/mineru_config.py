"""
MinerU 云端解析服务配置

承载 PDF 解析服务的地址与鉴权令牌。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class MineruConfig:
    base_url: str  # 服务基础地址
    api_key: str  # 鉴权令牌


mineru_config = MineruConfig(
    base_url=os.getenv("MINERU_BASE_URL"),
    api_key=os.getenv("MINERU_API_TOKEN"),
)
