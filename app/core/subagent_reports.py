"""
子智能体返回契约的收集与汇总（可观测性的落点）
"""

import threading
from typing import Any, Dict, List, Optional

# 每会话保留的报告条数上限（只用于展示与汇总，超出丢弃最旧的）
MAX_REPORTS_PER_SESSION = 50

# thread_id -> 报告列表（每条是一个扁平 dict，便于直接 JSON 序列化推给前端）
_reports: Dict[str, List[Dict[str, Any]]] = {}
_lock = threading.Lock()


def record_report(
    thread_id: Optional[str],
    subagent: str,
    report: Any = None,
    result_chars: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    记录一次子智能体返回，并返回可直接推给前端的扁平 payload。

    :param thread_id: 会话 id；为空则**不记录**并返回 None（上游据此跳过上报）
    :param subagent: 子智能体名（如「网络搜索助手」）
    :param report: `SubAgentReport` 实例；**None 表示契约块缺失或解析失败**（属可接受降级）
    :param result_chars: 原始返回文本长度（用于观察"模型是不是干脆没给代码块"）
    :return: payload dict；无会话 key 或内部异常时返回 None（绝不抛）
    """
    if not thread_id:
        return None
    try:
        parsed = report is not None
        payload: Dict[str, Any] = {
            "subagent": str(subagent or "未知助手"),
            "parsed": parsed,
            "result_chars": int(result_chars or 0),
            "sources": len(getattr(report, "sources", []) or []) if parsed else 0,
            "truncated_by_limit": bool(getattr(report, "truncated_by_limit", False)) if parsed else False,
            "error": (getattr(report, "error", None) if parsed else None),
        }
        with _lock:
            bucket = _reports.setdefault(str(thread_id), [])
            bucket.append(payload)
            if len(bucket) > MAX_REPORTS_PER_SESSION:
                del bucket[: len(bucket) - MAX_REPORTS_PER_SESSION]
        return payload
    except Exception:  # noqa: BLE001
        return None


def get_reports(thread_id: Optional[str]) -> List[Dict[str, Any]]:
    """列出某会话已记录的报告（返回副本；绝不抛异常）。"""
    if not thread_id:
        return []
    try:
        with _lock:
            return [dict(item) for item in _reports.get(str(thread_id), [])]
    except Exception:  # noqa: BLE001
        return []


def summarize(thread_id: Optional[str]) -> Dict[str, Any]:
    """
    汇总某会话的报告，供任务收尾日志使用。

    :return: {"count": 报告数, "truncated": [助手名...], "failed": [助手名...], "unparsed": [助手名...]}
             无会话 key 时返回各字段为空的结构（调用方无需判 None）
    """
    empty = {"count": 0, "truncated": [], "failed": [], "unparsed": []}
    if not thread_id:
        return empty
    try:
        items = get_reports(thread_id)
        truncated: List[str] = []
        failed: List[str] = []
        unparsed: List[str] = []
        for item in items:
            name = str(item.get("subagent") or "未知助手")
            if not item.get("parsed"):
                unparsed.append(name)
                continue
            if item.get("error"):
                failed.append(f"{name}（{str(item['error'])[:60]}）")
            if item.get("truncated_by_limit"):
                truncated.append(name)
        return {"count": len(items), "truncated": truncated, "failed": failed, "unparsed": unparsed}
    except Exception:  # noqa: BLE001
        return empty


def reset_reports(thread_id: Optional[str]) -> None:
    """
    清空某会话的报告（**新任务启动前必须调用**，绝不抛异常）。

    不清理会让上一次任务的"某路失败/被截断"残留到新任务的汇总里 —— thread_id 跨天复用。
    """
    if not thread_id:
        return
    try:
        with _lock:
            _reports.pop(str(thread_id), None)
    except Exception:  # noqa: BLE001
        pass


def clear_reports(thread_id: Optional[str]) -> None:
    """
    任务收尾时清理该会话报告（绝不抛异常）。

    与 `reset_reports` 行为等价，分开命名只为让「启动重置 / 收尾清理」两个调用点意图明确，
    与 `cancel.py`、`tool_budget.py` 的命名保持对称。
    """
    reset_reports(thread_id)
