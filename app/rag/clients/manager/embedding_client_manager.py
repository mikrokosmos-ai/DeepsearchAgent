"""
Embedding 客户端管理器

统一创建和管理本地 BGE-M3 混合向量模型，服务于稠密 + 稀疏向量的生成。
"""

from typing import Optional

from pymilvus.model.hybrid import BGEM3EmbeddingFunction

from app.rag.conf.embedding_config import embedding_config
from app.core.exceptions import ClientInitError, EmbeddingError
from app.core.logger import logger


class EmbeddingClientManager:
    def __init__(self, embedding_config):
        # 保存配置，init() 时按它加载本地模型
        self.embedding_config = embedding_config
        # 声明为 None，真正的模型加载放到 init() 里（显存开销大，需延迟到用时）
        self.client: Optional[BGEM3EmbeddingFunction] = None

    def init(self):
        # 幂等：已加载则直接返回，避免重复加载模型
        if self.client is not None:
            return

        # 优先用本地快照路径，缺失时回退到仓库标识（由下层自行决定是否联网下载）
        model_name = self.embedding_config.bge_m3_path or self.embedding_config.bge_m3
        device = self.embedding_config.bge_device or "cpu"
        use_fp16 = self.embedding_config.bge_fp16 or False

        if not model_name:
            raise ClientInitError(
                "Embedding 配置缺失：请在 .env 中配置 BGE_M3_PATH 或 BGE_M3"
            )

        logger.info(
            f"开始初始化 BGE-M3：model={model_name} device={device} fp16={use_fp16}"
        )

        try:
            # normalize_embeddings=True：模型侧做 L2 归一化，配合 Milvus 的 IP（内积）度量
            # 即可等价于余弦相似度，省去检索期再做一次归一化
            self.client = BGEM3EmbeddingFunction(
                model_name=model_name,
                device=device,
                use_fp16=use_fp16,
                normalize_embeddings=True,
            )
            logger.info("BGE-M3 初始化成功（已开启 L2 归一化，适配 IP 内积检索）")
        except Exception as e:
            raise ClientInitError(
                f"BGE-M3 模型加载失败：{model_name}（device={device}）", cause=e
            ) from e

    def close(self):
        # 释放模型引用（显存回收交由 GC 与 torch 管理）
        self.client = None

    def encode(self, texts):
        """
        为文本列表生成稠密 + 稀疏混合向量

        :param texts: 待向量化文本列表，单条文本也需封装为列表
        :return: {"dense": list[list[float]], "sparse": list[dict[int, float]]}
        :raises EmbeddingError: 入参非法，或模型输出结构不符合预期
        """
        if not isinstance(texts, list) or len(texts) == 0:
            raise EmbeddingError("参数 texts 必须是非空列表", node_name="embedding.encode")

        # 懒加载兜底：未经 lifespan 预热时，首次调用自动加载模型
        if self.client is None:
            self.init()

        logger.info(f"开始为 {len(texts)} 条文本生成混合向量")

        try:
            embeddings = self.client.encode_documents(texts)
        except Exception as e:
            raise EmbeddingError(
                f"BGE-M3 向量化失败（{len(texts)} 条文本）", node_name="embedding.encode", cause=e
            ) from e

        # 模型返回的 sparse 是整批共享的 CSR 矩阵，需按 indptr 切段拆成"每条文本一个 dict"
        sparse_matrix = embeddings["sparse"]
        processed_sparse = []
        for i in range(len(texts)):
            start, end = sparse_matrix.indptr[i], sparse_matrix.indptr[i + 1]
            indices = sparse_matrix.indices[start:end].tolist()
            data = sparse_matrix.data[start:end].tolist()
            processed_sparse.append({k: v for k, v in zip(indices, data)})

        result = {
            "dense": [emb.tolist() for emb in embeddings["dense"]],
            "sparse": processed_sparse,
        }

        # 条数一致性校验：dense/sparse 任一与入参数量不符都说明模型输出异常，
        # 此时若继续写库会造成向量与文本错位，故在边界处直接失败
        if len(result["dense"]) != len(texts) or len(result["sparse"]) != len(texts):
            raise EmbeddingError(
                f"向量化结果数量与输入不一致：输入 {len(texts)} 条，"
                f"dense {len(result['dense'])} 条，sparse {len(result['sparse'])} 条",
                node_name="embedding.encode",
            )

        logger.info(f"{len(texts)} 条文本向量生成完成")
        return result


# 全局可复用的 Embedding 客户端管理器单例
embedding_client_manager = EmbeddingClientManager(embedding_config)
