"""
LLM 客户端管理器

统一创建和管理 LangChain ChatOpenAI 客户端，按 (模型名, JSON 模式) 缓存实例。
"""

from typing import Optional

from langchain_core.exceptions import LangChainException
from langchain_openai import ChatOpenAI

from app.rag.conf.lm_config import lm_config
from app.core.exceptions import ClientInitError, LLMError
from app.core.logger import logger


class LLMClientManager:
    def __init__(self, lm_config):
        # 保存配置，get() 时按它构造客户端
        self.lm_config = lm_config
        # 缓存：键为 (模型名, JSON 模式)，值为 ChatOpenAI 实例，避免重复构造
        self._cache = {}

    def get(self, model: Optional[str] = None, json_mode: bool = False) -> ChatOpenAI:
        """
        获取（并缓存）ChatOpenAI 客户端

        :param model: 目标模型名；为空时取配置中的默认文本模型
        :param json_mode: 是否要求 JSON 结构化输出
        :return: ChatOpenAI 实例
        :raises ClientInitError: API 关键配置缺失或 LangChain 层构造失败
        """
        # 模型名按「显式入参 → 配置默认 → 兜底常量」优先级确定，保证非空
        target_model = model or self.lm_config.llm_model or "qwen-max"
        cache_key = (target_model, json_mode)

        if cache_key in self._cache:
            logger.debug(f"[LLM客户端] 缓存命中：model={target_model} json_mode={json_mode}")
            return self._cache[cache_key]

        # 关键配置校验前置：把"运行到一半才发现没配 Key"提前到构造时刻
        if not self.lm_config.api_key:
            raise ClientInitError("[LLM客户端] 配置缺失：请在 .env 中配置 OPENAI_API_KEY")
        if not self.lm_config.base_url:
            raise ClientInitError("[LLM客户端] 配置缺失：请在 .env 中配置 OPENAI_BASE_URL")

        # extra_body 为千问等国产模型专属参数（LangChain 透传至 API）；
        # enable_thinking=False 关闭思考链，避免 JSON 模式下混入推理文本导致解析失败
        extra_body = {"enable_thinking": False}
        model_kwargs = {}
        if json_mode:
            model_kwargs["response_format"] = {"type": "json_object"}

        try:
            llm_client = ChatOpenAI(
                model=target_model,
                temperature=self.lm_config.llm_temperature or 0.1,
                api_key=self.lm_config.api_key,
                base_url=self.lm_config.base_url,
                extra_body=extra_body,
                model_kwargs=model_kwargs,
            )
        except LangChainException as e:
            raise ClientInitError(
                f"[LLM客户端] 模型【{target_model}】初始化失败（LangChain 层）", cause=e
            ) from e

        self._cache[cache_key] = llm_client
        logger.info(f"[LLM客户端] 实例已初始化并缓存：model={target_model} json_mode={json_mode}")
        return llm_client


# 全局可复用的 LLM 客户端管理器单例
llm_client_manager = LLMClientManager(lm_config)
