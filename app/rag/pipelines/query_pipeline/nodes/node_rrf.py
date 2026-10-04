import sys

from app.rag.conf.query_pipeline_config import query_pipeline_config
from app.rag.pipelines.query_pipeline.state import resolve_trace_key
from app.utils.task_utils import add_running_task, add_done_task
from app.core.logger import logger, node_log, step_log

# RRF 融合参数（来源：app/conf/query_pipeline_config.py，默认值等于改造前的默认参数值）
# k：平滑参数，用于削弱排名的过大影响
RRF_K = query_pipeline_config.rrf_k
# top：融合排序后保留的条数
RRF_TOP = query_pipeline_config.rrf_top
# 知识图谱路的融合权重（向量路 / HyDE 路恒为 1.0）
RRF_KG_WEIGHT = query_pipeline_config.rrf_kg_weight

# 参与 RRF 的三路名称，顺序与 param_list 严格对齐（漏斗指标的 key 来源）
ROUTE_NAMES = ("embedding", "hyde", "kg")

"""
  节点: 多路融合排序 (node_rrf)
  入参:  embedding_chunks / hyde_embedding_chunks / kg_chunks
  出参:  rrf_chunks
  说明:  联网路（web_search_docs）不参与 RRF —— 它不带 chunk_id、也不做 rank 融合，
         由下游 node_rerank 以「独立来源」并入统一证据池（见 node_rerank.step_2）。
"""


@step_log("step_1_data_validates")
def step_1_data_validates(state):
    """
    获取参数并且校验
    :param state:
    :return:
    """
    embedding_chunks = state.get("embedding_chunks", [])
    hyde_embedding_chunks = state.get("hyde_embedding_chunks", [])
    kg_chunks = state.get("kg_chunks", [])
    return embedding_chunks, hyde_embedding_chunks, kg_chunks


@step_log("step_2_rrf_list")
def step_2_rrf_list(param_list, k: int = RRF_K, top: int = RRF_TOP):
    """
    进行多路融合排序 , 同源,启动算法!
    本次算法 = (1.0 / (k + rank)) * weight
    :param param_list: [([1 {id:xx,distance:xx,entity:{chunk_id}},2,3],1.0),([1,2,3],1.0),([],1.0)]
    :param k 平滑参数,用于削弱排名的过大影响（默认取配置 RRF_K）
    :param top 最终的获取数量（默认取配置 RRF_TOP）
    :return: (final_entity_list, funnel)
             final_entity_list: [entity,entity....]
             funnel: 本段的召回漏斗指标（见 _build_funnel）
    """
    # 1. 定义两个字典( 分别存储chunk_id，累计得分  || chunk_id  chunk entity )
    score_dict = {}  # key ：chunk_id |  value ：score
    entity_dict = {}  # key ：chunk_id |  value ：chunk entity
    # 1.1 chunk_id -> 命中的路名集合（用于统计跨路一致性；一条被 N 路同时召回 = 强信号）
    hit_routes: dict = {}
    # 2. 循环路 (计算每一路的分 )
    for route_name, (chunks_list, weight) in zip(ROUTE_NAMES, param_list):
        # 3. 循环单路的积分 (排名第一开始处理)
        for rank, chunk in enumerate(chunks_list, start=1):
            # rank排名 1 2 3  = chunk 数据
            # {id:xx,distance:分 , entity:{chunk_id:xx }}
            chunk_id = chunk.get("id") or chunk['entity']['chunk_id']
            # 获取之前的分 + 本次的分
            score_dict[chunk_id] = score_dict.get(chunk_id, 0.0) + (1.0 / (k + rank)) * weight
            # 存储chunk_id -> entity
            # 如果没有值,才赋值! 第一次已经赋值了,后面就不会更新了
            entity_dict.setdefault(chunk_id, chunk.get("entity", {}))
            hit_routes.setdefault(chunk_id, set()).add(route_name)
    # 4. 处理数据和排序（按累计得分降序）
    entity_list = []
    for chunk_id, score in score_dict.items():
        entity_list.append(
            (
                entity_dict.get(chunk_id, {}),
                score
            )
        )
    entity_list.sort(key=lambda x: x[1], reverse=True)
    final_entity_list = [entity for entity, score in entity_list[:top]]
    # 5. 汇总本段漏斗指标（各路输入条数 / 去重后总数 / 跨路一致性分布 / 存活条数）
    funnel = _build_funnel(param_list, hit_routes, len(final_entity_list))
    # 6. 返回结果即可
    return final_entity_list, funnel


def _build_funnel(param_list, hit_routes, out_count: int) -> dict:
    """
    汇总 RRF 段的漏斗指标。

    :param param_list: [(chunks_list, weight), ...] 顺序与 ROUTE_NAMES 对齐
    :param hit_routes: chunk_id -> 命中的路名集合
    :param out_count: 融合排序后实际保留条数
    :return: {"in": {路名: 条数}, "unique": N, "overlap": {...}, "out": M}
    """
    routes_in = {
        name: len(chunks_list) for name, (chunks_list, _w) in zip(ROUTE_NAMES, param_list)
    }
    # 跨路一致性分布：一条 chunk 被几路同时召回（多路命中越多，证据越可信）
    overlap = {"3-way": 0, "2-way": 0, "1-way": 0}
    for routes in hit_routes.values():
        n = len(routes)
        if n >= 3:
            overlap["3-way"] += 1
        elif n == 2:
            overlap["2-way"] += 1
        else:
            overlap["1-way"] += 1
    return {
        "in": routes_in,
        "unique": len(hit_routes),
        "overlap": overlap,
        "out": out_count,
    }


@node_log("node_rrf")
def node_rrf(state):
    """
    节点功能：Reciprocal Rank Fusion
    将多路召回的结果（向量、HyDE、KG）进行加权融合排序。
    """
    # 1. 日志+任务
    add_running_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
    # 2. 参数获取和校验 embedding_chunks hyde_embedding_chunks  get("key",[])
    embedding_chunks, hyde_embedding_chunks, kg_chunks = step_1_data_validates(state)
    # 3. 处理下集合参数 [(embedding_chunks,1.0),(hyde_embedding_chunks,1.0),(kg_chunks,RRF_KG_WEIGHT)]  -> param_list
    param_list = [
        (embedding_chunks, 1.0),
        (hyde_embedding_chunks, 1.0),
        (kg_chunks, RRF_KG_WEIGHT)
    ]
    # 4. RRF + 权重排序：param_list -> rrf_chunks（元素为 entity dict）
    entity_list, rrf_funnel = step_2_rrf_list(param_list)
    # 5.更新结果
    state["rrf_chunks"] = entity_list
    # 5.1 漏斗第一段（RRF 路）：合并写入，不覆盖已有片段
    #     （node_rerank 会在其后追加 rerank 段；若在此覆盖会把后续段清空）
    funnel = dict(state.get("retrieval_funnel") or {})
    funnel["rrf"] = rrf_funnel
    state["retrieval_funnel"] = funnel
    logger.info(
        f"RRF 漏斗：各路输入={rrf_funnel['in']}，去重后={rrf_funnel['unique']}，"
        f"跨路一致性={rrf_funnel['overlap']}，融合保留={rrf_funnel['out']}"
    )
    add_done_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
    return state


# ================================
# 本地测试入口
# ================================
if __name__ == "__main__":
    from app.rag.pipelines.query_pipeline.state import create_query_default_state

    print("\n" + "=" * 50)
    print(">>> 启动 node_rrf 本地测试")
    print("=" * 50)

    mock_state = create_query_default_state(
        session_id="test_rrf_session",
        task_id="test_rrf_session",
        is_stream=False,
        original_query="HAK 180 烫金机怎么操作？",
        rewritten_query="HAK 180 烫金机的具体操作步骤是什么？",
        item_names=["HAK 180 烫金机"]
    )

    try:
        from app.rag.pipelines.query_pipeline.nodes.node_search_embedding import node_search_embedding
        from app.rag.pipelines.query_pipeline.nodes.node_search_embedding_hyde import node_search_embedding_hyde

        emb_res = node_search_embedding(mock_state)
        hyde_res = node_search_embedding_hyde(mock_state)
        mock_state['embedding_chunks'] = emb_res.get("embedding_chunks") or []
        mock_state['hyde_embedding_chunks'] = hyde_res.get("hyde_embedding_chunks") or []

        result = node_rrf(mock_state)
        rrf_chunks = result.get("rrf_chunks", [])
        funnel = result.get("retrieval_funnel") or {}

        emb_cnt = len(mock_state.get("embedding_chunks") or [])
        hyde_cnt = len(mock_state.get("hyde_embedding_chunks") or [])

        print("\n" + "=" * 50)
        print(">>> 测试结果摘要:")
        print(f"输入数量: Embedding={emb_cnt}, HyDE={hyde_cnt}")
        print(f"输出数量: {len(rrf_chunks)}")
        print(f"漏斗指标: {funnel.get('rrf')}")
        print("-" * 30)

        print("最终排名:")
        for i, doc in enumerate(rrf_chunks, 1):
            doc_id = doc.get("chunk_id") or doc.get("id")
            content = (doc.get("content") or "")[:20]
            print(f"Rank {i}: ID={doc_id}, Content={content}...")

        print("=" * 50)

    except Exception as e:
        logger.exception(f"测试运行期间发生未捕获异常: {e}")
