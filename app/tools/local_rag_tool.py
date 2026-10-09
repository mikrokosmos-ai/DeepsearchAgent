"""
本地知识库检索工具模块

把本地 RAG 检索链路（`app/pipelines/query_pipeline` 的 `query_app`）封装成一个
DeepAgents 可调用的 LangChain 工具，供「本地知识库助手」子智能体使用。

"""

from uuid import uuid4

from langchain_core.tools import tool

from app.api.context import get_thread_context
from app.api.monitor import monitor
from app.api.rag_event_bridge import RagEventBridge
from app.core.logger import logger
# 知识库配图：同样由子图 state 带出，供主智能体收尾落库（历史回读时展示）
from app.core.knowledge_image_store import record_images
# 检索漏斗指标：子图 state 里的 retrieval_funnel 经此带出，供收尾写入 agent_run
from app.core.retrieval_funnel_store import record_funnel
from app.core.answer_shortcircuit import (
    get_short_circuit,
    acquire_inflight,
    finish_inflight,
    mark_short_circuit,
    mark_unresolved,
)
from app.core.tool_failfast import get_tool_failure, mark_tool_failed
from app.rag.pipelines.query_pipeline.graph import query_app
from app.rag.pipelines.query_pipeline.state import create_query_default_state

# 取不到 DeepAgents 会话上下文时的兜底会话
_DEFAULT_SESSION_PREFIX = "local_kb_nocx"

# 故障熔断标记用的工具名（与 @tool 注册名保持一致）
_TOOL_NAME = "local_rag_search"

def _clarify_message(candidate_text: str) -> str:
    """
    组装「需要用户确认型号」的返回文案。

    单独抽成函数是为了让同问题短路能返回**同一条**反问：短路只存候选原文，
    文案模板留在这里（唯一载体），避免两处各写一份后悄悄漂移。
    """
    return (
        "【需要用户确认型号】知识库无法确定你问的是哪一个产品。"
        "这**不是检索失败**，而是问题里的型号不够准确 —— 知识库的返回是："
        f"{candidate_text}\n"
        "请把这一情况如实上报给主智能体，由主智能体在最终答复里请用户补充准确的型号。"
        "**不要再换措辞或换角度重复调用本工具**：型号没确定之前，换什么问法都只会撞同一堵墙，"
        "重试不会得到不同结果、只会成倍消耗预算与时间。请立即结束检索并上报。"
        "（系统说明：本任务内再调用本工具只会原样返回本条问句、不会执行任何检索。）"
    )


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
    context_session = get_thread_context()
    # 有上下文（主链路）：用会话 id —— 同一会话多轮共享历史，故障短路按会话生效。
    # 无上下文（离线脚本 / 单测等）：退化为本次调用唯一 id，避免跨会话污染。
    if context_session:
        session_id = context_session
    else:
        session_id = f"{_DEFAULT_SESSION_PREFIX}#{uuid4().hex[:8]}"
        logger.warning(
            f"本地知识库检索未取到会话上下文，已退化为一次性会话键：session_id={session_id}"
            "（本次调用不共享多轮历史；故障短路仅对本次生效）"
        )
    task_id = f"{session_id}#{uuid4().hex[:8]}"

    # 失败短路：本任务内该工具已确认故障 → 直接返回，不再执行检索链路。
    previous_failure = get_tool_failure(session_id, _TOOL_NAME)
    if previous_failure:
        logger.warning(
            f"本地知识库检索已短路（本任务内该工具已故障）：session_id={session_id}，"
            f"首次失败原因：{previous_failure}"
        )
        return (
            f"本地知识库检索失败，错误原因：{previous_failure}"
            "（该故障在本任务内已确认，重复调用不会成功。请勿重试，"
            "请如实向主智能体上报本次故障。）"
        )

    clarified = get_short_circuit(session_id, question)
    if clarified:
        logger.warning(
            "本地知识库检索已短路（本会话内该问题已被判定为需确认型号）："
            f"session_id={session_id}，问题={question[:60]}"
        )
        return _clarify_message(clarified)


    proceed, inflight_clarify = acquire_inflight(session_id)
    if not proceed:
        logger.warning(
            "本地知识库检索被确权闸门拦下（本任务已发生确权未完成，型号未确定前不再检索）："
            f"session_id={session_id}，问题={question[:60]}"
        )
        return _clarify_message(inflight_clarify)

    try:
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

        # 桥接在独立线程消费 pipeline 推送的事件，主线程执行检索本身
        with RagEventBridge(task_id):
            result_state = query_app.invoke(state)

        # 采集检索漏斗：必须用 session_id 作 key —— task_id 每次调用都变，
        # 用它会让收尾读不到任何指标（与故障短路的键选择同理）。
        # 无论后续走向哪条分支（确权未完成 / 无答案 / 正常），漏斗都已产生，故在此处先记录。
        funnel = result_state.get("retrieval_funnel") or {}
        if record_funnel(session_id, funnel):
            logger.info(f"已采集检索漏斗指标：session_id={session_id}，{funnel}")

        # 图片地址不在本工具的返回值里（汇总环节会丢），走会话级收集器带到收尾落库
        images = result_state.get("image_urls") or []
        if record_images(session_id, images):
            logger.info(f"已采集知识库配图：session_id={session_id}，{images}")

        answer = (result_state.get("answer") or "").strip()

        # 证据闸门拦截：整批证据被判为无关并归零 → 明确告知"没有相关资料"，
        # 注意判据顺序：闸门拦截时 reranked_docs 为空，但 answer 可能非空
        # （模型在 prompt 里看到"无本地证据"的提示后仍会生成话术），故必须优先判闸门。
        gate_blocked = bool(result_state.get("_evidence_gate_blocked"))
        if gate_blocked:
            logger.warning(
                f"本地知识库证据闸门拦截（本批判定为无关）：task_id={task_id}"
            )
            finish_inflight(session_id)
            return (
                "知识库没有相关资料。"
                "（本次检索命中的内容与问题相关性过低，已被证据闸门整体过滤，"
                "不是检索故障。请如实向主智能体上报「知识库中无相关资料」，不要据此推测或编造。）"
            )

        if answer and not (result_state.get("item_names") or []):
            logger.warning(
                f"本地知识库需要用户确认型号（确权未完成）：task_id={task_id}，知识库返回={answer[:80]}"
            )
            # 登记同问题短路：同会话内再问**完全相同**的问题时直接返回同一条反问；
            # 换角度/换措辞的问题不受影响（多角度检索是召回质量来源，不能被误伤）
            mark_short_circuit(session_id, question, answer)
            # 同时开闸：本任务内后续调用（含换措辞 / 换角度的问法）一律返回同一条反问
            mark_unresolved(session_id, answer)
            # 回填占位并唤醒等待者：它们拿到的会是逐字一致的同一条反问，且不再执行链路
            finish_inflight(session_id, answer)
            return _clarify_message(answer)

        if not answer:
            logger.warning(f"本地知识库检索未产出答案：task_id={task_id}")
            finish_inflight(session_id)
            return "本地知识库未返回任何内容，可能知识库中没有与该问题相关的资料。"
        # 正常完成：回填"未开闸"，等待方醒来后会被放行去跑各自的角度（多角度检索是设计行为）
        finish_inflight(session_id)
        return answer
    except Exception as e:
        # 其它失败不应中断整个智能体任务：转成中文提示交给模型继续处理
        logger.exception(f"本地知识库检索失败：task_id={task_id}，原因：{e}")
        # 标记本任务内该工具已故障 → 后续调用在入口直接短路，杜绝重试风暴。
        # 键必须是 session_id（thread_id），不能用 task_id（见 app/core/tool_failfast.py）
        mark_tool_failed(session_id, _TOOL_NAME, str(e))
        finish_inflight(session_id)
        return f"本地知识库检索失败，错误原因：{str(e)}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证本地 RAG 链路（需 Milvus/Mongo/Neo4j 可用）
    print(local_rag_search.invoke({"question": "HAK 180 烫金机怎么操作？"}))
