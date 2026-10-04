"""
主智能体运行元数据访问层
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.rag.clients.mongo_client import get_history_mongo_tool

# 单次查询返回的运行记录上限（防止一次拉爆上下文/内存）
DEFAULT_RUNS_LIMIT = 50


def save_agent_run(
    session_id: str,
    outcome: str,
    elapsed_ms: int,
    recursion_limit: Optional[int] = None,
    tool_usage: Optional[Dict[str, int]] = None,
    failed_tools: Optional[Dict[str, str]] = None,
    report_summary: Optional[Dict[str, Any]] = None,
    retrieval_funnel: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    写入一条任务运行元数据

    :param session_id: 任务 ID（thread_id）
    :param outcome: 结局，取值 success / cancelled / recursion_limit / error / skipped_cancelled
    :param elapsed_ms: 本次任务耗时（毫秒）
    :param recursion_limit: 本次生效的图轮次上限（便于事后解释"为什么在 200 步被截断"）
    :param tool_usage: 各工具调用次数快照（`tool_budget.get_usage`）
    :param failed_tools: 本次故障短路的工具 -> 首因（`tool_failfast.get_failed_tools`）
    :param report_summary: 子智能体返回契约汇总（`subagent_reports.summarize`）
    :param retrieval_funnel: 检索漏斗指标（各通道召回量 / 跨路一致性 / 逐段淘汰数）
    :return: 是否写入成功；任何异常都返回 False 且不抛出
    """
    try:
        usage = dict(tool_usage or {})
        failed = dict(failed_tools or {})
        summary = dict(report_summary or {})
        truncated = list(summary.get("truncated") or [])
        # 漏斗指标可能含 set 等非 BSON 类型；统一归一化为纯基础类型
        funnel = _normalize_funnel(retrieval_funnel)

        doc = {
            "session_id": str(session_id),
            # 与 chat_message / agent_message 保持同一时间基准（timestamp，便于排序与聚合）
            "ts": datetime.now().timestamp(),
            "outcome": str(outcome or "unknown"),
            "elapsed_ms": int(elapsed_ms or 0),
            "recursion_limit": recursion_limit,
            "tool_usage": usage,
            # 直接落一份派发次数：这是最常被问的指标，不必让查询方自己从 usage 里挖
            "task_calls": int(usage.get("task", 0)),
            "failed_tools": failed,
            "failed_count": len(failed),
            "reports": {
                "count": int(summary.get("count") or 0),
                "truncated": truncated,
                "failed": list(summary.get("failed") or []),
                "unparsed": list(summary.get("unparsed") or []),
            },
            "truncated_any": bool(truncated),
            "retrieval_funnel": funnel,
        }

        mongo_tool = get_history_mongo_tool()
        mongo_tool.agent_run.insert_one(doc)
        return True
    except Exception as e:  # noqa: BLE001
        # 收尾路径上绝不抛：写失败只记录，不影响任务本身的成败判定
        logger.warning(f"保存任务运行元数据失败（不影响任务本身）：session={session_id}，原因：{e}")
        return False


def _normalize_funnel(funnel: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    把漏斗指标归一化为可 BSON 序列化的结构（set -> list，dict 递归）。

    :param funnel: 原始漏斗指标（可能含 set / 非基础类型）
    :return: 纯基础类型结构；非法输入返回 {}
    """
    if not isinstance(funnel, dict):
        return {}

    def _conv(value):
        if isinstance(value, dict):
            return {str(k): _conv(v) for k, v in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(str(v) for v in value)
        if isinstance(value, (list, tuple)):
            return [_conv(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    return _conv(funnel)


def get_agent_runs(session_id: str, limit: int = DEFAULT_RUNS_LIMIT) -> List[Dict[str, Any]]:
    """
    读取指定会话的运行记录（按时间正序，便于直接看"这个会话越跑越好还是越跑越差"）

    :param session_id: 会话唯一标识
    :param limit: 最多返回条数
    :return: 记录列表；查询失败返回空列表
    """
    mongo_tool = get_history_mongo_tool()
    try:
        query = {"session_id": session_id}
        # 与 history_repo 的读法一致：先按 ts 降序取最近 N 条，再反转成正序。
        # 必须带 _id 作为第二排序键 —— Windows 上 datetime.now() 粒度约 15.6ms，
        #  同一 tick 的两条记录 ts 相同，只按 ts 排序时并列顺序是不确定的。
        cursor = (
            mongo_tool.agent_run.find(query)
            .sort([("ts", -1), ("_id", -1)])
            .limit(limit)
        )
        runs = list(cursor)
        runs.reverse()
        for run in runs:
            run.pop("_id", None)
        return runs
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取任务运行元数据失败：session={session_id}，原因：{e}")
        return []


def get_recent_run_stats(limit: int = DEFAULT_RUNS_LIMIT) -> Dict[str, Any]:
    """
    跨会话聚合最近 N 条运行记录，给出可直接用于汇报的指标

    这是"评测基线"的原料：改动前后各跑一轮，对比这里的数字即可判断有没有变好。

    :param limit: 参与聚合的最近记录数
    :return: {"runs": n, "outcomes": {...}, "avg_elapsed_ms": ..., "avg_task_calls": ...,
              "truncated_runs": n, "failed_runs": n}
              查询失败时返回各字段为 0/空的结构（调用方无需判 None）
    """
    empty: Dict[str, Any] = {
        "runs": 0,
        "outcomes": {},
        "avg_elapsed_ms": 0,
        "avg_task_calls": 0.0,
        "truncated_runs": 0,
        "failed_runs": 0,
    }
    mongo_tool = get_history_mongo_tool()
    try:
        cursor = mongo_tool.agent_run.find({}).sort([("ts", -1), ("_id", -1)]).limit(limit)
        runs = list(cursor)
        if not runs:
            return empty

        outcomes: Dict[str, int] = {}
        for run in runs:
            key = str(run.get("outcome") or "unknown")
            outcomes[key] = outcomes.get(key, 0) + 1

        n = len(runs)
        return {
            "runs": n,
            "outcomes": outcomes,
            "avg_elapsed_ms": int(sum(int(r.get("elapsed_ms") or 0) for r in runs) / n),
            "avg_task_calls": round(sum(int(r.get("task_calls") or 0) for r in runs) / n, 2),
            "truncated_runs": sum(1 for r in runs if r.get("truncated_any")),
            "failed_runs": sum(1 for r in runs if int(r.get("failed_count") or 0) > 0),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"聚合任务运行元数据失败：{e}")
        return empty
