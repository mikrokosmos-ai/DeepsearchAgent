"""
工具调用次数预算（会话级硬护栏）
"""

import os
import threading
from typing import Dict, NamedTuple, Optional

# 「task」= 主智能体派发子智能体的工具名（DeepAgents 固定用它）
TASK_TOOL_NAME = "task"

# 每个会话、每个工具名允许的最大调用次数。
# 取值对齐既有提示词：网络搜索 5 次、本地知识库 5 次（`agents.yml` 明写）；
# 数据库三个工具提示词没写次数，按其"先列表→预览→查"的链路给一组不会误伤的宽松值。
TOOL_CALL_LIMITS: Dict[str, int] = {
    "internet_search": 5,
    "local_rag_search": 5,
    "list_sql_tables": 3,
    "get_table_data": 5,
    "execute_sql_query": 6,
}

# 「task」的两级预算：单助手预算落实「不重复派发」，总预算兜住整体失控。
TASK_PER_AGENT_LIMIT = 5
TASK_TOTAL_LIMIT = 16

# 环境变量覆盖前缀：TOOL_BUDGET_INTERNET_SEARCH / TOOL_BUDGET_TASK_TOTAL / ...
_ENV_PREFIX = "TOOL_BUDGET_"

# thread_id -> {计数键: 已用次数}；计数键 = 工具名，或 task 的 "task" / "task::{助手名}"
_usage: Dict[str, Dict[str, int]] = {}
# 保护字典操作：任务在事件循环线程重置，工具在 threadpool 线程读写，两者并发访问
_lock = threading.Lock()


class BudgetDecision(NamedTuple):
    """一次额度消费的结果。

    :param allowed: 是否放行
    :param scope: 计数键（工具名，或 task 的 `task` / `task::{助手名}`）
    :param used: 本次消费后已用次数；被拒时为当前值（未消费）
    :param limit: 对应上限
    """

    allowed: bool
    scope: str
    used: int
    limit: int


def _env_limit(env_key: str, default: int) -> int:
    """读取环境变量覆盖值；缺失/非法/非正数一律回退 default（绝不抛）。"""
    try:
        raw = os.getenv(_ENV_PREFIX + env_key)
        if raw is None or not str(raw).strip():
            return default
        value = int(str(raw).strip())
        return value if value > 0 else default
    except Exception:  # noqa: BLE001
        return default


def resolve_tool_limit(tool_name: str) -> Optional[int]:
    """返回某工具名在本会话的调用上限；不受限（或参数非法）返回 None。"""
    if not tool_name:
        return None
    default = TOOL_CALL_LIMITS.get(str(tool_name))
    if default is None:
        return None
    return _env_limit(str(tool_name).upper(), default)


def resolve_task_limits() -> tuple[int, int]:
    """返回 task 的 (单助手上限, 总上限)。"""
    return (
        _env_limit("TASK_PER_AGENT", TASK_PER_AGENT_LIMIT),
        _env_limit("TASK_TOTAL", TASK_TOTAL_LIMIT),
    )


def consume_tool_call(thread_id: str, tool_name: str) -> Optional[BudgetDecision]:
    """
    尝试为一次普通工具调用消费额度。

    :return: 受该工具限制时返回决策；不受限 / 参数非法 / 内部异常时返回 None（=允许，fail-open）
    """
    if not thread_id or not tool_name:
        return None
    try:
        limit = resolve_tool_limit(tool_name)
        if limit is None:
            return None
        key = str(tool_name)
        with _lock:
            bucket = _usage.setdefault(str(thread_id), {})
            used = bucket.get(key, 0)
            if used >= limit:
                return BudgetDecision(False, key, used, limit)
            bucket[key] = used + 1
            return BudgetDecision(True, key, used + 1, limit)
    except Exception:  # noqa: BLE001
        return None


def consume_task_call(thread_id: str, subagent_type: str) -> Optional[BudgetDecision]:
    """
    尝试为一次 `task`（派发子智能体）消费额度。

    两级检查，**先单项后总量**：单项被拒时给出的是"该助手已派发 N 次"这种更好操作的提示；
    任一级被拒则本次**不消费**（不增加任何计数），保持账目干净。

    :return: 决策；参数非法 / 内部异常时返回 None（=允许，fail-open）
    """
    if not thread_id:
        return None
    try:
        per_limit, total_limit = resolve_task_limits()
        agent_key = f"{TASK_TOOL_NAME}::{subagent_type or 'unknown'}"
        total_key = TASK_TOOL_NAME
        with _lock:
            bucket = _usage.setdefault(str(thread_id), {})
            used_agent = bucket.get(agent_key, 0)
            used_total = bucket.get(total_key, 0)
            if used_agent >= per_limit:
                return BudgetDecision(False, agent_key, used_agent, per_limit)
            if used_total >= total_limit:
                return BudgetDecision(False, total_key, used_total, total_limit)
            bucket[agent_key] = used_agent + 1
            bucket[total_key] = used_total + 1
            return BudgetDecision(True, agent_key, used_agent + 1, per_limit)
    except Exception:  # noqa: BLE001
        return None


def get_usage(thread_id: str) -> Dict[str, int]:
    """列出某会话当前的计数快照（供日志/调试/验证；返回副本；绝不抛异常）。"""
    try:
        with _lock:
            return dict(_usage.get(str(thread_id), {}))
    except Exception:  # noqa: BLE001
        return {}


def reset_budgets(thread_id: str) -> None:
    """
    清空某会话的计数（**新任务启动前必须调用**，绝不抛异常）。

    不调用会导致上一次任务的计数残留 —— 同一 thread_id 被复用时会**误拦**新任务的工具调用。
    """
    if not thread_id:
        return
    try:
        with _lock:
            _usage.pop(str(thread_id), None)
    except Exception:  # noqa: BLE001
        pass


def clear_budgets(thread_id: str) -> None:
    """
    任务收尾时清理该会话计数（绝不抛异常）。

    与 `reset_budgets` 行为等价 —— 分开命名只为让"启动重置 / 收尾清理"两个调用点的意图
    在代码里一目了然，与 `cancel.py`、`tool_failfast.py` 的命名保持对称。
    """
    reset_budgets(thread_id)
