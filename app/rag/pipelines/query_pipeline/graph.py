from langgraph.graph import StateGraph, END

from app.rag.pipelines.query_pipeline.nodes.node_answer_output import node_answer_output
from app.rag.pipelines.query_pipeline.nodes.node_item_name_confirm import node_item_name_confirm
from app.rag.pipelines.query_pipeline.nodes.node_rerank import node_rerank
from app.rag.pipelines.query_pipeline.nodes.node_rrf import node_rrf
from app.rag.pipelines.query_pipeline.nodes.node_search_embedding import node_search_embedding
from app.rag.pipelines.query_pipeline.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.rag.pipelines.query_pipeline.nodes.node_web_search_mcp import node_web_search_mcp
from app.rag.pipelines.query_pipeline.nodes.node_query_kg import node_query_kg
from app.rag.pipelines.query_pipeline.state import QueryGraphState
from app.core.logger import logger

# 1. 定义状态图对象,并且指定全局的state
query_graph = StateGraph(QueryGraphState)
# 2. 添加节点信息
query_graph.add_node("node_item_name_confirm", node_item_name_confirm)
query_graph.add_node("node_search_embedding", node_search_embedding)
query_graph.add_node("node_search_embedding_hyde", node_search_embedding_hyde)
query_graph.add_node("node_web_search_mcp", node_web_search_mcp)
query_graph.add_node("node_query_kg", node_query_kg)
query_graph.add_node("node_rrf", node_rrf)
query_graph.add_node("node_rerank", node_rerank)
query_graph.add_node("node_answer_output", node_answer_output)

# 3. 指定入口节点 (有条件边)
query_graph.set_entry_point("node_item_name_confirm")


# 4. 指定条件边,动态边
# state answer 进行判定!
# None -> 第一个节点已经顺利的识别出了 item_names 提问没有问题
# str  -> 提问是空 | 有不确定的item_names  | 没有识别对应的item_name
def node_item_name_confirm_after_router(state: QueryGraphState):
    # 用 .get 而非 [] 索引：与 node_item_name_confirm.step_6 中 `if "answer" in state` 的
    # 防御口径保持一致 —— 确权成功分支会删除 answer 键，若调用方传入的 state 未携带该键，
    # 直接索引会抛 KeyError；经 create_query_default_state 构造的完整 state 下行为完全等价。
    if state.get('answer'):
        # 不为空!（确权失败，已写入兜底提示）→ 直接进入答案输出
        logger.warning(f"node_item_name_confirm_无法继续向后执行: {state['answer']}")
        return "node_answer_output"
    # 为空,可以正常执行,并发执行多路检索节点
    # 说明：这里返回「元组」即表示并行扇出 4 路召回（不引入虚节点），
    #      因此 path_map 必须同步补齐这 4 条映射，否则运行期会因映射缺失而报错。
    return "node_search_embedding", "node_search_embedding_hyde", "node_web_search_mcp", "node_query_kg"


query_graph.add_conditional_edges("node_item_name_confirm",
                                   node_item_name_confirm_after_router,
                                   {
                                       "node_answer_output": "node_answer_output",
                                       "node_search_embedding": "node_search_embedding",
                                       "node_search_embedding_hyde": "node_search_embedding_hyde",
                                       "node_web_search_mcp": "node_web_search_mcp",
                                       "node_query_kg": "node_query_kg"
                                   })
# 5. 指定静态边（4 路召回汇入 node_rrf，由 LangGraph 的 superstep 语义隐式汇合）
query_graph.add_edge("node_search_embedding", "node_rrf")
query_graph.add_edge("node_search_embedding_hyde", "node_rrf")
query_graph.add_edge("node_web_search_mcp", "node_rrf")
query_graph.add_edge("node_query_kg", "node_rrf")
query_graph.add_edge("node_rrf", "node_rerank")
query_graph.add_edge("node_rerank", "node_answer_output")
query_graph.add_edge("node_answer_output", END)
# 6. 编译对象即可
query_app = query_graph.compile()
