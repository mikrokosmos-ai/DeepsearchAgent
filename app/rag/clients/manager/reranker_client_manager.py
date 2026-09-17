"""
Reranker 客户端管理器

统一创建和管理本地 FlagReranker 重排模型，服务于查询结果的精排。

【P0 修复说明】（对应改造书 R2）
    BGE-Reranker-Large 的 config.json 不含 pad_token_id（该字段是 Qwen3-Reranker
    等模型的约定），而 sentence-transformers 在 batch_size > 1 时会用
    config.pad_token_id 做 padding，取到 None 即抛 ValueError。
    这里在建连后显式把 pad_token_id 对齐为 tokenizer 的 pad token，
    使 batch 打分（而非退化到逐条打分）能够正常工作。

【并发安全说明】（本次修复）
    本机为单卡（8GB）且 embedding / reranker 同卡，两条查询流水线可能同时进入重排。
    原实现有三处隐患，均已在此收口：
      1. init() 的幂等判断是「读 self.client 是否 None」，两个线程可同时通过并重复
         加载同一模型（实测可复现）；现改为双检锁（_init_lock），保证只加载一次。
      2. compute_score 是同步阻塞调用，且 FlagEmbedding 内部存在自适应降 batch 的
         while 循环，一旦不收敛就是永久阻塞；现统一加超时（score_timeout），
         超时抛 RerankError 交由上游降级，而非无限等待。
      3. 单卡上并发推理会互相争抢显存/算力并可能触发上述不收敛分支；现加推理锁
         （_infer_lock）把重排串行化，牺牲少量吞吐换确定性。
"""

import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional

from FlagEmbedding import FlagReranker

from app.rag.conf.reranker_config import reranker_config
from app.core.exceptions import ClientInitError, RerankError
from app.core.logger import logger

# 单次打分的默认批大小：显存友好（FlagEmbedding 默认为 128，8GB 卡上偏大）
DEFAULT_BATCH_SIZE = 8
# 单次打分的默认超时（秒）：需覆盖首次冷加载 + 一批推理
DEFAULT_SCORE_TIMEOUT = 30.0


class RerankerClientManager:
    def __init__(self, reranker_config):
        # 保存配置，init() 时按它加载本地模型
        self.reranker_config = reranker_config
        # 声明为 None，真正的模型加载放到 init() 里
        self.client: Optional[FlagReranker] = None
        # 加载锁：保证并发下模型只被加载一次（原实现无锁，实测可重复加载）
        self._init_lock = threading.Lock()
        # 推理锁：单卡环境下把重排串行化，避免并发争抢触发 FlagEmbedding 的
        # 自适应降 batch 分支不收敛（表现为永久卡住）
        self._infer_lock = threading.Lock()

    def init(self):
        # 幂等：已加载则直接返回（快速路径，无锁读取）
        if self.client is not None:
            return

        # 双检锁：并发调用时只有一个线程真正加载，其余在此等待后直接复用
        with self._init_lock:
            if self.client is not None:
                return
            self._load_model()

    def _load_model(self):
        """真正执行模型加载（仅在持有 _init_lock 时调用）"""
        model_path = self.reranker_config.bge_reranker_large
        if not model_path:
            raise ClientInitError(
                "Reranker 配置缺失：请在 .env 中配置 BGE_RERANKER_LARGE"
            )

        device = self.reranker_config.bge_reranker_device or "cpu"
        fp16 = self.reranker_config.bge_reranker_fp16
        logger.info(
            f"开始初始化 BGE-Reranker：model={model_path} device={device} fp16={fp16}"
        )

        try:
            self.client = FlagReranker(
                model_name_or_path=model_path,
                device=device,
                use_fp16=fp16,
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

    def compute_score(
        self,
        pairs,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout: float = DEFAULT_SCORE_TIMEOUT,
        max_length: Optional[int] = None,
    ):
        """
        对 (query, passage) 对打分（带超时与串行保护）

        :param pairs: [(query, passage), ...] 形式的相关性对
        :param batch_size: 批大小；默认 8（FlagEmbedding 原生默认 128，8GB 卡上偏大）
        :param timeout: 单次打分超时（秒）。超时抛 RerankError，由上游降级为 RRF 原序，
                        避免 FlagEmbedding 内部降 batch 循环不收敛导致的永久阻塞
        :param max_length: 显式最大长度（None 则用模型默认 512）
        :return: 分数列表，与入参顺序一一对应
        :raises RerankError: 打分失败、超时，或返回数量与入参不一致
        """
        if not pairs:
            return []

        if self.client is None:
            self.init()

        # 串行化推理：单卡环境下避免并发争抢导致的不确定行为
        with self._infer_lock:
            return self._compute_score_locked(
                pairs, batch_size=batch_size, timeout=timeout, max_length=max_length
            )

    def _compute_score_locked(
        self,
        pairs,
        batch_size: int,
        timeout: float,
        max_length: Optional[int],
    ):
        """持有推理锁时执行打分；超时单独走线程池，避免阻塞调用无法中断"""
        kwargs = {"batch_size": batch_size}
        if max_length is not None:
            kwargs["max_length"] = max_length

        def _do_score():
            return self.client.compute_score(pairs, **kwargs)

        # 用独立线程池执行：同步的 compute_score 无法被 asyncio 取消，
        # 但可以用 future 超时把「永久卡住」变成「限时失败 + 降级」
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")
        try:
            future = executor.submit(_do_score)
            try:
                scores = future.result(timeout=timeout)
            except FutureTimeoutError:
                logger.error(
                    f"重排打分超时（>{timeout}s，batch_size={batch_size}，"
                    f"候选 {len(pairs)} 条），放弃本轮重排"
                )
                raise RerankError(
                    f"重排打分超时（>{timeout}s）", node_name="rerank"
                )
            except ValueError as e:
                # 兜底：极少数权重（如缺失 pad_token_id 且无 eos 可补）在 batch>1 时会失败，
                # 此时退化为逐条打分，以性能换可用性
                logger.warning(f"批打分失败（batch_size={batch_size}），退化为逐条打分：{e}")
                try:
                    scores = [
                        self.client.compute_score([pair], batch_size=1) for pair in pairs
                    ]
                except Exception as inner:
                    raise RerankError(
                        "重排打分失败（含逐条兜底）", node_name="rerank", cause=inner
                    ) from inner
            except RerankError:
                raise
            except Exception as e:
                raise RerankError("重排打分失败", node_name="rerank", cause=e) from e
        finally:
            # 不等待未完成的线程（超时场景下它会继续跑完，但不再阻塞主流程）
            executor.shutdown(wait=False)

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
