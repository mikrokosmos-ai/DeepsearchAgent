import sys

from app.rag.conf.milvus_config import milvus_config
from app.rag.conf.query_pipeline_config import query_pipeline_config
from app.rag.clients.embedding_client import generate_embeddings
from app.rag.clients.milvus_client import get_milvus_client
from app.rag.repositories.vector_search_repo import create_hybrid_search_requests, hybrid_search
from app.core.logger import logger, node_log, step_log
from app.rag.pipelines.query_pipeline.channel_timeout import run_with_timeout
from app.rag.pipelines.query_pipeline.state import resolve_trace_key
from app.utils.task_utils import add_done_task, add_running_task

# 切片混合检索参数（与 node_search_embedding 共用同一组稠密/稀疏权重）
CHUNK_SEARCH_WEIGHTS = (
    query_pipeline_config.chunk_search_dense_weight,
    query_pipeline_config.chunk_search_sparse_weight,
)
# 补充路召回条数（份额）：无 item_name 过滤，必须显式限制条数防止全库检索放大耗时
FALLBACK_SEARCH_LIMIT = query_pipeline_config.fallback_search_limit
# 通道级耗时预算：与其余四路共用同一超时，超预算按空结果降级
CHANNEL_TIMEOUT_S = query_pipeline_config.channel_timeout_s


FALLBACK_TYPE = "milvus_fallback"

"""
  节点: 兜底召回 (node_search_embedding_fallback)
  入参:  rewritten_query  (+ task_id 用于追踪；item_names 仅用于校验，不参与过滤)
  出参:  fallback_chunks
  步骤:
       1. 参数校验（rewritten_query）
       2. 问题向量化（稠密 + 稀疏）
       3. Milvus 混合检索（**不加 item_name 过滤**，只取 FALLBACK_SEARCH_LIMIT 条）

"""


@step_log("step_1_data_validates")
def step_1_data_validates(state):
    """获取并校验入参：兜底路只依赖改写后的问题，item_names 仅要求存在（保持与主路一致的入口契约）"""
    item_names = state.get("item_names")
    rewritten_query = state.get("rewritten_query")
    if not item_names or not rewritten_query:
        logger.error("item_names或rewritten_query不存在,无法继续业务!")
        raise ValueError("item_names或rewritten_query不存在,无法继续业务!")
    return rewritten_query


@step_log("step_2_rewritten_query_vector")
def step_2_rewritten_query_vector(rewritten_query):
    """把改写后的问题向量化，取第一条的稠密与稀疏向量"""
    result = generate_embeddings([rewritten_query])
    return result['dense'][0], result['sparse'][0]


@step_log("step_3_milvus_no_filter_search")
def step_3_milvus_no_filter_search(dense_vector, sparse_vector):
    """
    全库混合检索（无 item_name 过滤）。
    与主路的差异仅在 `expr`：这里传 None，即不设过滤表达式。
    """
    mivlus_client = get_milvus_client()
    # 关键：expr 缺省（None）= 不过滤 —— 全库检索，正是本路的存在意义
    reqs = create_hybrid_search_requests(dense_vector, sparse_vector, expr=None)
    resp = hybrid_search(
        client=mivlus_client,
        collection_name=milvus_config.chunks_collection,
        reqs=reqs,
        ranker_weights=CHUNK_SEARCH_WEIGHTS,
        norm_score=True,
        limit=FALLBACK_SEARCH_LIMIT,
        output_fields=["chunk_id", "file_title", "item_name", "content", "title", "parent_title", "part"]
    )
    return resp[0] if resp and len(resp) > 0 else []


@step_log("step_4_tag_fallback_type")
def step_4_tag_fallback_type(hits):
    """给每条召回的切片 entity 打上来源标记 type=milvus_fallback（供下游分区与本地保底口径区分）"""
    for hit in hits or []:
        entity = hit.get("entity")
        if isinstance(entity, dict):
            entity["type"] = FALLBACK_TYPE
    return hits


@node_log("node_search_embedding_fallback")
def node_search_embedding_fallback(state):
    """
    节点功能：无主体过滤的兜底召回（第 5 路并行召回，与主路同一 superstep）。
    """
    # 1. 日志和任务处理
    add_running_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
    # 2. 参数校验
    rewritten_query = step_1_data_validates(state)
    # 3~4. 向量化 + 无过滤检索 + 打标 整体包通道超时（与主路同款预算）
    def _do_fallback():
        dense_vector, sparse_vector = step_2_rewritten_query_vector(rewritten_query)
        hits = step_3_milvus_no_filter_search(dense_vector, sparse_vector)
        return step_4_tag_fallback_type(hits)

    result, status, elapsed = run_with_timeout(_do_fallback, CHANNEL_TIMEOUT_S, "fallback")
    mivlus_result = result if status == "ok" and result is not None else []
    # 5. 返回结果 + 本路耗时/降级状态（独立字段名，避免并行分支互相覆盖）
    add_done_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
    return {
        "fallback_chunks": mivlus_result,
        "channel_stat_fallback": {"status": status, "elapsed_s": round(elapsed, 3), "count": len(mivlus_result)},
    }


if __name__ == "__main__":
    from app.rag.pipelines.query_pipeline.state import create_query_default_state

    test_state = create_query_default_state(
        session_id="test_fallback_001",
        task_id="test_fallback_001",
        rewritten_query="HAK 180 烫金机 前盖 内部组件 使用说明书",
        item_names=["HAK 180 烫金机"],
        is_stream=False
    )

    print("\n>>> 开始测试 node_search_embedding_fallback 节点...")
    try:
        result = node_search_embedding_fallback(test_state)
        chunks = result.get("fallback_chunks", [])
        print(f"\n>>> 测试完成！兜底检索到 {len(chunks)} 条结果")
        types = {c.get("entity", {}).get("type") for c in chunks}
        print(f">>> 来源标记集合: {types}")
    except Exception as e:
        logger.exception(f"测试运行失败: {e}")
