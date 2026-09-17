"""
本地知识库检索工具模块

把本地 RAG 检索链路（`app/pipelines/query_pipeline` 的 `query_app`）封装成一个
DeepAgents 可调用的 LangChain 工具，供「本地知识库助手」子智能体使用。

工具契约（与原外部知识库工具的调用约定保持一致，便于主智能体平滑路由）：
    1. 入参是自然语言问题，**返回字符串**（模型可直接阅读的答案文本）；
    2. 工具内部先通过 `monitor` 上报调用参数，前端可展示当前检索动作；
    3. 异常不抛出，转为中文错误提示返回，避免一次检索失败打断整个智能体任务。

与 SSE 的关系：
    query pipeline 内部通过 SSE 队列推送节点进度；本工具在调用期间用
    `RagEventBridge` 把这些事件转发到 `monitor` 的 WebSocket 通道，
    使前端能实时看到「确认问题产品 → 4 路召回 → 融合排序 → 生成答案」的进度。
    因此 state 的 `is_stream` 置为 True —— 它是 task_utils 推送进度事件的前置开关
    （`add_running_task/add_done_task` 仅在 is_stream 为真时推送），
    与「答案最终以字符串返回」并不冲突。
"""

from uuid import uuid4

from langchain_core.tools import tool

from app.api.context import get_thread_context
from app.api.monitor import monitor
from app.api.rag_event_bridge import RagEventBridge
from app.core.logger import logger
from app.rag.pipelines.query_pipeline.graph import query_app
from app.rag.pipelines.query_pipeline.state import create_query_default_state

# 取不到 DeepAgents 会话上下文时的兜底会话名：保证工具仍可用（只是历史聚合到同一会话）
_DEFAULT_SESSION = "local_kb_default"


@tool
def local_rag_search(question: str) -> str:
    """
    检索企业内部产品知识库（本地 RAG），返回与该问题相关的知识库答案

    注意：本工具只用于查询企业内部私有资料（产品手册、说明书、白皮书、制度文件等），
    不用于查询互联网公开信息，也不用于查询结构化业务数据库。

    :param question: 需要检索的问题，建议是包含具体产品名称的完整自然语言问题
    :return: 知识库生成的答案文本；未命中或异常时返回中文提示
    """
    # 会话维度的三元 key：
    #   session_id —— DeepAgents 的 thread_id，用于让同一会话的多轮检索共享 Mongo 历史；
    #   task_id    —— 每次工具调用唯一，作为 SSE 队列 key，避免并发调用互相串台。
    session_id = get_thread_context() or _DEFAULT_SESSION
    task_id = f"{session_id}#{uuid4().hex[:8]}"

    # 埋点：与其它工具一致，前端可据此展示「正在执行本地知识库检索」
    monitor.report_tool(
        tool_name="本地知识库检索工具：local_rag_search",
        args={"question": question},
    )

    # 必须用 create_query_default_state 构造**完整** state：query 链路的默认 state
    # 是各节点读取字段的契约基准（缺失键会触发节点内显式报错）
    state = create_query_default_state(
        session_id=session_id,
        task_id=task_id,
        original_query=question,
        is_stream=True,
    )

    try:
        # 桥接在独立线程消费 pipeline 推送的事件，主线程执行检索本身
        with RagEventBridge(task_id):
            result_state = query_app.invoke(state)

        answer = (result_state.get("answer") or "").strip()
        if not answer:
            logger.warning(f"本地知识库检索未产出答案：task_id={task_id}")
            return "本地知识库未返回任何内容，可能知识库中没有与该问题相关的资料。"
        return answer
    except Exception as e:
        # 检索失败不应中断整个智能体任务：转成中文提示交给模型继续处理
        logger.error(f"本地知识库检索失败：task_id={task_id}，原因：{e}", exc_info=True)
        return f"本地知识库检索失败，错误原因：{str(e)}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证本地 RAG 链路（需 Milvus/Mongo/Neo4j 可用）
    print(local_rag_search.invoke({"question": "HAK 180 烫金机怎么操作？"}))
