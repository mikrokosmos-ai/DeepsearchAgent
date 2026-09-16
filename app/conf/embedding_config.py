"""
Embedding 模型配置（BGE-M3）

承载本地权重路径与推理设备。BGE_M3_PATH 直接指向 ModelScope 缓存中的快照目录，
避免运行期回源下载数 GB 权重；BGE_M3 保留仓库标识，供未来切换到在线加载时使用。
按 D2，目标机器有可用 NVIDIA GPU，故默认 cuda:0 + FP16。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class EmbeddingConfig:
    bge_m3_path: str  # 本地模型快照路径（优先使用）
    bge_m3: str  # 模型仓库标识（离线加载时为兜底标识）
    bge_device: str  # 运行设备（cuda:0 / cpu）
    bge_fp16: bool  # 是否启用半精度


embedding_config = EmbeddingConfig(
    bge_m3_path=os.getenv("BGE_M3_PATH"),
    bge_m3=os.getenv("BGE_M3"),
    bge_device=os.getenv("BGE_DEVICE"),
    # .env 中以 1/0 表达布尔，这里兼容常见的数字与字符串写法
    bge_fp16=os.getenv("BGE_FP16", "1") in ("1", "True", "true"),
)
