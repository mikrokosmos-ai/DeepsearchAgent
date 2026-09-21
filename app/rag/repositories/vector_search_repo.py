"""
Milvus 向量检索数据访问层

只承载「纯 Milvus 请求构造与检索执行」：封装稠密 + 稀疏双路混合检索，
供查询链路的商品名匹配与切片召回共用。
"""
from pymilvus import AnnSearchRequest, WeightedRanker

from app.core.logger import logger


def _coerce_int64_ids(ids):
    """
    转换 chunk_id 为 Milvus 要求的 INT64 类型（主键字段 schema 为 INT64）

    过滤无效 ID，分离可转换 / 不可转换的 ID。

    :param ids: 待转换的 chunk_id 列表
    :return: 元组 (ok_ids, bad_ids)，ok_ids 为可转换的 int64 类型 ID 列表，bad_ids 为无效 ID 列表
    """
    ok, bad = [], []
    for x in (ids or []):
        if x is None:
            continue
        try:
            ok.append(int(x))
        except Exception:
            bad.append(x)
    return ok, bad


def fetch_chunks_by_chunk_ids(
        client,
        collection_name: str,
        chunk_ids,
        *,
        output_fields=None,
        batch_size: int = 100,
):
    """
    通过 chunk_id 主键批量查询 Milvus 中的切片数据

    用于补全「仅拥有 chunk_id 无文本内容」场景的切片信息：
    优先使用 get 方法（主键直查，性能最优），失败则回退 query 过滤查询。

    :param client: MilvusClient 实例
    :param collection_name: 集合名称
    :param chunk_ids: 待查询的 chunk_id 列表
    :param output_fields: 需要返回的字段列表，默认返回核心切片字段
    :param batch_size: 分批查询大小，避免单次查询数据量过大，默认 100
    :return: List[dict]，Milvus 实体字典列表，查询失败返回空列表
    """
    if client is None:
        return []
    if not collection_name:
        return []
    if output_fields is None:
        output_fields = ["chunk_id", "content", "title", "parent_title", "item_name"]

    ok_ids, bad_ids = _coerce_int64_ids(chunk_ids)
    if bad_ids:
        logger.warning(f"存在无法转换为 INT64 的 chunk_id，将跳过查询：{bad_ids}")

    if not ok_ids:
        return []

    results = []
    for i in range(0, len(ok_ids), batch_size):
        batch = ok_ids[i: i + batch_size]

        # 方式 1：优先使用主键 get 方法查询（性能最优）
        if hasattr(client, "get"):
            try:
                got = client.get(collection_name=collection_name, ids=batch, output_fields=output_fields)
                if got:
                    results.extend(got)
                continue
            except Exception as e:
                logger.warning(f"Milvus get 方法查询失败，将回退至 query 方法：{str(e)}")

        # 方式 2：get 方法失败，回退使用 filter 过滤查询
        try:
            expr = f"chunk_id in [{', '.join(str(x) for x in batch)}]"
            q = client.query(collection_name=collection_name, filter=expr, output_fields=output_fields)
            if q:
                results.extend(q)
        except Exception as e:
            logger.exception(f"Milvus query 方法批量查询 chunk_id 失败：{str(e)}")

    return results


def create_hybrid_search_requests(dense_vector, sparse_vector, dense_params=None, sparse_params=None, expr=None,
                                  limit=5):
    """
    构建 Milvus 混合搜索请求对象

    分别创建稠密 / 稀疏向量的搜索请求，用于后续混合搜索融合。

    :param dense_vector: 文本生成的稠密向量
    :param sparse_vector: 文本生成的稀疏向量
    :param dense_params: 稠密向量搜索参数，默认使用内积（IP）
    :param sparse_params: 稀疏向量搜索参数，默认使用内积（IP）
    :param expr: 搜索过滤表达式，用于精准筛选数据
    :param limit: 单向量搜索返回结果数量，默认 5
    :return: 搜索请求列表，包含 [dense_req, sparse_req]
    """
    # 稠密向量默认搜索参数：内积（IP）
    if dense_params is None:
        dense_params = {"metric_type": "IP"}
    # 稀疏向量默认搜索参数：内积（IP），适配 BGE-M3 稀疏向量
    if sparse_params is None:
        sparse_params = {"metric_type": "IP"}

    dense_req = AnnSearchRequest(
        data=[dense_vector],
        anns_field="dense_vector",
        param=dense_params,
        expr=expr,
        limit=limit
    )

    sparse_req = AnnSearchRequest(
        data=[sparse_vector],
        anns_field="sparse_vector",
        param=sparse_params,
        expr=expr,
        limit=limit
    )

    return [dense_req, sparse_req]


def hybrid_search(client, collection_name, reqs, ranker_weights=(0.5, 0.5), norm_score=False, limit=5,
                  output_fields=None, search_params=None):
    """
    执行 Milvus 稠密 + 稀疏向量混合搜索

    基于 WeightedRanker 实现双向量搜索结果加权融合，提升检索准确性。

    :param client: MilvusClient 实例
    :param collection_name: 集合名称
    :param reqs: 搜索请求列表，固定为 [dense_req, sparse_req]
    :param ranker_weights: 加权融合权重，默认 (0.5, 0.5)，依次对应稠密 / 稀疏向量
    :param norm_score: 是否归一化评分后再融合，避免评分量级差异导致权重失效
    :param limit: 混合搜索最终返回结果数量，默认 5
    :param output_fields: 需要返回的字段列表，默认返回 item_name
    :param search_params: 搜索参数，如 ef / topk 等，默认 None
    :return: 混合搜索结果列表，搜索失败返回 None
    """
    try:
        # norm_score=True：先将两个向量评分归一化到 0~1 区间，再加权计算
        rerank = WeightedRanker(ranker_weights[0], ranker_weights[1], norm_score=norm_score)

        if output_fields is None:
            output_fields = ["item_name"]

        res = client.hybrid_search(
            collection_name=collection_name,
            reqs=reqs,
            ranker=rerank,
            limit=limit,
            output_fields=output_fields,
            search_params=search_params
        )
        logger.info(f"Milvus 混合搜索完成，集合[{collection_name}]共检索到 {len(res[0])} 条结果")
        return res
    except Exception as e:
        logger.exception(f"Milvus 混合搜索执行失败，集合[{collection_name}]：{str(e)}")
        return None
