"""
查询链路可调参数配置（对应 query pipeline 各节点）

说明：
    1. 本文件只承载"业务可调参数"，不承载连接信息（连接类配置见 milvus_config / lm_config 等）；
    2. 每个字段均可用同名环境变量覆盖；
    3. 字段与节点的对应关系写在每个字段的行内注释里，便于反查；
    4. 历史对话预算（HISTORY_BUDGET）不在此声明：它是
       「总预算 - 本地证据 - 联网证据」推导出来的，派生而非独立配置，
       这样可从构造上保证"三段之和不超总预算"这一不变式。
"""

import os
from dataclasses import dataclass

from app.core.exceptions import ConfigurationError
from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class QueryPipelineConfig:
    """查询链路可调参数"""

    # ==================== 重排与动态截断（node_rerank）====================
    rerank_max_topk: int  # 动态 TopK 硬上限
    rerank_min_topk: int  # 动态 TopK 下限：断崖截断后至少保留的条数
    rerank_min_local_keep: int  # 分区保底：本地（milvus）切片至少保留条数（0 = 关闭）
    rerank_gap_ratio: float  # 断崖检测：相对落差阈值
    rerank_gap_abs: float  # 断崖检测：绝对分差阈值
    # 证据闸门：整批最高精排分的绝对下限。低于该值 → 整批判定为"无关噪声"并丢弃。
    # 0 = 关闭闸门（默认，零行为变化）。BGE-reranker 是 logit 域，阈值须用标定脚本实测，
    # 不要照抄其它项目的数值。
    evidence_min_score: float

    # ==================== 多路融合（node_rrf）====================
    rrf_k: int  # RRF 平滑参数，削弱排名影响
    # 三段预算之二（融合保留数）：RRF 融合后保留条数。
    # 必须显著大于最终 topK，否则 rerank 无候选可淘汰，退化成"排序器"。
    rrf_top: int

    # ==================== 商品名确认（node_item_name_confirm）====================
    item_name_high_threshold: float  # 高置信阈值，达到即确认
    item_name_mid_threshold: float  # 中置信下限，介于两者之间转"待用户确认"
    item_name_max_options: int  # 待确认时最多给出的候选数量
    item_name_match_dense_weight: float  # 商品名向量匹配：稠密向量权重
    item_name_match_sparse_weight: float  # 商品名向量匹配：稀疏向量权重

    # ==================== 切片检索（node_search_embedding / node_search_embedding_hyde）====================
    chunk_search_dense_weight: float  # 切片混合检索：稠密向量权重
    chunk_search_sparse_weight: float  # 切片混合检索：稀疏向量权重
    # 三段预算之一（召回扇出基数）：单路检索返回条数。
    # 与 rrf_top（融合保留）/ rerank_max_topk（成本天花板）构成"召回要大、最终要精"的漏斗。
    chunk_search_limit: int

    # ==================== 答案生成（node_answer_output）====================
    max_context_chars: int  # 上下文总字符预算
    local_evidence_budget: int  # 本地知识库证据区预算
    web_evidence_budget: int  # 联网证据区预算

    # ==================== 通道级超时（4 路召回节点共用，T4）====================
    # 单路检索通道的耗时预算（秒）。超预算 → 该路按空结果降级，其余各路照常融合。
    # 取值原则：显著大于 P99 正常耗时 —— 超时只"放弃等待"而不"中止底层调用"，
    # 设得太小等于整路白算（底层仍占着资源跑完，结果却被丢弃）。
    channel_timeout_s: float

    # ==================== 联网搜索（node_web_search_mcp）====================
    web_search_count: int  # MCP 联网搜索返回条数

    # ==================== 知识图谱检索（node_query_kg / node_rrf / node_answer_output）====================
    kg_max_seed_candidates: int  # 实体对齐阶段最多取多少个种子实体
    kg_max_total_triples: int  # 一跳扩展返回的三元组上限
    rrf_kg_weight: float  # RRF 融合时图谱路的权重（其余两路恒为 1.0）
    kg_evidence_budget: int  # 图谱证据区预算（从 LOCAL_EVIDENCE_BUDGET 中划分，不额外挤占总预算）

    # ==================== 兜底召回（node_search_embedding_fallback / node_rrf / node_rerank）====================
    # 补充路：不加 item_name 过滤做一次全库混合检索，给"确权选错主体"场景兜底。
    fallback_search_limit: int  # 补充路召回条数（份额）；只取固定条数，防止全库检索放大耗时
    fallback_min_keep: int  # 补充路最小保留条数（0 = 关闭，纯靠 cross-encoder 竞争决定去留）
    rrf_fallback_weight: float  # 补充路在 RRF 融合中的权重（与 embedding/hyde 恒 1.0、kg 0.7 并列）


query_pipeline_config = QueryPipelineConfig(
    # ---- 重排与动态截断 ----
    rerank_max_topk=int(os.getenv("RERANK_MAX_TOPK", "10")),
    # 默认 3：断崖截断触发时若下限为 1，实测会把本地证据全部砍掉
    # （联网首条高分远超本地 → gap 超阈 → topk=1），故下限抬到 3
    rerank_min_topk=int(os.getenv("RERANK_MIN_TOPK", "3")),
    rerank_gap_ratio=float(os.getenv("RERANK_GAP_RATIO", "0.25")),
    rerank_gap_abs=float(os.getenv("RERANK_GAP_ABS", "0.5")),
    # 默认 0 = 闸门关闭：这是本项改造的"零行为变化"保证 ——
    # 标定脚本产出建议阈值后，由运维侧写入 EVIDENCE_MIN_SCORE 才真正生效。
    evidence_min_score=float(os.getenv("EVIDENCE_MIN_SCORE", "0")),
    # 默认 3：cross-encoder 是全局同池打分（本地切片与联网结果一起排序），
    # 实测本地的短句证据会被高分网页挤出最终证据，故断崖截断后按来源补足本地切片
    rerank_min_local_keep=int(os.getenv("RERANK_MIN_LOCAL_KEEP", "3")),
    # ---- 多路融合 ----
    rrf_k=int(os.getenv("RRF_K", "60")),
    # 默认 10：与 rerank_max_topk(10) 相当，使融合后的候选池显著大于最终证据数，
    # 让 cross-encoder 真正承担"淘汰"职责（旧值 5 < max_topk 10 → 漏斗方向反了）。
    rrf_top=int(os.getenv("RRF_TOP", "10")),
    # ---- 商品名确认 ----
    item_name_high_threshold=float(os.getenv("ITEM_NAME_HIGH_THRESHOLD", "0.65")),
    item_name_mid_threshold=float(os.getenv("ITEM_NAME_MID_THRESHOLD", "0.50")),
    item_name_max_options=int(os.getenv("ITEM_NAME_MAX_OPTIONS", "2")),
    item_name_match_dense_weight=float(os.getenv("ITEM_NAME_MATCH_DENSE_WEIGHT", "0.5")),
    item_name_match_sparse_weight=float(os.getenv("ITEM_NAME_MATCH_SPARSE_WEIGHT", "0.5")),
    # ---- 切片检索 ----
    chunk_search_dense_weight=float(os.getenv("CHUNK_SEARCH_DENSE_WEIGHT", "0.8")),
    chunk_search_sparse_weight=float(os.getenv("CHUNK_SEARCH_SPARSE_WEIGHT", "0.2")),
    # 默认 12：单路召回基数。三路融合去重后候选池约 15~25 条，
    # 显著大于 rerank_max_topk(10)，为断崖截断与分区保底留出筛选空间。
    chunk_search_limit=int(os.getenv("CHUNK_SEARCH_LIMIT", "12")),
    # ---- 答案生成 ----
    max_context_chars=int(os.getenv("MAX_CONTEXT_CHARS", "12000")),
    local_evidence_budget=int(os.getenv("LOCAL_EVIDENCE_BUDGET", "8000")),
    web_evidence_budget=int(os.getenv("WEB_EVIDENCE_BUDGET", "2500")),
    # ---- 通道级超时（4 路召回共用）----
    # 默认 20s：本地 Milvus 检索通常亚秒级、Neo4j 一跳扩展也是毫秒级，
    # 20s 已是 P99 的数十倍 —— 能拦住的只有"真挂起"，不会误杀正常慢查询。
    channel_timeout_s=float(os.getenv("CHANNEL_TIMEOUT_S", "20")),
    # ---- 联网搜索 ----
    web_search_count=int(os.getenv("WEB_SEARCH_COUNT", "10")),
    # ---- 知识图谱检索 ----
    kg_max_seed_candidates=int(os.getenv("KG_MAX_SEED_CANDIDATES", "3")),
    kg_max_total_triples=int(os.getenv("KG_MAX_TOTAL_TRIPLES", "50")),
    rrf_kg_weight=float(os.getenv("RRF_KG_WEIGHT", "0.7")),
    kg_evidence_budget=int(os.getenv("KG_EVIDENCE_BUDGET", "2000")),
    # ---- 兜底召回 ----
    # 默认 3：主路 chunk_search_limit(12) 的 1/4，先小值观测存活率，用存活率决定是否放大。
    fallback_search_limit=int(os.getenv("FALLBACK_SEARCH_LIMIT", "3")),
    # 默认 0 = 关闭：补充路证据先纯靠 cross-encoder 同池竞争，不强行保底（避免误放噪声）。
    fallback_min_keep=int(os.getenv("FALLBACK_MIN_KEEP", "0")),
    # 默认 1.0：与 embedding/hyde 同权，同池竞争；最终去留由 cross-encoder 分数决定。
    rrf_fallback_weight=float(os.getenv("RRF_FALLBACK_WEIGHT", "1.0")),
)


def _validate_query_pipeline_config(cfg: QueryPipelineConfig) -> None:
    """
    校验查询链路配置的合法性（fail-fast）

    设计意图：
        与导入链路同理——参数之间存在隐含约束（如 TopK 上下限必须有序、
        证据区预算之和不能超过总预算），配置矛盾时应在启动阶段明确报错，
        而不是让检索结果静默劣化。

    注意：默认值天然满足下列全部约束，因此只会拦住"人为改坏"的配置。
    :param cfg: 待校验的查询链路配置
    :raises ConfigurationError: 任意一条不变式被破坏时抛出（错误信息含环境变量名与当前值）
    """
    # 1. TopK 上下限必须有序，且下限至少为 1（保证无论如何都保留结果）
    if cfg.rerank_min_topk < 1:
        raise ConfigurationError(f"配置非法：RERANK_MIN_TOPK({cfg.rerank_min_topk}) 必须大于等于 1")
    if cfg.rerank_min_topk > cfg.rerank_max_topk:
        raise ConfigurationError(
            f"配置冲突：RERANK_MIN_TOPK({cfg.rerank_min_topk}) 不能大于 "
            f"RERANK_MAX_TOPK({cfg.rerank_max_topk})"
        )
    # 1.1 分区保底条数必须非负，且不超过 TopK 上限（否则保底永远无法满足）
    if cfg.rerank_min_local_keep < 0:
        raise ConfigurationError(
            f"配置非法：RERANK_MIN_LOCAL_KEEP({cfg.rerank_min_local_keep}) 不能为负数（0 表示关闭分区保底）"
        )
    if cfg.rerank_min_local_keep > cfg.rerank_max_topk:
        raise ConfigurationError(
            f"配置冲突：RERANK_MIN_LOCAL_KEEP({cfg.rerank_min_local_keep}) 不能大于 "
            f"RERANK_MAX_TOPK({cfg.rerank_max_topk})"
        )
    # 2. 断崖阈值必须为非负
    if cfg.rerank_gap_ratio < 0 or cfg.rerank_gap_abs < 0:
        raise ConfigurationError(
            f"配置非法：RERANK_GAP_RATIO({cfg.rerank_gap_ratio}) 与 RERANK_GAP_ABS({cfg.rerank_gap_abs}) "
            f"不能为负数"
        )
    # 2.1 证据闸门：阈值必须非负。阈值 > 0 时要求 rerank 可用 ——
    #     闸门判据取自精排分，rerank 恒降级时无分可读、闸门语义不确定。
    #     本项目 rerank 常开（无开关配置项），故该互斥天然满足；
    #     这里只保留非负校验，并把这个前提写进注释以免未来加开关时漏改。
    if cfg.evidence_min_score < 0:
        raise ConfigurationError(
            f"配置非法：EVIDENCE_MIN_SCORE({cfg.evidence_min_score}) 不能为负数（0 表示关闭闸门）"
        )
    # 3. 证据区预算之和不能超过总预算，否则历史对话预算会变成负数而被静默截断
    evidence_sum = cfg.local_evidence_budget + cfg.web_evidence_budget
    if evidence_sum > cfg.max_context_chars:
        raise ConfigurationError(
            f"配置冲突：LOCAL_EVIDENCE_BUDGET({cfg.local_evidence_budget}) + "
            f"WEB_EVIDENCE_BUDGET({cfg.web_evidence_budget}) = {evidence_sum} "
            f"不能大于 MAX_CONTEXT_CHARS({cfg.max_context_chars})，"
            f"否则历史对话区预算将为负数"
        )
    # 4. 商品名置信度区间必须有序且在 [0, 1] 内，否则确认/待确认分支永远不会命中
    if not 0.0 <= cfg.item_name_mid_threshold <= cfg.item_name_high_threshold <= 1.0:
        raise ConfigurationError(
            f"配置冲突：需满足 0 <= ITEM_NAME_MID_THRESHOLD({cfg.item_name_mid_threshold}) <= "
            f"ITEM_NAME_HIGH_THRESHOLD({cfg.item_name_high_threshold}) <= 1"
        )
    # 5. 条数类参数必须为正
    if cfg.rrf_top <= 0:
        raise ConfigurationError(f"配置非法：RRF_TOP({cfg.rrf_top}) 必须大于 0")
    if cfg.item_name_max_options <= 0:
        raise ConfigurationError(f"配置非法：ITEM_NAME_MAX_OPTIONS({cfg.item_name_max_options}) 必须大于 0")
    if cfg.chunk_search_limit <= 0:
        raise ConfigurationError(f"配置非法：CHUNK_SEARCH_LIMIT({cfg.chunk_search_limit}) 必须大于 0")
    if cfg.web_search_count <= 0:
        raise ConfigurationError(f"配置非法：WEB_SEARCH_COUNT({cfg.web_search_count}) 必须大于 0")
    # 5.1 检索漏斗三段预算的方向不变式：
    #     ① rrf_top 不能超过三路召回基数之和 + 补充路份额（宽松上界）——超过则融合"永远取不满"，
    #        说明参数互相矛盾，应显式报错而不是让检索结果静默劣化；
    #     ② 最终证据池 = RRF 池 + 联网池，故 rerank_max_topk 不能超过二者之和，
    #        否则意味着"配置期望的证据数大于所有来源能提供的总量"。
    three_route_capacity = 3 * cfg.chunk_search_limit + cfg.fallback_search_limit
    if cfg.rrf_top > three_route_capacity:
        raise ConfigurationError(
            f"配置冲突：RRF_TOP({cfg.rrf_top}) 不能大于三路召回容量 + 补充路份额 "
            f"3 * CHUNK_SEARCH_LIMIT({cfg.chunk_search_limit}) + "
            f"FALLBACK_SEARCH_LIMIT({cfg.fallback_search_limit}) = {three_route_capacity}"
        )
    merged_capacity = cfg.rrf_top + cfg.web_search_count
    if cfg.rerank_max_topk > merged_capacity:
        raise ConfigurationError(
            f"配置冲突：RERANK_MAX_TOPK({cfg.rerank_max_topk}) 不能大于 "
            f"RRF_TOP({cfg.rrf_top}) + WEB_SEARCH_COUNT({cfg.web_search_count}) = {merged_capacity}"
            f"（最终证据池 = RRF 池 + 联网池）"
        )
    # 5.2 通道级超时必须为正（0 或负数会让每一路都立即超时 → 检索恒为空）
    if cfg.channel_timeout_s <= 0:
        raise ConfigurationError(
            f"配置非法：CHANNEL_TIMEOUT_S({cfg.channel_timeout_s}) 必须大于 0"
        )
    # 6. 检索权重必须为非负（Milvus WeightedRanker 不接受负权重）
    for name, value in (
        ("ITEM_NAME_MATCH_DENSE_WEIGHT", cfg.item_name_match_dense_weight),
        ("ITEM_NAME_MATCH_SPARSE_WEIGHT", cfg.item_name_match_sparse_weight),
        ("CHUNK_SEARCH_DENSE_WEIGHT", cfg.chunk_search_dense_weight),
        ("CHUNK_SEARCH_SPARSE_WEIGHT", cfg.chunk_search_sparse_weight),
    ):
        if value < 0:
            raise ConfigurationError(f"配置非法：{name}({value}) 不能为负数")
    # 7. 知识图谱相关：种子/三元组数量必须为正、权重非负，
    #    且图谱证据预算不能超过本地证据总预算（图谱区是从本地预算里划分出来的子区）
    if cfg.kg_max_seed_candidates <= 0:
        raise ConfigurationError(
            f"配置非法：KG_MAX_SEED_CANDIDATES({cfg.kg_max_seed_candidates}) 必须大于 0"
        )
    if cfg.kg_max_total_triples <= 0:
        raise ConfigurationError(
            f"配置非法：KG_MAX_TOTAL_TRIPLES({cfg.kg_max_total_triples}) 必须大于 0"
        )
    if cfg.rrf_kg_weight < 0:
        raise ConfigurationError(f"配置非法：RRF_KG_WEIGHT({cfg.rrf_kg_weight}) 不能为负数")
    if cfg.kg_evidence_budget < 0:
        raise ConfigurationError(
            f"配置非法：KG_EVIDENCE_BUDGET({cfg.kg_evidence_budget}) 不能为负数"
        )
    if cfg.kg_evidence_budget > cfg.local_evidence_budget:
        raise ConfigurationError(
            f"配置冲突：KG_EVIDENCE_BUDGET({cfg.kg_evidence_budget}) 不能大于 "
            f"LOCAL_EVIDENCE_BUDGET({cfg.local_evidence_budget})，"
            f"因为图谱证据区预算是从本地证据总预算中划分出来的"
        )
    # 8. 兜底召回相关：份额非负、最小保留在 [0, 份额] 内（保底补入后总数永不超过份额）、权重非负
    if cfg.fallback_search_limit < 0:
        raise ConfigurationError(
            f"配置非法：FALLBACK_SEARCH_LIMIT({cfg.fallback_search_limit}) 不能为负数"
        )
    if cfg.fallback_min_keep < 0 or cfg.fallback_min_keep > cfg.fallback_search_limit:
        raise ConfigurationError(
            f"配置冲突：FALLBACK_MIN_KEEP({cfg.fallback_min_keep}) 必须满足 "
            f"0 <= FALLBACK_MIN_KEEP <= FALLBACK_SEARCH_LIMIT({cfg.fallback_search_limit})"
        )
    if cfg.rrf_fallback_weight < 0:
        raise ConfigurationError(
            f"配置非法：RRF_FALLBACK_WEIGHT({cfg.rrf_fallback_weight}) 不能为负数"
        )


# 模块加载即校验：配置矛盾时直接阻断启动，避免带病运行
_validate_query_pipeline_config(query_pipeline_config)
