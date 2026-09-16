"""
流水线进度事件桥接层

本地 RAG 的两条链路都通过 SSE 队列（`app.utils.sse_utils.push_to_session`）
向上层推送**节点进度**（导入链路还会推送流式增量）：

    · 检索链路 `app/pipelines/query_pipeline` —— 节点进度 + 答案流式增量；
    · 入库链路 `app/pipelines/import_pipeline` —— 节点进度。

而前端的事件统一走 `monitor` 的 WebSocket 通道。本模块负责把前者转换成后者，
让前端在「知识导入」页与「对话」页都能实时看到节点进度，而**不需要为流水线
单独开推送通道**。

设计要点：
    1. **不修改任何 pipeline 代码** —— 它们继续按原有方式 push_to_session；
    2. 桥接在**独立线程**消费 SSE 队列：pipeline 是同步阻塞执行（`invoke`），
       不能在同一线程里边跑边消费，否则事件会堆积到执行结束才被读到；
    3. 生命周期与调用方严格对齐（上下文管理器）：进入时注册队列并起消费线程，
       退出时先停线程再释放队列，避免队列泄漏与后续调用串台；
    4. **消费线程必须恢复 thread_id**：裸 `threading.Thread` 不会继承 ContextVar，
       否则 `monitor` 取不到推送目标而静默丢弃事件（详见 `__init__` 注释）；
    5. **delta 默认不转发**：一条答案会产生数百个 token 级增量事件，逐条推送会
       同时拖慢 WebSocket 与刷屏日志。需要流式打字机效果时可显式开启；
    6. `event_prefix` / `topic` 让同一套桥接服务两条链路，且前端可按前缀分流
       （`rag_*` = 检索进度，`kb_*` = 导入进度）。
"""

import queue
import threading
from typing import Any, Dict, Optional, Tuple

from app.api.context import get_thread_context, set_thread_context
from app.api.monitor import monitor
from app.core.logger import logger
from app.utils.sse_utils import SSEEvent, create_sse_queue, get_sse_queue, remove_sse_queue

# 消费线程的轮询间隔（秒）：既保证事件及时转发，又避免空转占满 CPU
_POLL_INTERVAL = 0.2
# 退出时等待消费线程收尾的最长时间（秒）
_JOIN_TIMEOUT = 2.0


def _to_monitor_event(
    event: str,
    data: Dict[str, Any],
    prefix: str = "rag",
    topic: str = "本地知识库",
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """
    把 pipeline 的 SSE 事件映射为 monitor 事件三元组。

    :param event: SSE 事件类型（`SSEEvent.*`）
    :param data: SSE 事件负载
    :param prefix: monitor 事件前缀（`rag` = 检索链路，`kb` = 入库链路）
    :param topic: 面向前端的业务名（用于拼装说明文案）
    :return: (monitor 事件类型, 面向前端的说明, 结构化数据)；无需转发时返回 None
    """
    data = data or {}

    if event == SSEEvent.PROGRESS:
        # 节点进度：task_utils.task_push_queue 推的是中文节点名列表
        done_list = data.get("done_list") or []
        running_list = data.get("running_list") or []
        running_text = "、".join(running_list) if running_list else "（等待中）"
        return (
            f"{prefix}_progress",
            f"{topic}处理中：{running_text}",
            {
                "status": data.get("status", ""),
                "done_list": done_list,
                "running_list": running_list,
            },
        )

    if event == SSEEvent.FINAL:
        image_urls = data.get("image_urls") or []
        return (
            f"{prefix}_final",
            f"{topic}处理完成",
            {
                "status": data.get("status", "completed"),
                "image_urls": image_urls,
                # 答案正文由调用方返回值携带，这里不回传全文，避免事件体过大
                "answer_length": len(data.get("answer") or ""),
            },
        )

    if event == SSEEvent.DELTA:
        # token 级流式增量：**是否转发由 PipelineEventBridge.forward_delta 决定**，
        # 本函数只负责给出映射结果（默认关闭时调用方会在映射之前就跳过）。
        delta = data.get("delta") or ""
        if not delta:
            return None
        return (f"{prefix}_delta", f"{topic}正在生成内容", {"delta": delta})

    if event == SSEEvent.ERROR:
        return (f"{prefix}_error", f"{topic}出错：{data.get('error', '')}", {"error": data.get("error", "")})

    # READY / CLOSE 等生命周期事件不转发
    return None


class PipelineEventBridge:
    """
    流水线事件桥接上下文管理器

    用法：
        with PipelineEventBridge(task_id) as bridge:
            query_app.invoke(state)            # 期间产生的事件会被转发给 monitor

        with PipelineEventBridge(task_id, event_prefix="kb", topic="知识库导入"):
            kb_import_app.invoke(state)

    :param task_id: 与 pipeline state 中 `task_id` 一致的追踪 key（SSE 队列 key）
    :param forward_delta: 是否转发 token 级流式增量（默认关闭，见模块 docstring）
    :param event_prefix: monitor 事件前缀（默认 `rag`）
    :param topic: 面向前端的业务名（默认「本地知识库」）
    """

    def __init__(
        self,
        task_id: str,
        forward_delta: bool = False,
        event_prefix: str = "rag",
        topic: str = "本地知识库",
    ):
        self.task_id = task_id
        self.forward_delta = forward_delta
        self.event_prefix = event_prefix
        self.topic = topic
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._forwarded = 0
        # 关键：必须在**调用方线程**里捕获 thread_id。
        # 消费线程是独立裸线程，ContextVar 不会随线程创建而继承；若不显式恢复，
        # `monitor._emit` 内部的 `get_thread_context()` 取到 None，事件会被静默丢弃
        # （现象：前端收不到 rag_progress / kb_progress，而 tool_start 等仍然正常 ——
        #  后者在 LangChain 线程池线程里执行，context 已被 copy_context 复制过）。
        self._thread_id = get_thread_context()

    # ------------------------------------------------------------------
    # 内部：事件转发
    # ------------------------------------------------------------------
    def _forward(self, msg: Dict[str, Any]) -> None:
        """转发单条 SSE 消息（消费线程与同步收尾共用）"""
        event = msg.get("event")
        if event == SSEEvent.DELTA and not self.forward_delta:
            return
        mapped = _to_monitor_event(event, msg.get("data") or {}, self.event_prefix, self.topic)
        if mapped is None:
            return
        event_type, message, data = mapped
        monitor.report_custom(event_type, message, data)
        self._forwarded += 1

    def _consume(self, event_queue: "queue.Queue") -> None:
        """在独立线程里持续取事件并转发给 monitor"""
        # 恢复会话上下文：新建线程的 ContextVar 是默认值，必须显式回填，
        # 否则 monitor 无法定位 thread_id，事件会被静默丢弃
        if self._thread_id:
            set_thread_context(self._thread_id)
        while not self._stop.is_set():
            try:
                msg = event_queue.get(timeout=_POLL_INTERVAL)
            except queue.Empty:
                continue
            try:
                self._forward(msg)
            except Exception as e:  # noqa: BLE001
                # 单个事件转发失败不应打断整条链路
                logger.warning(f"[{self.event_prefix}] 事件转发失败（已跳过）：event={msg.get('event')}，原因：{e}")

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------
    def __enter__(self) -> "PipelineEventBridge":
        event_queue = create_sse_queue(self.task_id)
        self._thread = threading.Thread(
            target=self._consume,
            args=(event_queue,),
            name=f"{self.event_prefix}-event-bridge-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        # 先停消费线程，再释放队列：顺序反了会让线程读到已被移除的空队列而空转
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=_JOIN_TIMEOUT)
        # 兜底把队列里尚未转发的事件捞干净（invoke 结束后可能仍有残留）
        self._drain_remaining()
        remove_sse_queue(self.task_id)
        logger.info(
            f"[{self.event_prefix}] 事件桥接结束：task_id={self.task_id}，累计转发 {self._forwarded} 条事件"
        )
        # 不吞异常：桥接只是旁路，业务异常照常向上抛出
        return False

    def _drain_remaining(self) -> None:
        """把队列中剩余事件转发完毕（消费线程已停止，此处为同步收尾）"""
        event_queue = get_sse_queue(self.task_id)
        if event_queue is None:
            return
        while True:
            try:
                msg = event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._forward(msg)
            except Exception:  # noqa: BLE001
                continue


# 向后兼容别名：检索链路在阶段 5 以 RagEventBridge 之名接入并通过验收，
# 保留该名字可避免既有调用点与测试脚本失效。
RagEventBridge = PipelineEventBridge
