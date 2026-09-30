"""
上传文件读取工具

供主智能体读取用户在当前会话中上传的临时附件。工具会先通过 ContextVar
拿到本次 session_dir，再把模型传入的文件名解析到真实路径，支持文本、
Word、PDF 和 Excel 等常见格式。
"""

import os
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from langchain_core.tools import tool

from app.api.context import get_session_context
from app.api.monitor import monitor
from app.core.logger import logger
from app.utils.path_utils import resolve_path

load_dotenv()

# --- 可配置上限（有安全默认值，不改 .env 也能生效）--------------------------
DEFAULT_MAX_CHARS = 30_000

# 截断时头部保留比例：结论/附录常在文末，只留开头会让模型误判"文件就这些内容"
_HEAD_RATIO = 0.7


def _int_env(name: str, default: int) -> int:
    """读取整型环境变量，非法值回退默认值（避免 int('abc') 抛 ValueError 击穿工具）。"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            f"[read_file] 环境变量 {name}={raw!r} 不是整数，已回退默认值 {default}"
        )
        return default
    return value if value > 0 else default


def max_chars() -> int:
    """单次读取返回给模型的最大字符数。"""
    return _int_env("READ_FILE_MAX_CHARS", DEFAULT_MAX_CHARS)


def _truncate_text(text: str, filename: str) -> str:
    """
    超长文本按「开头 + 结尾」截断，并在最前面显式声明截断事实

    :param text: 原始提取文本
    :param filename: 文件名，用于在提示里指明是哪份材料被截断
    :return: 原文（未超限）或「截断声明 + 头尾摘录」
    """
    limit = max_chars()
    if len(text) <= limit:
        return text

    head_len = int(limit * _HEAD_RATIO)
    tail_len = limit - head_len
    omitted = len(text) - head_len - tail_len
    notice = (
        f"【内容已截断】文件「{filename}」共 {len(text):,} 字符，超过单次读取上限 "
        f"{limit:,} 字符。以下只包含**开头 {head_len:,} 字符**与**结尾 {tail_len:,} 字符**，"
        f"中间约 {omitted:,} 字符未提供。\n"
        f"⇒ 你**并未拿到该文件的完整内容**：不要基于不完整内容下确定性结论；"
        f"若结论依赖被省略的部分，请在最终答复的「缺失项」中说明它会影响什么结论。\n"
    )
    return (
        f"{notice}{'=' * 58}\n"
        f"{text[:head_len]}\n\n"
        f"……（中间约 {omitted:,} 字符已省略）……\n\n"
        f"{text[-tail_len:]}"
    )


# 文档解析依赖按需导入：缺少某类依赖时，只影响对应文件格式，不影响工具整体注册
try:
    import docx
except ImportError:
    docx = None

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import pandas as pd
except ImportError:
    pd = None


@tool
def read_file_content(
    filename: Annotated[
        str, "要读取的文件名或路径（支持 .md, .docx, .pdf, .xlsx, .xls）"
    ],
    instruction: Annotated[
        str, "对提取内容的具体指令（例如：'提取摘要', '统计数据'）"
    ] = "提取全部内容",
) -> str:
    """
    读取当前会话目录中的指定文件内容

    对于 Excel 文件，会自动提供数据统计信息（head 和 describe）。
    所有格式的返回值都受 `READ_FILE_MAX_CHARS` 限制，超限时会在返回内容开头
    声明「已截断」，避免超大附件把主智能体的上下文撑爆。
    :param filename: 文件名或相对路径，通常由主智能体从上传文件列表中选择
    :param instruction: 模型传入的读取意图，用于监控展示，不改变底层解析逻辑
    :return: 文件文本内容、表格摘要，或中文错误提示
    """
    monitor.report_tool(
        "文件内容读取工具", {"filename": filename, "instruction": instruction}
    )

    # 解析路径时优先约束在当前 session_dir 内，避免模型传入绝对路径导致越界读取
    session_dir = get_session_context()
    file_path = Path(resolve_path(filename, session_dir))

    if not file_path.exists():
        return f"错误：文件 '{filename}' 不存在 (解析路径: {file_path})。"

    # 根据文件后缀选择解析方式；未知后缀会先按 UTF-8 文本兜底读取
    ext = file_path.suffix.lower()

    try:
        if ext in [".md", ".txt"]:
            return _truncate_text(file_path.read_text(encoding="utf-8"), filename)

        elif ext == ".docx":
            if docx is None:
                return "错误：未安装 'python-docx' 库，无法读取 Word 文件。"
            # python-docx 读取段落文本，适合课程中的普通 Word 附件
            doc = docx.Document(str(file_path))
            full_text = [para.text for para in doc.paragraphs]
            return _truncate_text("\n".join(full_text), filename)

        elif ext == ".pdf":
            if pypdf is None:
                return "错误：未安装 'pypdf' 库，无法读取 PDF 文件。"
            # pypdf 按页提取文本，扫描件或图片型 PDF 可能无法提取有效文字
            reader = pypdf.PdfReader(str(file_path))
            text = "\n".join([page.extract_text() or "" for page in reader.pages])
            return _truncate_text(text, filename)

        elif ext in [".xlsx", ".xls"]:
            if pd is None:
                return "错误：未安装 'pandas' 库，无法读取 Excel 文件。"

            try:
                # 不显式指定 engine：pandas 会按后缀自动选择（.xlsx→openpyxl、.xls→xlrd），
                # 写死 engine 反而会让另一类后缀失去自动选择能力
                df = pd.read_excel(str(file_path))
            except ImportError as e:
                # .xls 依赖 xlrd，缺失时给出明确的安装指引，而不是把底层 ImportError 透出去
                return (
                    f"读取 Excel 失败: 缺少读取 '{ext}' 所需的依赖库。"
                    f".xlsx 需要 openpyxl，.xls 需要 xlrd（原始错误: {e}）"
                )
            except Exception as e:
                return f"读取 Excel 失败: {str(e)}"

            # Excel 不直接返回全量数据，先给模型列名、预览和统计摘要，避免上下文过长。
            # describe() 在 pandas 3.x 下默认只统计数值列（全对象列时回退为频次统计），
            # 因此无需再手工筛选列，也不会因混杂类型而抛错
            result = [
                f"文件: {filename}",
                f"行数: {len(df)}, 列数: {len(df.columns)}",
                f"列名: {', '.join(df.columns.astype(str))}",
                "\n[前5行数据预览]:",
                df.head().to_string(index=False),
                "\n[统计描述]:",
                df.describe().to_string(),
            ]
            # 列数极多时摘要本身也可能很长，统一过一遍截断闸
            return _truncate_text("\n".join(result), filename)

        else:
            try:
                return _truncate_text(file_path.read_text(encoding="utf-8"), filename)
            except UnicodeDecodeError:
                return f"错误：不支持的文件格式 '{ext}'，且无法作为文本读取。"

    except Exception as e:
        return f"读取文件出错: {str(e)}"


if __name__ == "__main__":
    # 本地自检入口：`python -m app.tools.upload_file_read_tool` 可离线验证截断闸
    # （不需要任何外部服务；用 __main__ 里的假 session_dir 覆盖 ContextVar 读取）
    def get_session_context():
        return "./examples/test_docs"

    def _selfcheck() -> int:
        """截断闸的离线自检：小文件不提示、超限必提示，且头尾都保留。"""
        import tempfile

        failures = []

        def case(desc: str, ok: bool, detail: str = ""):
            print(f"  [{'OK' if ok else 'FAIL'}] {desc}{'' if ok else '  <- ' + detail}")
            if not ok:
                failures.append(desc)

        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.txt"
            small.write_text("短内容", encoding="utf-8")
            big = Path(tmp) / "big.txt"
            # 头尾各埋一个可识别的标记，验证「头 + 尾」都真的保留了
            big.write_text("HEAD_MARK" + "x" * 5000 + "TAIL_MARK", encoding="utf-8")

            def read(p: Path) -> str:
                return read_file_content.invoke({"filename": str(p)})

            os.environ.pop("READ_FILE_MAX_CHARS", None)
            print(f"[默认上限 {max_chars():,} 字符]")
            out_small = read(small)
            case("小文件不出现截断声明", "【内容已截断】" not in out_small, out_small[:80])
            case("小文件内容原样返回", out_small == "短内容", repr(out_small[:80]))

            out_big_default = read(big)
            case(
                f"未超默认上限（{len('HEAD_MARK') + 5000 + len('TAIL_MARK')} 字符）时不截断",
                "【内容已截断】" not in out_big_default,
                out_big_default[:80],
            )

            # 反向对照：把上限压到 100，同样的文件必须触发截断
            os.environ["READ_FILE_MAX_CHARS"] = "100"
            print("[上限压到 100 字符]")
            out_big = read(big)
            case("超过上限时出现截断声明", "【内容已截断】" in out_big, out_big[:120])
            case("声明里写明了省略字符数", "字符已省略" in out_big, out_big[:200])
            case("头尾标记都被保留", "HEAD_MARK" in out_big and "TAIL_MARK" in out_big)
            case(
                "返回值长度受控（≈上限 + 声明与分隔符）",
                len(out_big) < 100 + 400,
                f"实际 {len(out_big)} 字符",
            )

            # 存量回归：把上限设回默认，同一文件不应再被截断（证明闸门随配置生效）
            os.environ.pop("READ_FILE_MAX_CHARS", None)
            case(
                "恢复默认上限后不再截断（配置确实生效）",
                "【内容已截断】" not in read(big),
            )

            # 非法值回退：不应抛异常
            os.environ["READ_FILE_MAX_CHARS"] = "abc"
            case("非法上限回退默认值（不抛异常）", max_chars() == DEFAULT_MAX_CHARS)
            os.environ["READ_FILE_MAX_CHARS"] = "0"
            case("非正数上限回退默认值", max_chars() == DEFAULT_MAX_CHARS)
            os.environ.pop("READ_FILE_MAX_CHARS", None)

        print()
        if failures:
            print(f"自检失败 {len(failures)} 项：{failures}")
            return 1
        print("自检全部通过")
        return 0

    import sys

    sys.exit(_selfcheck())
