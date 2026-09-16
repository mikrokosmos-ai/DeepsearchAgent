"""
百炼 MCP 联网检索配置（D6）

承载 MCP 服务地址与鉴权。
api_key 复用 OPENAI_API_KEY：百炼平台的兼容模式与 MCP 服务共用同一把密钥，
单独声明一个变量反而会让两处凭据有走失配的风险。
"""

import os
from dataclasses import dataclass

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载


@dataclass
class McpConfig:
    mcp_base_url: str  # MCP 服务地址
    api_key: str  # 鉴权密钥（复用 OPENAI_API_KEY）


mcp_config = McpConfig(
    mcp_base_url=os.getenv("MCP_DASHSCOPE_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
)
