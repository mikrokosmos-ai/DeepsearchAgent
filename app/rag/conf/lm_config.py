"""
LLM / VLM 配置

承载大模型与视觉语言模型的调用参数。
与现有 app/agent/llm.py 的 LLM_QWEN_MAX 并存：前者服务于已有多智能体链路，
本配置服务于新增 RAG 链路（图节点内直接调用），两者指向同一组 API 凭据。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class LLMConfig:
    base_url: str  # OpenAI 兼容接口地址
    api_key: str  # API 密钥
    vl_model: str  # 视觉语言模型名（图片摘要用）
    llm_model: str  # 通用文本模型名（RAG 节点用）
    llm_temperature: float  # 采样温度


lm_config = LLMConfig(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    vl_model=os.getenv("VL_MODEL"),
    llm_model=os.getenv("LLM_DEFAULT_MODEL"),
    llm_temperature=float(os.getenv("LLM_DEFAULT_TEMPERATURE", "0.1")),
)
