"""
通用整理子智能体配置模块
"""

from app.agent.middleware.tool_budget_middleware import ToolBudgetMiddleware
from app.prompts.agent_loader import sub_agents_content
from app.tools.upload_file_read_tool import read_file_content

# 只读定位：授予读取工具，不授予任何产出交付文件的工具
# middleware：统一挂护栏（read_file_content 当前不受限 → 无副作用；
#             但以后往 TOOL_CALL_LIMITS 里加工具时会自动生效）
general_purpose_agent = {
    "name": sub_agents_content["general_purpose"]["name"],
    "description": sub_agents_content["general_purpose"]["description"],
    "system_prompt": sub_agents_content["general_purpose"]["system_prompt"],
    "tools": [read_file_content],
    "middleware": [ToolBudgetMiddleware()],
}
