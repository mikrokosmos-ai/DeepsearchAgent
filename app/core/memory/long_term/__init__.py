"""
长期记忆：跨会话用户事实

与 L0/L1/L2 的分工：

    L0/L1  某一会话的图状态与消息原文（热层 + 统一会话集合）
    L2     会话超长时被压掉原文的摘要（替代物）
    L3     从会话里抽出的**稳定事实**，按 user_id 跨会话生效（MySQL 独立库为权威）

L3 的内容量与对话长度无关（抽取只取用户消息），因此三层叠加不会互相膨胀。
"""

from app.core.memory.long_term import models, render, repository

__all__ = ["models", "render", "repository"]
