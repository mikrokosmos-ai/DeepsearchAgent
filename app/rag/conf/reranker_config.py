"""
Reranker 模型配置（BGE-Reranker-Large）

承载 Cross-Encoder 重排模型的本地权重路径与推理设备。
与 Embedding 同理：路径指向 ModelScope 缓存快照，避免重复下载。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class RerankerConfig:
    bge_reranker_large: str  # 本地模型快照路径
    bge_reranker_device: str  # 运行设备（cuda:0 / cpu）
    bge_reranker_fp16: bool  # 是否启用半精度


reranker_config = RerankerConfig(
    bge_reranker_large=os.getenv("BGE_RERANKER_LARGE"),
    bge_reranker_device=os.getenv("BGE_RERANKER_DEVICE"),
    bge_reranker_fp16=os.getenv("BGE_RERANKER_FP16", "1") in ("1", "True", "true"),
)
