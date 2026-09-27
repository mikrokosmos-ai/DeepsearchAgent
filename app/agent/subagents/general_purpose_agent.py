"""
通用整理子智能体配置模块
"""

from app.prompts.agent_loader import sub_agents_content
from app.tools.upload_file_read_tool import read_file_content

# 只读定位：授予读取工具，不授予任何产出交付文件的工具
general_purpose_agent = {
    "name": sub_agents_content["general_purpose"]["name"],
    "description": sub_agents_content["general_purpose"]["description"],
    "system_prompt": sub_agents_content["general_purpose"]["system_prompt"],
    "tools": [read_file_content],
}
