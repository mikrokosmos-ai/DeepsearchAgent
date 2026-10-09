"""
块感知分块：先判定块类型，再分发到对应处理方式。

Markdown 已由 MinerU 产出、结构可直接用，所以只在文本上做块识别 ——
不引入新解析器、不做全量 AST 化：为通用性付出的复杂度，在「手册 + 简历」这类语料上
换不回收益，却会让后续每一次改动都要先理解一层解析器。

"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

BLOCK_CODE = "code"
BLOCK_TABLE = "table"
BLOCK_IMAGE = "image"
BLOCK_LIST = "list"
BLOCK_TEXT = "text"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_INDENT_RE = re.compile(r"^\s+\S")


@dataclass
class Block:
    """一个块：同类行 + 它所属的章节位置"""

    lines: List[str]
    block_type: str
    # 当前块所属标题（最近一级），无标题时是文档名
    title: str
    # 完整标题链（章节路径），不含文档名
    section_path: List[str] = field(default_factory=list)


def heading_of(line: str) -> Optional[str]:
    """行是不是标题；是则返回标题正文（去掉 `#` 与两侧空白）"""
    match = _HEADING_RE.match(line.strip())
    return match.group(2) if match else None


def heading_level(line: str) -> int:
    """标题层级（1..6）；非标题返回 0"""
    match = _HEADING_RE.match(line.strip())
    return len(match.group(1)) if match else 0


def is_fence(line: str) -> bool:
    return bool(_FENCE_RE.match(line))


def is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line))


def is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEP_RE.match(line))


def is_list_item(line: str) -> bool:
    return bool(_LIST_ITEM_RE.match(line))


def classify_line(line: str) -> str:
    """块类型判定（围栏代码由调用方在扫描时单独处理，因为它有跨行的状态）"""
    if is_table_row(line):
        return BLOCK_TABLE
    if _IMAGE_LINE_RE.match(line):
        return BLOCK_IMAGE
    if is_list_item(line):
        return BLOCK_LIST
    return BLOCK_TEXT


def slice_blocks(md_content: str, file_title: str) -> List[Block]:
    """
    按「标题栈 + 块类型」把 Markdown 切成块序列

    标题栈全量维护（不依赖「父标题有没有正文」）：父标题只要自己有正文，
    旧实现就不会把它带进子块的章节链里 —— 章节层级因此是残缺的。
    栈式维护让每个块都拿到完整路径，这既是章节元数据的来源，也是长块补前缀的依据。
    """
    blocks: List[Block] = []
    stack: List[tuple] = []  # [(level, title)]
    buffer: List[str] = []
    buffer_type: Optional[str] = None
    in_fence = False

    def current_title() -> str:
        return stack[-1][1] if stack else file_title

    def current_path() -> List[str]:
        return [title for _, title in stack]

    def flush() -> None:
        nonlocal buffer, buffer_type
        if not buffer:
            buffer_type = None
            return
        # 块内空行只起分隔作用，留在两端会污染表格/代码的判读
        while buffer and not buffer[0].strip():
            buffer.pop(0)
        while buffer and not buffer[-1].strip():
            buffer.pop()
        if buffer:
            blocks.append(
                Block(list(buffer), buffer_type or BLOCK_TEXT, current_title(), current_path())
            )
        buffer = []
        buffer_type = None

    for line in md_content.split("\n"):
        if in_fence:
            buffer.append(line)
            if is_fence(line):
                in_fence = False
                flush()
            continue

        if is_fence(line):
            flush()
            in_fence = True
            buffer_type = BLOCK_CODE
            buffer.append(line)
            continue

        title = heading_of(line)
        if title is not None:
            flush()
            level = heading_level(line)
            # 同级或更深的标题出栈：`### a` 之后的 `## b` 必须把 a 弹掉
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue

        kind = classify_line(line)
        # 空行归到当前块（不改判类型），这样纯文本段落仍像改造前一样整段累积
        if buffer and line.strip() and kind != buffer_type:
            flush()
        if not buffer:
            buffer_type = kind
        buffer.append(line)

    flush()
    return blocks


def section_prefix(section_path: List[str], levels: int) -> str:
    """
    章节路径前缀：取末 levels 级、按行连接

    长路径会稀释向量（越往上越泛、信息量越低），故默认只取末两级；
    levels<=0 或路径为空时返回空串（不启用章节增强的配置）。
    """
    if levels <= 0 or not section_path:
        return ""
    kept = [str(item).strip() for item in section_path if str(item).strip()]
    return "\n".join(kept[-levels:])


def table_batches(lines: List[str], max_rows: int, max_chars: int) -> List[List[str]]:
    """
    把表格切成多批，**每批都带表头**（含分隔行）

    表头必须跟着每一批走：数据行脱离表头就不知道列名是什么，检索到也无法回答
    「这一列的数值是什么意思」。数据行不丢不重，由调用方断言守恒。
    """
    rows = [line for line in lines if line.strip()]
    if not rows:
        return []

    if len(rows) > 1 and is_table_separator(rows[1]):
        prefix, body = rows[:2], rows[2:]
    else:
        prefix, body = rows[:1], rows[1:]

    batches: List[List[str]] = []
    current: List[str] = []
    for row in body:
        candidate = prefix + current + [row]
        if current and (len(current) >= max_rows or len("\n".join(candidate)) > max_chars):
            batches.append(prefix + current)
            current = []
        current.append(row)
    if current or not batches:
        batches.append(prefix + current)
    return batches


def list_batches(lines: List[str], max_chars: int) -> List[List[str]]:
    """
    列表按条目分批（条目 + 其缩进续行算一组）

    列表尽量整段保留：只有真的超长才分，且按条目边界分，不在条目中间切断。
    """
    items: List[List[str]] = []
    for line in lines:
        if not line.strip():
            continue
        if is_list_item(line) or not items:
            items.append([line])
        else:
            if _INDENT_RE.match(line):
                items[-1].append(line)
            else:
                items.append([line])

    batches: List[List[str]] = []
    current: List[str] = []
    for item in items:
        candidate = current + item
        if current and len("\n".join(candidate)) > max_chars:
            batches.append(current)
            current = []
        current.extend(item)
    if current or not batches:
        batches.append(current)
    return batches


def chunk_row(
    content: str,
    block: Block,
    file_title: str,
    part: int,
) -> Dict[str, object]:
    """
    组装一条 chunk

    字段语义（与改造前的差异写在 `output/DeepsearchAgent_批次5验证记录.md`）：
        title        当前块所属标题；序号只进 part，不再写「原标题_序号」这种伪标题
        parent_title 同 title（保留字段以向后兼容读取方）
        part         0=未切；>0=切分后的第几片
        section_path 完整标题链（列表形式，入向量库前再归一成字符串）
        block_type   块类型，供检索侧与验证脚本辨别
    """
    return {
        "content": content,
        "title": block.title,
        "file_title": file_title,
        "parent_title": block.title,
        "part": part,
        "section_path": list(block.section_path),
        "block_type": block.block_type,
    }
