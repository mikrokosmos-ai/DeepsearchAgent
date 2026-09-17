"""
Reranker 客户端管理器

统一创建和管理本地 FlagReranker 重排模型，服务于查询结果的精排。

【P0 修复说明】（对应改造书 R2）
    BGE-Reranker-Large 的 config.json 不含 pad_token_id（该字段是 Qwen3-Reranker
    等模型的约定），而 sentence-transformers 在 batch_size > 1 时会用
    config.pad_token_id 做 padding，取到 None 即抛 ValueError。
    这里在建连后显式把 pad_token_id 对齐为 tokenizer 的 pad token，
    使 batch 打分（而非退化到逐条打分）能够正常工作。
"""

from typing import Optional

from FlagEmbedding import FlagReranker

from app.rag.conf.reranker_config import reranker_config
from app.core.exceptions import ClientInitError, RerankError
from app.core.logger import logger


class RerankerClientManager:
    def __init__(self, reranker_config):
        # 保存配置，init() 时按它加载本地模型
        self.reranker_config = reranker_config
        # 声明为 None，真正的模型加载放到 init() 里
        self.client: Optional[FlagReranker] = None

    def init(self):
        # 幂等：已加载则直接返回
        if self.client is not None:
            return

        model_path = self.reranker_config.bge_reranker_large
        if not model_path:
            raise ClientInitError(
                "Reranker 配置缺失：请在 .env 中配置 BGE_RERANKER_LARGE"
            )

        device = self.reranker_config.bge_reranker_device or "cpu"
        logger.info(
            f"开始初始化 BGE-Reranker：model={model_path} device={device} "
            f"fp16={self.reranker_config.bge_reranker_fp16}"
        )

        try:
            self.client = FlagReranker(
                model_name_or_path=model_path,
                device=device,
                use_fp16=self.reranker_config.bge_reranker_fp16,
            )
        except Exception as e:
            raise ClientInitError(
                f"BGE-Reranker 模型加载失败：{model_path}（device={device}）", cause=e
            ) from e

        self._fix_pad_token_id()
        logger.info("BGE-Reranker 初始化成功")

    def _fix_pad_token_id(self):
        """
        修正 pad_token_id 缺失导致的 batch 打分失败（P0）

        只在真正缺失时补写，避免覆盖模型自身的合法配置。
        """
        try:
            # FlagReranker 内部持有 AutoModel / AutoTokenizer，逐层取到最内层模型对象
            model = getattr(self.client, "model", None)
            tokenizer = getattr(self.client, "tokenizer", None)
            if model is None or tokenizer is None:
                return

            config = getattr(model, "config", None)
            if config is None or getattr(config, "pad_token_id", None) is not None:
                return

            pad_token_id = getattr(tokenizer, "pad_token_id", None)
            if pad_token_id is None:
                pad_token_id = getattr(tokenizer, "eos_token_id", None)
            if pad_token_id is None:
                logger.warning(
                    "Reranker 的 tokenizer 未提供 pad_token_id / eos_token_id，"
                    "batch 打分可能仍会失败"
                )
                return

            config.pad_token_id = pad_token_id
            logger.info(f"已修正 Reranker 的 pad_token_id = {pad_token_id}（P0 修复）")
        except Exception as e:
            # 修正失败不阻断初始化：后续按 batch_size=1 逐条打分仍可用，
            # 只是损失批处理性能，故记录告警而非抛出
            logger.warning(f"修正 Reranker pad_token_id 时出现异常，将按单条打分兜底：{e}")

    def close(self):
        # 释放模型引用（显存回收交由 GC 与 torch 管理）
        self.client = None

    def compute_score(self, pairs, batch_size: int = 8):
        """
        对 (query, passage) 对打分

        :param pairs: [(query, passage), ...] 形式的相关性对
        :param batch_size: 批大小；模型不支持批处理时由调用方降为 1
        :return: 分数列表，与入参顺序一一对应
        :raises RerankError: 打分失败或返回数量与入参不一致
        """
        if not pairs:
            return []

        if self.client is None:
            self.init()

        try:
            scores = self.client.compute_score(pairs, batch_size=batch_size)
        except ValueError as e:
            # 兜底：极少数权重（如缺失 pad_token_id 且无 eos 可补）在 batch>1 时会失败，
            # 此时退化为逐条打分，以性能换可用性
            logger.warning(f"批打分失败（batch_size={batch_size}），退化为逐条打分：{e}")
            try:
                scores = [self.client.compute_score([pair], batch_size=1) for pair in pairs]
            except Exception as inner:
                raise RerankError("重排打分失败（含逐条兜底）", node_name="rerank", cause=inner) from inner
        except Exception as e:
            raise RerankError("重排打分失败", node_name="rerank", cause=e) from e

        # 单条打分时 FlagReranker 可能返回标量而非列表，统一为列表以稳定调用方契约
        if not isinstance(scores, list):
            scores = [scores]

        if len(scores) != len(pairs):
            raise RerankError(
                f"重排分数数量与候选数不一致：候选 {len(pairs)} 条，分数 {len(scores)} 条",
                node_name="rerank",
            )
        return scores


# 全局可复用的 Reranker 客户端管理器单例
reranker_client_manager = RerankerClientManager(reranker_config)
