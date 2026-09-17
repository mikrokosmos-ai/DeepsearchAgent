import asyncio
import json
import sys

from agents.mcp import MCPServerStreamableHttp

from app.rag.conf.bailian_mcp_config import mcp_config
from app.rag.conf.query_pipeline_config import query_pipeline_config
from app.rag.pipelines.query_pipeline.state import resolve_trace_key
from app.utils.task_utils import add_running_task, add_done_task
from app.core.logger import logger, node_log, step_log

DASHSCOPE_BASE_URL_STREAMBLE = mcp_config.mcp_base_url
DASHSCOPE_API_KEY = mcp_config.api_key

# 联网搜索返回条数（来源：app/conf/query_pipeline_config.py，默认值等于改造前调用点的字面量 10）
# 说明：函数默认值与调用点统一使用本常量，避免同一个可调参数出现两处取值来源。
WEB_SEARCH_COUNT = query_pipeline_config.web_search_count

"""
  节点: 联网搜索（百炼 MCP）(node_web_search_mcp)
  入参:  rewritten_query  (+ task_id 用于追踪)
  出参:  web_search_docs（[{title,url,snippet,source}]）
  步骤:
       1. 参数校验（rewritten_query）
       2. 通过 MCP 调用 bailian_web_search 工具
       3. 解析 pages 结构
  可用性分级：联网检索是**补充能力**（证据 trust=low），
       远端 MCP 不可用时只告警并返回空结果，不阻断其余三路召回（与 node_query_kg 同款降级策略）。
"""


@step_log("step_1_data_validate")
def step_1_data_validate(state):
    rewritten_query = state['rewritten_query']
    if not rewritten_query:
        logger.error("rewritten_query不能为空!")
        raise ValueError("rewritten_query不能为空!")
    return rewritten_query


@step_log("node_web_search_mcp_async")
async def node_web_search_mcp_async(rewritten_query: str, count: int = WEB_SEARCH_COUNT):
    """
    使用openai的方式调用mcpserver提供的工具
    :param rewritten_query:
    :param count:
    :return:
    """
    # 1. 链接mcpserver服务
    mcp_server = MCPServerStreamableHttp(
        name="search_mcp",  # 随便写
        params={
            "url": DASHSCOPE_BASE_URL_STREAMBLE,
            "headers": {"Authorization": DASHSCOPE_API_KEY},
            "timeout": 300,
        },
        max_retry_attempts=3
    )

    try:
        # 2. 进行mcp_server链接
        await mcp_server.connect()
        # 3. 调用工具
        # https: // openai.github.io / openai - agents - python / ref / mcp / server /  # agents.mcp.server.MCPServer.call_tool
        tool_list = await mcp_server.list_tools()
        logger.info(f"工具列表:{tool_list}")
        mcp_result = await mcp_server.call_tool(
            tool_name="bailian_web_search",
            arguments={
                "query": rewritten_query,
                "count": count
            }
        )
        return mcp_result
    finally:
        # 4.释放本次链接资源
        await mcp_server.cleanup()


@node_log("node_web_search_mcp")
def node_web_search_mcp(state):
    """
    节点功能，调用外部搜索引擎补充信息
    :param state:
    :return:
    """
    # 1. 任务和认知
    add_running_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state["is_stream"])
    # 2. 获取数据和校验
    rewritten_query = step_1_data_validate(state)
    # 3. mcp的调用流程封装成一个异步函数
    #    结构化结果形如：
    #    {
    #        "type": "tool_call",
    #        "content": [
    #           {"text": "{\"pages\": [{\"title\":\"...\",\"url\":\"...\",\"snippet\":\"...\",\"source\":\"...\"}]}"}
    #        ]
    #    }
    pages = []
    try:
        mcp_result = asyncio.run(node_web_search_mcp_async(rewritten_query, count=WEB_SEARCH_COUNT))
        # 4. 结果解析
        text_dict = json.loads(mcp_result.content[0].text)
        pages = text_dict.get('pages', [])
        logger.info(f"联网搜索命中{len(pages)}条结果，明细：{pages}")
    except Exception as e:
        # 联网检索是可选补充能力：远端 MCP 超时/限流/返回结构异常时只告警，
        # 返回空结果让查询继续走其余三路召回（不阻断主链路）。
        logger.warning(f"联网搜索失败，已跳过该路召回：{e}", exc_info=True)
        pages = []
    # 记录任务结束
    add_done_task(resolve_trace_key(state), sys._getframe().f_code.co_name, state["is_stream"])
    return {"web_search_docs": pages}


if __name__ == '__main__':
    from app.rag.pipelines.query_pipeline.state import create_query_default_state

    test_state = create_query_default_state(
        session_id="xxxx",
        task_id="xxxx",
        is_stream=False,
        rewritten_query="HAK 180 在出厂默认状态下，若想在纸张上只把烫金膜转印到顶部 50 mm–170 mm 的局部区域，应在操作面板上如何设置"
    )

    # 调用 websearch_node 函数
    result_state = node_web_search_mcp(test_state)

    # 验证结果
    print("测试结果:")
    print(f"查询内容: {test_state.get('rewritten_query')}")
    print(f"查询内容: {result_state}")
