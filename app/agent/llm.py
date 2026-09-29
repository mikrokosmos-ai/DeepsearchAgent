"""
大模型初始化模块

负责从 .env 中读取模型配置，并创建项目统一复用的模型对象
后续主智能体和子智能体都从这里导入 model，避免在多个文件里重复加载环境变量
"""

import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

# find_dotenv 会从当前目录向上查找 .env，适合脚本和 Web 服务从不同入口启动的场景
load_dotenv(find_dotenv())

# 模型名 + 凭据 + 兼容端点，三者缺一不可（用途见下方 _REMEDY）
_REQUIRED_ENV_KEYS = ("LLM_QWEN_MAX", "OPENAI_API_KEY", "OPENAI_BASE_URL")

_REMEDY = {
    "LLM_QWEN_MAX": "要调用的模型名（本项目用 OpenAI 兼容协议接入，例如 qwen-max）",
    "OPENAI_API_KEY": "该兼容端点的 API Key（对应 .env 的 OPENAI_API_KEY）",
    "OPENAI_BASE_URL": (
        "OpenAI 兼容端点地址（如 DashScope 兼容模式的 https://dashscope.aliyuncs.com/compatible-mode/v1）；"
        "若确实要直连官方 OpenAI，请显式写成 https://api.openai.com/v1"
    ),
}


def missing_env_keys() -> list[str]:
    """
    返回缺失（或为空白）的必填环境变量名列表，顺序与 _REQUIRED_ENV_KEYS 一致。

    抽成函数是为了可被验证脚本直接调用（不必靠 subprocess 才能检查）。
    """
    return [k for k in _REQUIRED_ENV_KEYS if not (os.getenv(k) or "").strip()]


def build_missing_env_error(keys: list[str]) -> RuntimeError:
    """构造「缺配置」的启动期错误。"""
    lines = [
        "模型配置缺失，服务无法启动：.env 中缺少以下环境变量 ——",
    ]
    lines += [f"  - {k}：{_REMEDY[k]}" for k in keys]
    lines.append(
        "请在仓库根目录的 .env 中补齐后重启（可用 .env.example 作模板）；"
        "注意本模块在导入期执行，因此该错误会直接终止 uvicorn 启动，而不是等到第一次请求。"
    )
    return RuntimeError("\n".join(lines))


_missing_keys = missing_env_keys()
if _missing_keys:
    raise build_missing_env_error(_missing_keys)

# 使用 OpenAI 兼容协议接入模型；具体模型名由 .env 中的 LLM_QWEN_MAX 控制
model = init_chat_model(
    model=os.getenv("LLM_QWEN_MAX"),
    model_provider="openai",
)
