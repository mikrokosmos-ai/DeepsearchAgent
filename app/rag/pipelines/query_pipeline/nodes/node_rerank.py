import os
import sys

from app.core.exceptions import RerankError
from app.rag.clients.manager.reranker_client_manager import reranker_client_manager
from app.rag.conf.query_pipeline_config import query_pipeline_config
from app.rag.pipelines.query_pipeline.state import resolve_trace_key
from app.utils.task_utils import add_running_task, add_done_task
from app.core.logger import logger, step_log, node_log, node_guard

# -----------------------------
# Rerank / TopK 全局常量
# 契约：本组参数一律**从配置读取，不从 state 读取**——state 只流转数据，不承载调优参数。
#       来源：app/conf/query_pipeline_config.py，默认值等于改造前的字面量现值。
# -----------------------------
# 动态 TopK 硬上限：最多取前 N 条
RERANK_MAX_TOPK: int = query_pipeline_config.rerank_max_topk
# 最小 TopK：至少保留前 N 条（>=1，且 <= RERANK_MAX_TOPK）
RERANK_MIN_TOPK: int = query_pipeline_config.rerank_min_topk
# 分区保底：本地（type == "milvus"）切片至少保留 N 条（0 = 关闭）
RERANK_MIN_LOCAL_KEEP: int = query_pipeline_config.rerank_min_local_keep
# 断崖阈值（相对）
RERANK_GAP_RATIO: float = query_pipeline_config.rerank_gap_ratio
# 断崖阈值（绝对）
RERANK_GAP_ABS: float = query_pipeline_config.rerank_gap_abs
# 证据闸门：整批最高精排分的绝对下限。0 = 关闭（恒放行）。
# 判据是整批最高分而非逐条 —— 逐条过滤的误丢代价远高于误放。
EVIDENCE_MIN_SCORE: float = query_pipeline_config.evidence_min_score
# -----------------------------
# Cross-Encoder 打分的资源约束
RERANK_BATCH_SIZE: int = int(os.getenv("RERANK_BATCH_SIZE", "8"))
# 单次打分超时（秒）：超时降级为 RRF 原序，而非无限等待
RERANK_TIMEOUT_S: float = float(os.getenv("RERANK_TIMEOUT_S", "30"))
# 显式最大长度：与模型原生默认一致，写进代码以免未来版本默认值变更引发 silent 行为漂移
RERANK_MAX_LENGTH: int = int(os.getenv("RERANK_MAX_LENGTH", "512"))


"""
  节点: Cross-Encoder 重排 (node_rerank)
  入参:  rrf_chunks / web_search_docs  (+ rewritten_query)
  出参:  reranked_docs（统一结构 {title,text,url,type,score}）
  步骤:
       1. 参数校验
       2. RRF 路与联网路统一格式合并（并传播 type：milvus / kg / web）
       3. Cross-Encoder 批量打分并排序
       4. 断崖式动态截断（min_topk ~ max_topk）
       5. 分区保底：本地切片至少保留 N 条
"""


@step_log("step_1_data_validates")
def step_1_data_validates(state):
    """
    获取rrf_node粗排序的结果以及mcp网络搜索结果
    :param state:
    :return:
    """
    rrf_chunks = state.get("rrf_chunks", [])
    web_search_docs = state.get("web_search_docs", [])
    return rrf_chunks, web_search_docs


@step_log("step_2_merged_rrf_and_mcp")
def step_2_merged_rrf_and_mcp(rrf_chunks, web_search_docs):
    """
    进行两路数据融合! 统一格式方便后续数据处理!
    {title: , text: content or snippet , url : mcp专属 , type: milvus or kg or web , score : 0.0}
    :param rrf_chunks:
    :param web_search_docs:
    :return: [{title: , text: content or snippet , url : mcp专属 , type: milvus or kg or web , score : 0.0}]
    """
    # 1. 准备工作,定义融合的数据集合
    final_chunk_list = []
    # 2. 循环rrf_chunks路数据整合
    #    关键：type 必须从 entity 传播过来 —— 图谱证据带 type="kg"，
    #    若在此统一写成 "milvus"，图谱证据会被误并入本地知识库区（丢失独立分区）。
    #    补充路证据带 type="milvus_fallback"，同样经此传播，供下游区分「定向路 vs 兜底路」，
    #    并让 step_5 的本地保底（type == "milvus" 精确匹配）天然把它排除在外。
    if rrf_chunks and len(rrf_chunks) > 0:
        for chunk in rrf_chunks:
            final_chunk_list.append({
                "title": chunk.get("title"),
                "text": chunk.get("content"),
                "url": None,
                "type": chunk.get("type") or "milvus",
                "score": 0.0
            })
    # 3. 循环web_mcp路数据整合
    if web_search_docs and len(web_search_docs) > 0:
        for doc in web_search_docs:
            final_chunk_list.append({
                "title": doc.get("title"),
                "text": doc.get("snippet"),
                "url": doc.get("url"),
                "type": "web",
                "score": 0.0
            })
    # 4. 返回数据处理
    logger.info(
        f"完成了两路数据统一格式处理,rrf路原数据条数:{len(rrf_chunks)},web_mcp路原数据条数:{len(web_search_docs)},"
        f"合并后数据:{len(final_chunk_list)}条"
    )
    return final_chunk_list


@step_log("step_3_rerank_score_and_sort")
def step_3_rerank_score_and_sort(state, final_chunk_list):
    """
    给问题 + 答案 进行打分和排序处理!
    :param state: 获取重写的问题
    :param final_chunk_list: 有答案 text 就是答案
    :return: 带有分的数据 [{title: , text: content or snippet , url : mcp专属 , type: milvus or web , score :0.x}]
    """
    # 0. 空候选保护：RRF 路与联网路同时为空（例如知识库尚未入库、检索无命中）时，
    #    无文档可重排，直接返回空列表 —— 避免把空列表喂给底层 reranker
    if not final_chunk_list:
        logger.warning("RRF 路与联网路召回结果均为空，无文档可重排，跳过打分")
        return final_chunk_list
    # 1.获取重写的问题
    rewritten_query = state.get("rewritten_query") or state.get("original_query")
    # 2.获取顺序获取所有答案
    #  text_list 顺序 = [得分的顺序] =  final_chunk_list 顺序
    text_list = [item.get("text") for item in final_chunk_list]
    # 3.组装问题+答案对的集合
    # 问题 + 答案长度大于reranker模型的上线!
    # 需要调用 lm模型进行压缩处理
    question_paris = []
    for text in text_list:
        # 进行判断
        # 512 = 一对长度  rewritten_query [自己已经超过了512 -> lm进行重写!在对应提示词添加长度限制!! ]  text 长度 = 512 - 问题的长度
        # todo: 动态的提示词  原答案 + 限制的长度
        # 注意 [问题 , 答案]
        question_paris.append([rewritten_query, text])
    # 4.批量进行问题和答案打分
    # 统一走 manager：一次性获得 batch_size 控制、推理串行锁、超时保护与异常包装。
    try:
        score_list = reranker_client_manager.compute_score(
            question_paris,
            batch_size=RERANK_BATCH_SIZE,
            timeout=RERANK_TIMEOUT_S,
            max_length=RERANK_MAX_LENGTH,
        )
    except RerankError as e:
        # 降级：重排不可用不应让整条查询链路失败。
        # 保留 RRF 给出的顺序，仅补零分占位，后续 step_4 的断崖截断仍可正常工作。
        # 降级标记写进 state：step_3.5 闸门据此"一律放行"（分数不可信时不做丢弃决策）。
        logger.warning(f"重排失败，降级为 RRF 原序返回：{e}")
        state["_rerank_degraded"] = True
        for chunk in final_chunk_list:
            chunk.setdefault("score", 0.0)
        return final_chunk_list
    # 5.将集合更新分数信息即可
    for score, chunk in zip(score_list, final_chunk_list):
        # score_list [分的列表 . xxxxxxx] 4位
        # chunk["score"] = f"{score:.4f}"  # 截取  0.12346 -> 0.1234
        chunk["score"] = round(score, 4)  # 四舍五入  0.12346 -> 0.1235
    # 6.基于分数进行集合排序
    final_chunk_list.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    logger.info(f"已完成编排和打分！最终结果为：{final_chunk_list}")
    # 7.返回数据
    return final_chunk_list


@step_log("step_3_5_evidence_gate")
def step_3_5_evidence_gate(chunk_list_score_sorted, degraded: bool = False):
    """
    证据闸门：判断本批证据是否"根本不相关"。
    :param chunk_list_score_sorted: step_3 输出的降序列表
    :param degraded: step_3 是否走了"重排失败补零分"的降级分支
    :return: (是否放行, 整批最高分)
    """
    if EVIDENCE_MIN_SCORE <= 0:
        # 闸门关闭：不计算、不判定，直接放行（保持零行为变化）
        return True, 0.0
    if degraded:
        logger.warning(
            f"证据闸门：重排处于降级状态，分数不可信，按约定放行（阈值={EVIDENCE_MIN_SCORE}）"
        )
        return True, 0.0
    if not chunk_list_score_sorted:
        # 空候选：无证据可判，放行让下游走"无资料"分支
        return True, 0.0
    top_score = chunk_list_score_sorted[0].get("score", 0.0)
    if top_score < EVIDENCE_MIN_SCORE:
        logger.warning(
            f"证据闸门拦截：整批最高精排分 {top_score} < 阈值 {EVIDENCE_MIN_SCORE}，"
            f"本批 {len(chunk_list_score_sorted)} 条证据判定为无关，整批丢弃"
        )
        return False, top_score
    logger.info(f"证据闸门通过：整批最高精排分 {top_score} >= 阈值 {EVIDENCE_MIN_SCORE}")
    return True, top_score


@step_log("step_4_chunk_topk")
def step_4_chunk_topk(chunk_list_score_sorted):
    """
    动态 TopK（最多 10）

    基于 final_chunk_list（已按 score 降序排序）进行智能截断，
    核心逻辑：结合固定上下限 + 断崖阈值判断，避免机械取前 N 条，保留语义相关的连续文档集合。

    min_topk  - topk截取数据  -  max_topk
    :param chunk_list_score_sorted:
    :return:
    """
    # 1. 参数接收
    min_topk = RERANK_MIN_TOPK
    max_topk = RERANK_MAX_TOPK
    gap_ratio = RERANK_GAP_RATIO  # 相对断崖阈值：分数下降的相对比例阈值
    max_gap = RERANK_GAP_ABS  # 绝对断崖阈值：分数下降的绝对差值阈值（全局常量配置）
    # 2. 前置条件处理
    topk = min(max_topk, len(chunk_list_score_sorted))  # 理想获取最大的值
    # 3. 范围循环确定 topk
    # [0.98,0.97,0.96,0.25,0.23]  -> topK = 3  [:topk 数量 index + 1 ]
    if topk > min_topk:
        # 上界必须用 topk-1（topk = min(max_topk, len(list))）而不是 max_topk-1：
        # 后者在"候选数少于 RERANK_MAX_TOPK"时会访问越界索引 → IndexError
        # （实测：6 条平滑分数直接崩；原代码只在断崖提前 break 时才侥幸不触发）。
        for index in range(min_topk - 1, topk - 1):
            score_1 = chunk_list_score_sorted[index].get("score", 0.0)
            score_2 = chunk_list_score_sorted[index + 1].get("score", 0.0)
            # 分差
            abs_score = score_1 - score_2
            ratio_score = abs_score / (abs(score_1) + 1e-7)  # 1e-7防止分母为0
            if abs_score >= max_gap or ratio_score >= gap_ratio:
                # 产生断崖了
                logger.info(f"数据集合{index}和{index+1}的位置发生了断崖，结束循!!")
                topk = index + 1  # 保证第一个一定获取! [:topk 数量 = index + 1 ]
                break
    # 4. topk数据截取
    final_chunk_list = chunk_list_score_sorted[:topk]
    logger.info(f"最终截取的长度：{topk}，截取的内容:{final_chunk_list}")
    # 5. 返回数据
    return final_chunk_list


@step_log("step_5_ensure_local_quota")
def step_5_ensure_local_quota(sorted_docs, selected_docs, min_local: int = None):
    """
    分区保底：保证「本地知识库切片」在最终证据里至少有 min_local 条。

    :param sorted_docs: step_3 输出的完整降序列表（含落选候选）
    :param selected_docs: step_4 断崖截断后的选中结果
    :param min_local: 本地切片至少保留条数（None 取配置 RERANK_MIN_LOCAL_KEEP）
    :return: 补齐后的最终证据（按 score 降序，总数不超过 RERANK_MAX_TOPK）
    """
    if min_local is None:
        min_local = RERANK_MIN_LOCAL_KEEP
    if min_local <= 0 or not sorted_docs:
        return selected_docs

    # 用 id() 判定"是否已被选中"：selected_docs 是 sorted_docs 的元素切片，对象引用相同
    selected_ids = {id(doc) for doc in selected_docs}
    local_selected = [doc for doc in selected_docs if doc.get("type") == "milvus"]
    need = min_local - len(local_selected)
    if need <= 0:
        return selected_docs

    extra = [doc for doc in sorted_docs
             if doc.get("type") == "milvus" and id(doc) not in selected_ids][:need]
    if not extra:
        logger.info(f"分区保底：本地切片 {len(local_selected)}/{min_local} 条，但已无可补候选")
        return selected_docs

    merged = sorted(selected_docs + extra, key=lambda x: x.get("score", 0.0), reverse=True)
    final_docs = merged[:RERANK_MAX_TOPK]
    local_final = sum(1 for doc in final_docs if doc.get("type") == "milvus")
    logger.info(
        f"分区保底：本地切片 {len(local_selected)} → {local_final} 条（补入 {len(extra)} 条），"
        f"最终证据 {len(final_docs)} 条"
    )
    return final_docs


# 装饰器定序约定：@node_guard 写在 @node_log 之上
# （Python 自下而上应用 → node_log 先包裹并记录原始堆栈，node_guard 再包裹做异常归一化）
# 本节点为 node_guard 的试点接入点，验证通过后将按同样方式逐步推广到其余节点。
@node_guard("node_rerank")
@node_log("node_rerank")
def node_rerank(state):
    """
    节点功能：使用 Cross-Encoder 模型对 RRF 后的结果进行精确打分重排。
    """
    # 1. 日志+任务
    add_running_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
    # 2. 获取参数校验
    rrf_chunks, web_search_docs = step_1_data_validates(state)
    # 3. 将两路数据捏到一起 [{},{}] -> 两个循环 rrf_chunks | web_search_docs
    # 约定返回结果: [{title: ,  text: content or snippet ,  url : mcp专属 ,  type: milvus or kg or web ,  score : 0.0}]
    final_chunk_list = step_2_merged_rrf_and_mcp(rrf_chunks, web_search_docs)
    # 3.1 漏斗：合池后条数（RRF 存活 + 联网）
    pool_count = len(final_chunk_list)
    # 4. 使用reranker进行问题和答案打分(批量处理)，并做好排序
    chunk_list_score_sorted = step_3_rerank_score_and_sort(state, final_chunk_list)
    # 4.1 证据闸门：打分后、截断前判定本批是否根本无关。
    #     不通过 → 整批归零并直接返回：绝不能继续跑到 step_5 分区保底，
    #     否则保底会把刚丢掉的本地切片重新捡回来（"闸门丢、保底捡"的循环）。
    gate_passed, gate_top_score = step_3_5_evidence_gate(
        chunk_list_score_sorted, degraded=bool(state.get("_rerank_degraded"))
    )
    if not gate_passed:
        state["reranked_docs"] = []
        # 显式标记：供 local_rag_tool 区分"闸门拦截"与"检索没命中"两种空结果
        state["_evidence_gate_blocked"] = True
        state["retrieval_funnel"] = _append_rerank_funnel(
            state, pool_count, len(chunk_list_score_sorted), 0, 0,
        )
        add_done_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
        return state
    # 5. 进行动态数据截取
    chunk__score_sorted_topk = step_4_chunk_topk(chunk_list_score_sorted)
    # 6. 分区保底：断崖之后仍保证本地切片至少 N 条（cross-encoder 全局同池排序会压制本地证据）
    reranked_docs = step_5_ensure_local_quota(chunk_list_score_sorted, chunk__score_sorted_topk)
    # 7. 保存结果
    state["reranked_docs"] = reranked_docs
    # 8. 漏斗第二段（rerank 路）：合池 → 打分 → 断崖 → 保底（合并写入，不覆盖 rrf 段）
    state["retrieval_funnel"] = _append_rerank_funnel(
        state, pool_count, len(chunk_list_score_sorted),
        len(chunk__score_sorted_topk), len(reranked_docs),
    )
    add_done_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state.get("is_stream"))
    return state


def _append_rerank_funnel(state, pool_count: int, scored_count: int,
                          after_gap_count: int, final_count: int) -> dict:
    """
    把 rerank 段漏斗合并进 state 的 retrieval_funnel（不覆盖 rrf 段）。

    :param state: 节点 state
    :param pool_count: 合池后条数（RRF 存活 + 联网）
    :param scored_count: 打分完成条数（降级时等于合池数，无真实分数）
    :param after_gap_count: 断崖截断后条数
    :param final_count: 保底后最终条数
    :return: 合并后的完整 retrieval_funnel
    """
    funnel = dict(state.get("retrieval_funnel") or {})
    docs = state.get("reranked_docs") or []
    # 降级判据：合池非空、最高分为 0、且断崖未淘汰任何条
    #   → 说明 step_3 走了 RerankError 的"补零分返回原序"分支，分数不可信
    top_score = max((doc.get("score") or 0.0) for doc in docs) if docs else 0.0
    degraded = bool(pool_count) and top_score == 0.0 and after_gap_count == pool_count
    funnel["rerank"] = {
        "pool": pool_count,
        "scored": scored_count,
        "after_gap": after_gap_count,
        "final": final_count,
        "degraded": degraded,
        "top_score": round(float(top_score), 4),
    }
    logger.info(
        f"Rerank 漏斗：合池={pool_count} → 打分={scored_count} → "
        f"断崖后={after_gap_count} → 最终={final_count}（降级={degraded}，最高分={top_score}）"
    )
    return funnel


if __name__ == "__main__":
    from app.rag.pipelines.query_pipeline.state import create_query_default_state

    print("\n" + "=" * 50)
    print(">>> 启动 node_rerank 本地测试")
    print("=" * 50)

    # 1. 模拟数据
    # 1.1 RRF 本地文档数据
    mock_rrf_chunks = [
        {"chunk_id": "local_1", "content": "RRF是一种倒数排名融合算法", "title": "算法介绍"},
        {"chunk_id": "local_2", "content": "BGE是一个强大的重排序模型", "title": "模型介绍"},
        {"chunk_id": "local_3", "content": "无关的测试文档内容", "title": "测试文档"}  # 预期低分
    ]

    # 1.2 MCP 联网搜索数据
    mock_web_docs = [
        {"title": "Rerank技术详解", "url": "http://web.com/1", "snippet": "Rerank即重排序，常用于RAG系统的第二阶段"},
        {"title": "无关网页", "url": "http://web.com/2", "snippet": "今天天气不错，适合出去游玩"}  # 预期低分
    ]

    mock_state = create_query_default_state(
        session_id="test_rerank_session",
        task_id="test_rerank_session",
        rewritten_query="什么是RRF和Rerank？",  # 查询意图：想了解这两个算法
        rrf_chunks=mock_rrf_chunks,
        web_search_docs=mock_web_docs,
        is_stream=False
    )

    try:
        # 运行节点
        result = node_rerank(mock_state)
        reranked = result.get("reranked_docs", [])

        print("\n" + "=" * 50)
        print(">>> 测试结果摘要:")
        print(f"输入文档总数: {len(mock_rrf_chunks) + len(mock_web_docs)}")
        print(f"输出文档总数: {len(reranked)}")
        print("-" * 30)

        print("最终排名:")
        for i, doc in enumerate(reranked, 1):
            print(f"Rank {i}: Type={doc.get('type')}, Score={doc.get('score'):.4f}, Text={doc.get('text')[:20]}...")

        # 验证逻辑：
        # 预期 "local_1", "local_2", "Rerank技术详解" 分数较高
        # 预期 "local_3", "无关网页" 分数较低，可能被截断或排在最后

        top1_score = reranked[0].get("score")
        if top1_score > 0:
            print("\n[PASS] Rerank 打分正常")
        else:
            print("\n[FAIL] Rerank 打分异常 (均为0或负数)")

        print("=" * 50)

    except Exception as e:
        logger.exception(f"测试运行期间发生未捕获异常: {e}")
