"""
本地知识库子智能体配置模块

将 app/prompts/agents.yml 中的 local_kb 配置与本地 RAG 检索工具组装成
DeepAgents 可识别的字典式子智能体。主智能体后续会根据 description
决定是否把企业内部非结构化文档查询任务分派给它。

说明：本模块替代了改造前的 `knowledge_base_agent`（原实现依赖外部知识库
服务的「先查助手列表 -> 再向指定助手提问」两段式调用）。改造后知识库检索
收敛为本地 query pipeline 的单一入口，因此只需要一个工具。
"""

from app.prompts.agent_loader import sub_agents_content
from app.tools.local_rag_tool import local_rag_search

# 本地知识库子智能体处理企业内部非结构化文档，与网络搜索助手、数据库查询助手形成互补
# 字典式子智能体的核心字段来自 YAML，便于后续只改配置就能调整路由描述和行为约束
# tools 列表声明该子智能体可以调用的真实能力：本地知识库检索
local_knowledge_agent = {
    "name": sub_agents_content["local_kb"]["name"],
    "description": sub_agents_content["local_kb"]["description"],
    "system_prompt": sub_agents_content["local_kb"]["system_prompt"],
    "tools": [local_rag_search],
}
