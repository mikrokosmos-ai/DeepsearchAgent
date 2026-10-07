"""
记忆层

统一承载会话记忆的配置、持久化与后续阶段的中期/长期记忆能力：

    config.py         记忆阈值单点配置与派生（唯一数字来源）
    checkpointer.py   L0 工作记忆的 Redis 持久化（LangGraph checkpointer）
    （后续阶段）summary.py / conversation_repo.py / long_term/ ...

本包只依赖 app.core 与 app.rag.clients，不反向依赖 app.agent / app.api，
避免与主智能体装配形成循环导入。
"""
