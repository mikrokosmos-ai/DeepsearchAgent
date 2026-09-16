"""
提示词资产统一入口（项目内唯一一处与 prompt 相关的目录）

两个加载器与两类资产一一对应：
    loader.py         ← templates/*.prompt     pipeline 节点提示词模板
    agent_loader.py   ← agents.yml             DeepAgent 主/子智能体配置

职责边界：本目录只管「提示词内容」，app/conf/ 只管「从 .env 读取的运行时
参数」，两边不交叉 —— 因此不要把 agent_loader 放进 app/conf/。
"""
from app.prompts.agent_loader import main_agent_content, sub_agents_content
from app.prompts.loader import PROMPTS_DIR, load_prompt

__all__ = ["load_prompt", "PROMPTS_DIR", "main_agent_content", "sub_agents_content"]
