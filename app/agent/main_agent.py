"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent 作为后续 API 层调用的统一入口。运行时还会为每个
session_id 创建独立工作目录，并把工具调用、子智能体调用和最终结果推送给前端。
"""

import asyncio
import os
import shutil
import time

from deepagents import create_deep_agent
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langgraph.errors import GraphRecursionError

from app.agent.llm import model
from app.agent.middleware.subagent_report_middleware import SubAgentReportMiddleware
from app.agent.middleware.tool_budget_middleware import ToolBudgetMiddleware
from app.prompts.agent_loader import main_agent_content
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.general_purpose_agent import general_purpose_agent
from app.agent.subagents.local_knowledge_agent import local_knowledge_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
    set_user_context,
)
from app.api.monitor import monitor
from app.core.cancel import TaskCancelledError, clear_cancel, is_cancelled
from app.core.logger import logger
# checkpointer 由记忆层工厂决定：优先 Redis 持久化，Redis 不可用时回退进程内内存实现
from app.core.memory.checkpointer import build_checkpointer
# 统一会话消息层：L0（图状态）为空时用它补齐上下文，见 _build_history_messages
from app.core.memory import conversation_repo
# 知识库配图：与检索漏斗同一条「子图 state → 主智能体收尾」通道
from app.core.knowledge_image_store import (
    clear_images as clear_knowledge_images,
    get_images as get_knowledge_images,
    reset_images as reset_knowledge_images,
)
from app.core.retrieval_funnel_store import (
    clear_funnel as clear_retrieval_funnel,
    get_funnel as get_retrieval_funnel,
    reset_funnel as reset_retrieval_funnel,
)
from app.core.tool_failfast import clear_tool_failures, get_failed_tools
from app.core.subagent_reports import (
    clear_reports as clear_subagent_reports,
    summarize as summarize_subagent_reports,
)
from app.core.tool_budget import clear_budgets, get_usage as get_budget_usage
from app.core.paths import PROJECT_ROOT
# 用别名导入：函数体内的局部变量名恰好是 session_dir，若直接导入同名函数会因为
# 「函数作用域内存在赋值」而被 Python 判定为局部变量 → 调用处 UnboundLocalError。
from app.core.runtime_paths import UPDATED_SESSIONS_DIR, session_dir as resolve_session_dir
# 主智能体的问答历史（独立集合 agent_message，与 RAG 多轮历史分离）
from app.rag.repositories.history_repo import save_agent_message
# 每次任务的运行元数据（独立集合 agent_run），供可观测性与评测（§5）
from app.rag.repositories.run_repo import save_agent_run

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content


VIRTUAL_FS_TOOLS = frozenset(
    {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}
)

# 单次任务的图轮次上限。
DEFAULT_RECURSION_LIMIT = 200


def _recursion_limit() -> int:
    """读取图轮次上限；缺失/非法/非正数一律回退默认（绝不抛）。"""
    raw = os.getenv("AGENT_RECURSION_LIMIT")
    if raw is None or not str(raw).strip():
        return DEFAULT_RECURSION_LIMIT
    try:
        value = int(str(raw).strip())
        return value if value > 0 else DEFAULT_RECURSION_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_RECURSION_LIMIT

# 一次性注册本项目的运行时策略（键用 provider，避免 .env 里模型改名后静默失配）：
register_harness_profile(
    "openai",
    HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_tools=VIRTUAL_FS_TOOLS,
    ),
)

# 主智能体是调度中心：tools 只放最终交付相关的文件工具，信息获取一律交给 subagents；
# middleware 均为纯增量（会话级次数护栏 + 子智能体返回契约观察者）。
# checkpointer 的实现由记忆层工厂决定（Redis 优先、不可用时回退 InMemorySaver），
# 此处只负责保证同一 thread_id 复用同一份执行上下文 —— 见 app/core/memory/checkpointer.py。
main_agent = create_deep_agent(
    model=model,
    system_prompt=main_agent_content["system_prompt"],
    tools=[generate_markdown, convert_md_to_pdf, read_file_content],
    # 会话级次数护栏：管 `task`（派发子智能体）的总量与单助手额度
    middleware=[ToolBudgetMiddleware(), SubAgentReportMiddleware()],
    checkpointer=build_checkpointer(),
    subagents=[
        general_purpose_agent,
        database_query_agent,
        network_search_agent,
        local_knowledge_agent,
    ],
)

# 会话工作区与上传暂存的目录契约统一由 app.core.runtime_paths 提供，
# 本模块只消费 resolve_session_dir() / UPDATED_SESSIONS_DIR，不再自行拼接路径。


async def _build_history_messages(config, session_id):
    """
    L0 无历史时，用 L1（统一会话层）补齐上下文
    """
    state_getter = getattr(main_agent, "aget_state", None)
    if state_getter is None:
        return []
    try:
        snapshot = await state_getter(config)
    except Exception as e:  # noqa: BLE001  读不到状态不能阻断任务
        logger.warning(f"[Memory] 读取图状态失败，按已有历史处理（不注入 L1）：{e}")
        return []
    values = getattr(snapshot, "values", None) or {}
    if values.get("messages"):
        return []
    messages = conversation_repo.build_context_messages(session_id)
    if messages:
        logger.info(
            f"[Memory] L0 无历史，用 L1 补齐上下文：session={session_id}，条数={len(messages)}"
        )
    return messages


async def run_deep_agent(task_query, session_id, user_id=None):
    """
    异步流式执行主智能体

    API 层会为每次任务传入用户问题和 session_id。本函数负责准备会话目录、
    复制上传文件、写入 ContextVar，并在流式执行过程中把关键事件上报给前端。
    :param task_query: 前端提交的原始任务问题
    :param session_id: 当前任务 ID，同时用于 thread_id、输出目录和 WebSocket 定向推送
    :param user_id: 前端持久化的稳定用户 ID（可选）；缺省时记忆按会话级处理，不报错
    """
    logger.info(f"[MainAgent] 开始执行会话，session_id={session_id}")

    # 运行元数据：进入函数即开始计时，收尾时连同结局一起写入 agent_run 集合
    started_at = time.perf_counter()
    # 检索漏斗：thread_id 跨任务复用，启动前必须清一次 ——
    # 否则上一次任务的漏斗会残留，被本次收尾当成"本次指标"写入（静默失真）。
    reset_retrieval_funnel(session_id)
    # 同上：thread_id 跨任务复用，图片收集器不清理会把上一轮的图挂到本轮答案上
    reset_knowledge_images(session_id)
    # 结局标记由各分支显式设置；默认 unknown 便于暴露"漏设"（收尾时读它落库）
    outcome = "unknown"

    # 每个会话独立使用 output/sessions/session_{session_id}，避免不同用户的产物互相覆盖。
    # resolve_session_dir() 内含旧路径只读兼容
    session_dir = resolve_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    # 前端和工具使用绝对路径；提示词里只给模型相对路径，降低模型误用系统绝对路径的概率
    session_dir_str = str(session_dir).replace("\\", "/")
    relative_session_dir_str = str(session_dir.relative_to(PROJECT_ROOT)).replace(
        "\\", "/"
    )

    # 上传文件先落在 updated/session_{session_id}，执行前复制到本次 output 工作目录
    # 这样读文件工具和生成文件工具都只需要围绕同一个 session_dir 工作
    updated_dir_path = UPDATED_SESSIONS_DIR / f"session_{session_id}"
    updated_info_prompt = ""
    if updated_dir_path.exists():
        files = [f.name for f in updated_dir_path.iterdir() if f.is_file()]
        if files:
            for filename in files:
                # copy2 会保留上传文件的修改时间、权限等元数据，便于后续排查文件来源
                shutil.copy2(updated_dir_path / filename, session_dir / filename)

            # 把上传文件列表注入用户消息，提醒模型先调用 read_file_content 获取附件内容
            updated_info_prompt = (
                "\n    [已上传文件] 已加载到工作目录:\n"
                + "\n".join([f"    - {f}" for f in files])
                + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
            )

    # ContextVar 让深层工具无需显式传参，也能拿到当前会话目录和 WebSocket thread_id
    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(session_id)
    # 用户身份同样是请求级横切信息：落库时由会话仓储直接取，不必逐层透传
    user_id_token = set_user_context(user_id)

    # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
    monitor.report_session_dir(session_dir_str)

    # checkpointer 依赖 thread_id 区分会话记忆；同一 session_id 会复用同一条执行上下文
    config = {
        "configurable": {"thread_id": session_id},
        # 显式收敛图轮次上限：覆盖 deepagents 绑定的 9999（等于无上限），
        # 防止模型在「派发 → 失败 → 再派发」里空转，烧掉时间与额度
        "recursion_limit": _recursion_limit(),
    }

    # 工作环境指令是运行时动态补充的，约束模型只在当前会话目录读写文件
    path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """

    try:
        # 连 astream 都不启动，避免为一个已取消的任务白跑一次模型调用。
        if is_cancelled(session_id):
            outcome = "skipped_cancelled"
            logger.info(f"[MainAgent] 任务在启动前已被取消，跳过执行：session_id={session_id}")
            monitor.report_task_cancelled()
            return

        # L1 必须在「本轮用户消息落库之前」组装：否则刚写入的本轮会被当成历史，
        # 与下面 payload 里的本轮一起进上下文，同一句话出现两遍。
        history_messages = await _build_history_messages(config, session_id)

        # 把用户提问落库，使刷新/断线后仍能看到完整问答（写入失败不影响任务）
        save_agent_message(session_id, "user", task_query)

        # astream 会持续产出模型节点、工具节点和子智能体节点的状态片段
        async for chunk in main_agent.astream(
            {
                "messages": [
                    *history_messages,
                    {"role": "user", "content": task_query + path_instruction},
                ]
            },
            config=config,
        ):
            # 协作式取消检查点：astream 的每一轮都是一次可中断边界。
            if is_cancelled(session_id):
                outcome = "cancelled"
                logger.info(f"[MainAgent] 检测到取消请求，停止流式执行：session_id={session_id}")
                monitor.report_task_cancelled()
                return

            # chunk 形如 {"model": {"messages": [...]}}，这里主要关心模型最新消息
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                messages = state["messages"]
                if messages and isinstance(messages, list):
                    last_msg = messages[-1]
                    if node_name == "model":
                        if last_msg.tool_calls:
                            # DeepAgents 调用子智能体时，本质上会产生名为 task 的工具调用
                            for tool_call in last_msg.tool_calls:
                                if tool_call["name"] == "task":
                                    # 子智能体调用单独上报，前端可以展示“正在调用哪个专家助手”
                                    monitor.report_assistant(
                                        tool_call["args"]["subagent_type"],
                                        {
                                            "description": tool_call["args"][
                                                "description"
                                            ]
                                        },
                                    )
                        elif last_msg.content:
                            # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                            logger.info(
                                f"主智能体执行结果，最终结果：{last_msg.content[:100]}"
                            )
                            # 先落库再推送 —— 前端刷新/断线后可从 /api/history 回读
                            # 图片不在主智能体可见的返回值里，只能从会话级收集器取
                            save_agent_message(
                                session_id,
                                "assistant",
                                last_msg.content,
                                image_urls=get_knowledge_images(session_id),
                            )
                            monitor.report_task_result(last_msg.content)

        # 流式执行自然结束 = 本次任务成功走完（无异常、未被取消）
        outcome = "success"

    except TaskCancelledError as e:
        # 节点边界检出的协作式取消：与 asyncio.CancelledError 同样上报取消事件，
        outcome = "cancelled"
        logger.info(f"[MainAgent] 任务已按用户请求取消：session_id={session_id}，{e}")
        monitor.report_task_cancelled()
    except asyncio.CancelledError:
        outcome = "cancelled"
        monitor.report_task_cancelled()
        raise
    except GraphRecursionError:
        # 达到图轮次上限：给用户可读提示
        # （框架原文是一句英文 + 文档链接，对前端用户没有意义）
        outcome = "recursion_limit"
        logger.warning(
            f"主智能体达到图轮次上限：session_id={session_id}，"
            f"recursion_limit={config.get('recursion_limit')}"
        )
        monitor.report_custom(
            "error",
            f"本次任务达到执行轮次上限（{config.get('recursion_limit')} 步）已停止。"
            f"请把问题拆得更具体后重试；如需处理更长的任务，可调大环境变量 AGENT_RECURSION_LIMIT。",
        )
    except Exception as e:
        # 异步执行异常也走 monitor，保证前端能收到明确错误事件；
        # 同时把完整堆栈写入服务端日志 —— 只上报异常摘要会让线上排查无从下手
        #  前端仅显示 "'ascii' codec can't encode ..."，无法定位到具体 header）。
        outcome = "error"
        logger.exception(f"主智能体执行异常：{e}")
        monitor.report_custom("error", f"执行主智能发生异常信息：{str(e)}")
    finally:
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或 thread_id
        reset_session_context(session_dir_token, session_id_token, user_id_token)
        # 清理协作式取消标志，避免内存态随会话数累积
        clear_cancel(session_id)
        # clear_budgets / clear_tool_failures / clear_subagent_reports 会把本次任务的
        # 计数与标记清空，先清再取就只能拿到空值 —— 这条顺序是本模块最容易踩的坑。
        report_summary = summarize_subagent_reports(session_id)
        tool_usage = get_budget_usage(session_id)
        failed_tools = get_failed_tools(session_id)
        # 检索漏斗：由 local_rag_tool 在每次检索后写入会话级收集器，此处取最后一次
        retrieval_funnel = get_retrieval_funnel(session_id)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        # 落库函数绝不抛；写失败只记 warning，不影响任务本身的成败判定
        save_agent_run(
            session_id=session_id,
            outcome=outcome,
            elapsed_ms=elapsed_ms,
            recursion_limit=config.get("recursion_limit"),
            tool_usage=tool_usage,
            failed_tools=failed_tools,
            report_summary=report_summary,
            retrieval_funnel=retrieval_funnel,
        )
        # 收尾日志（含运行元数据与子智能体契约汇总）
        logger.info(
            f"[MainAgent] 运行元数据：session_id={session_id}，结局={outcome}，"
            f"耗时={elapsed_ms}ms，工具用量={tool_usage}，故障工具={list(failed_tools)}，"
            f"检索漏斗={retrieval_funnel or '（本次未触发本地检索）'}"
        )
        if report_summary["count"]:
            logger.info(
                f"[MainAgent] 子智能体返回契约汇总：session_id={session_id}，"
                f"共 {report_summary['count']} 条，截断={report_summary['truncated']}，"
                f"故障={report_summary['failed']}，未解析={report_summary['unparsed']}"
            )
        # 清理内存态（统一放在采集之后）
        clear_tool_failures(session_id)
        clear_budgets(session_id)
        clear_subagent_reports(session_id)
        clear_retrieval_funnel(session_id)
        clear_knowledge_images(session_id)


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        run_deep_agent("从网络查询机器人信息，并生成Markdown文件", "test_session_001")
    )
