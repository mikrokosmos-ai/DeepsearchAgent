"""
Tavily 网络搜索工具模块

封装 internet_search 工具，供网络搜索子智能体检索互联网公开信息。
"""

import json
import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from app.api.monitor import monitor
from app.core.logger import logger

load_dotenv()

# 单次搜索允许返回的最大条数（上限收敛，防止模型传超大值撑爆上下文）
MAX_RESULTS_CAP = 20

# 惰性单例：首次调用时才构造客户端，避免"缺 key → 导入期崩溃 → 整个服务起不来"
_client: TavilyClient | None = None


def get_client() -> TavilyClient:
    """
    惰性获取 Tavily 客户端（带缓存）。

    :raises RuntimeError: 未配置 TAVILY_API_KEY（由调用方捕获并转成中文提示）
    """
    global _client
    if _client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key or not api_key.strip():
            raise RuntimeError("未配置环境变量 TAVILY_API_KEY")
        _client = TavilyClient(api_key=api_key)
    return _client


def _clamp_max_results(value) -> int:
    """把 max_results 收敛到 1..MAX_RESULTS_CAP，非法值回退 5。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    return max(1, min(number, MAX_RESULTS_CAP))


# @tool 会把函数签名和 docstring 暴露给 DeepAgents，模型据此决定是否调用以及如何填参
@tool
def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
) -> str:
    """
    根据用户问题检索互联网公开信息

    注意：本工具只用于外部公开网页、新闻、政策等信息，不用于查询业务数据库中的内部数据
    :param query: 搜索关键词或自然语言问题
    :param topic: 搜索主题，可选 news、finance、general
    :param max_results: 返回的最大结果数（1~20，超出会被收敛）
    :param include_raw_content: 是否返回网页原文内容；False 返回摘要，True 尝试返回更完整正文
    :return: JSON 文本 {"query":...,"results":[{"title","url","content","score"},...]}
             失败时返回中文提示（如「网络搜索失败，错误原因：...」），不会抛出
    """
    # 工具内部埋点比外层 stream 解析更直接：只要工具被调用，前端就能看到本次搜索参数
    # 这里只上报查询参数，不上报搜索结果正文，避免监控事件体过大
    try:
        monitor.report_tool(
            tool_name="网络搜索工具",
            args={
                "query": query,
                "topic": topic,
                "max_results": max_results,
                "include_raw_content": include_raw_content,
            },
        )
    except Exception as e:  # 埋点失败不应影响检索本身
        logger.warning(f"[tavily] 监控上报失败：{e}")

    if not isinstance(query, str) or not query.strip():
        return "搜索失败：query 不能为空。"

    if topic not in ("news", "finance", "general"):
        topic = "general"

    capped = _clamp_max_results(max_results)
    if capped != max_results:
        logger.info(f"[tavily] max_results={max_results} 已收敛为 {capped}")

    try:
        client = get_client()
        payload = client.search(
            query=query,
            topic=topic,
            max_results=capped,
            include_raw_content=include_raw_content,
        )
    except Exception as e:
        # 额度类错误给出可操作提示，其余如实上报；一律不抛出（否则会击穿整张图）
        text = str(e)
        logger.warning(f"[tavily] 检索失败：{type(e).__name__}: {text}")
        lowered = text.lower()
        if any(k in lowered for k in ("usage limit", "quota", "exceed", "limit")):
            return (
                "网络搜索失败，错误原因：Tavily 调用额度已用尽或超限"
                f"（原始信息：{text}）。请勿重复重试，如实向主智能体上报该情况。"
            )
        return f"网络搜索失败，错误原因：{type(e).__name__}: {text}"

    # 统一成 JSON 文本：字段边界明确，且避免 dict 被框架字符串化成 Python repr
    try:
        results = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "content": item.get("content"),
                "score": item.get("score"),
            }
            for item in (payload or {}).get("results", [])
        ]
        return json.dumps(
            {"query": query, "topic": topic, "result_count": len(results), "results": results},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.warning(f"[tavily] 结果序列化失败，回退原始文本：{e}")
        return str(payload)


if __name__ == "__main__":
    from pprint import pprint

    # 本地调试入口：直接运行本文件可验证 TAVILY_API_KEY 和 Tavily API 是否可用
    pprint(
        internet_search.invoke(
            {"query": "2026中国法定节假日放假安排表，我天天都想要放假"}
        )
    )
