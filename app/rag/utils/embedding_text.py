"""
送入向量模型的文本渲染
"""

from typing import Any, Dict, List

# 允许出现在模板里的占位符；渲染时一次性传入，模板只引用自己需要的那些
TEMPLATE_PLACEHOLDERS = ("item_name", "content", "section_path")


def section_path_levels(chunk: Dict[str, Any], levels: int) -> List[str]:
    """
    取章节路径的末 levels 级

    长路径会稀释向量：整条链路（文档 → 章 → 节 → 小节）越往上越泛、信息量越低，
    真正区分语义的往往是最近的上下文，所以默认只取末两级。
    levels <= 0 表示不取（用于「不启用章节增强」的模板）。
    """
    raw = chunk.get("section_path") or []
    if not isinstance(raw, list):
        raw = [raw]
    cleaned = [str(item).strip() for item in raw if str(item).strip()]
    if levels <= 0:
        return []
    return cleaned[-levels:]


def section_path_text(chunk: Dict[str, Any], levels: int) -> str:
    """章节路径的展示形态：以 ` / ` 连接（章节名里几乎不会出现该串，避免歧义）"""
    return " / ".join(section_path_levels(chunk, levels))


def _render_segments(template: str, values: Dict[str, str]) -> str:
    """
    分段模板渲染：`|` 分隔的段里，值全空的那段整段丢掉

    只对含 `|` 的模板生效 —— 单段模板（改造前的写法）走原样 format，
    这样「模板外置」那一步的输出保持逐字不变，等价性对照才有意义。
    """
    rendered: List[str] = []
    for segment in template.split("|"):
        text = segment.format(**values).strip()
        if not text:
            continue
        label, separator, value = text.partition(":")
        if label and separator and not value.strip():
            # 只剩标签（如「章节路径:」）说明该段的取值是空的，丢掉而不是留个空壳
            continue
        rendered.append(text)
    return " | ".join(rendered)


def render_embedding_text(
    chunk: Dict[str, Any],
    template: str,
    section_levels: int = 0,
) -> str:
    """
    用模板把 chunk 渲染成送入模型的文本

    无主体名时退化为纯正文：这与改造前节点里的分支逐字一致（旧写法是
    `f"主体:{item_name},内容:{content}" if item_name else content`）。
    保留这条退化分支是为了让「模板外置」这一步零行为漂移 —— 否则无主体名的
    切片会凭空多出 `主体:,内容:` 这类噪声前缀，向量口径就变了。
    """
    content = chunk.get("content") or ""
    item_name = (chunk.get("item_name") or "").strip()
    if not item_name:
        return content
    values = {
        "item_name": item_name,
        "content": content,
        "section_path": section_path_text(chunk, section_levels),
    }
    if "|" in template:
        return _render_segments(template, values)
    return template.format(**values)
