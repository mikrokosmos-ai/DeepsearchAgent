"""
运行时目录契约（单一事实来源）

    output/
    ├── sessions/session_{id}/      ← 会话工作区（Agent 运行期读写）
    ├── kb/{task_id}/               ← 知识库导入产物（按任务隔离）
    │   ├── {stem}/{stem}.md
    │   ├── {stem}/images/
    │   └── kg.json
    └── *.md                        ← 交付文档（教学文档、方案文档等，位置不变）

"""

from pathlib import Path

from app.core.paths import PROJECT_ROOT

# --- output 根与三个子域 ---------------------------------------------------

# 前端文件浏览 / 下载的白名单根（server.py 依赖此常量，必须保持与之一致）
OUTPUT_DIR: Path = PROJECT_ROOT / "output"

# 会话工作区根：output/sessions/session_{id}/
SESSIONS_DIR: Path = OUTPUT_DIR / "sessions"

# 知识库导入产物根：output/kb/{task_id}/
KB_OUTPUT_DIR: Path = OUTPUT_DIR / "kb"

# --- updated 状态区（用户上传暂存，不属于 output 白名单）------------------------

UPDATED_DIR: Path = PROJECT_ROOT / "updated"

# 对话链路的上传暂存：updated/session_{thread_id}/
UPDATED_SESSIONS_DIR: Path = UPDATED_DIR / "sessions"

# 知识库导入的**源文件**暂存：updated/kb_import/{task_id}/
KB_IMPORT_DIR: Path = UPDATED_DIR / "kb_import"


def session_dir(session_id: str) -> Path:
    """
    会话工作区目录（不创建）。

    兼容策略（D2：仅新会话生效，旧目录不迁移）：
    若新路径 `output/sessions/session_{id}` 不存在、但旧路径
    `output/session_{id}` 存在，则**继续沿用旧路径**——这样正在跑的旧会话
    与其历史产物不会因为一次目录改造而「凭空消失」。

    :param session_id: 会话 ID（即 thread_id）
    :return: 该会话的工作目录 Path（可能尚未存在）
    """
    new_dir = SESSIONS_DIR / f"session_{session_id}"
    if new_dir.exists():
        return new_dir

    legacy_dir = OUTPUT_DIR / f"session_{session_id}"
    if legacy_dir.exists():
        return legacy_dir

    return new_dir


def kb_output_dir(task_id: str) -> Path:
    """
    知识库导入的任务产物目录：`output/kb/{task_id}`（不创建）。

    该目录即导入链路 `state["local_dir"]` 的取值，
    `node_pdf_to_md` 在其下建 `{stem}/`，`node_import_kg` 在内写 `kg.json`。

    :param task_id: 导入任务 ID
    :return: 该任务的产物目录 Path（可能尚未存在）
    """
    return KB_OUTPUT_DIR / task_id
