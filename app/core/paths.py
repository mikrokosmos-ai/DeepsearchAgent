"""
项目路径工具

集中解析项目根目录，供配置类、提示词加载、日志输出等模块共用，
避免各模块用不同方式推导根目录导致路径不一致。
"""

import os
from pathlib import Path
from dotenv import  load_dotenv


def _find_project_root(identifier: str = ".env") -> Path:
    """
    从当前文件向上逐级查找项目根目录

    优先读取环境变量 PROJECT_ROOT（容器/生产环境可显式指定），
    否则以 identifier 文件（默认 .env）为锚点向上探测。
    :param identifier: 用于标识项目根的锚点文件名
    :return: 项目根目录的绝对路径
    :raises FileNotFoundError: 直到文件系统根仍未找到锚点文件
    """
    # 生产/容器环境通常挂载路径不固定，显式配置优先于目录探测
    env_root = os.getenv("PROJECT_ROOT")
    if env_root and Path(env_root).is_absolute() and Path(env_root).exists():
        return Path(env_root)

    # 以本文件为起点向上查找：core 目录层级可能随重构变化，故不写死 parents[N]
    current_dir = Path(__file__).resolve().parent
    for candidate in [current_dir, *current_dir.parents]:
        if (candidate / identifier).exists():
            # 顺带加载 .env，保证 import 本模块的调用方也能读到环境变量
            load_dotenv(dotenv_path=candidate / identifier)
            return candidate

    raise FileNotFoundError(
        f"未找到项目根目录标识「{identifier}」，且环境变量 PROJECT_ROOT 未配置"
    )


# 模块加载即确定根目录：后续所有路径拼接都以它为基准，避免运行期反复探测
PROJECT_ROOT = _find_project_root()
