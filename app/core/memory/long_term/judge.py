"""
长期记忆仲裁（LLM 调用 + 决策解析）

模型只有「读素材、吐决策」的权限：记什么、忘什么由服务端解析校验后落库。
因此解析失败的批次一律不写库 —— 宁可不记，也不写错的。
"""

import json
import re
from typing import List, Sequence

from app.core.logger import logger
from app.core.memory.long_term.models import (
    VALID_DECISIONS,
    Decision,
    JudgeResult,
    MemoryItem,
)
from app.prompts.loader import load_prompt

# 单批决策条数上限：与素材批次上限同量级，防止模型一次吐出几百条
MAX_DECISIONS = 40

# 中和所有围栏形态（含用户自己写出来的），防止伪造段落边界
_FENCE_RE = re.compile(r"</?user_speech[^>]*>", re.IGNORECASE)


def make_nonce() -> str:
    """一次性围栏标记（短且不可预测即可，不需要密码学强度）"""
    import uuid

    return uuid.uuid4().hex[:12]


def neutralize(text: str) -> str:
    """中和正文中的围栏标记"""
    return _FENCE_RE.sub("［围栏标记已移除］", text or "")


def wrap_utterances(utterances: Sequence[str], nonce: str) -> str:
    """把用户发言逐条包进围栏"""
    start, end = f"<user_speech_{nonce}>", f"</user_speech_{nonce}>"
    blocks = []
    for utterance in utterances:
        blocks.append(f"{start}\n{neutralize(utterance)}\n{end}")
    return "\n".join(blocks)


def _extract_json_blob(raw: str) -> str:
    """
    从模型输出里取出 JSON 主体

    模型偶尔会加代码块围栏或前后说明，这里做最小容错：先剥围栏，再取首尾大括号之间。
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start : end + 1]


def parse_decisions(raw: str) -> JudgeResult:
    """把模型输出解析成决策列表（纯函数，验证脚本可直接调用）"""
    blob = _extract_json_blob(raw)
    if not blob:
        return JudgeResult(raw=raw, parsed=False, error="未找到 JSON 主体")

    try:
        payload = json.loads(blob)
    except Exception as e:  # noqa: BLE001
        return JudgeResult(raw=raw, parsed=False, error=f"JSON 解析失败：{str(e)[:120]}")

    if not isinstance(payload, dict):
        return JudgeResult(raw=raw, parsed=False, error="顶层不是对象")

    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list):
        return JudgeResult(raw=raw, parsed=False, error="decisions 不是列表")

    decisions: List[Decision] = []
    for entry in raw_decisions[:MAX_DECISIONS]:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip().upper()
        if action not in VALID_DECISIONS:
            logger.warning(f"[LTM] 仲裁输出含未知 action，已丢弃：{action!r}")
            continue
        targets = entry.get("target_ids")
        target_ids: List[int] = []
        if isinstance(targets, list):
            for item in targets:
                try:
                    target_ids.append(int(item))
                except (TypeError, ValueError):
                    continue
        decisions.append(
            Decision(
                action=action,
                content=str(entry.get("content") or "").strip(),
                target_ids=target_ids,
                reason=str(entry.get("reason") or "").strip(),
            )
        )

    if not decisions:
        return JudgeResult(raw=raw, parsed=False, error="没有可用决策")

    return JudgeResult(decisions=decisions, raw=raw, parsed=True)


async def judge(user_id: str, existing: List[MemoryItem], utterances: Sequence[str]) -> JudgeResult:
    """
    调模型产出决策

    任何异常（超时、限流、输出不可解析）都转成 `parsed=False` 的结果，由调用方按
    「重试到上限 → DROPPED 并推水位」处理，绝不把异常抛给主链路。
    """
    from app.core.memory.long_term import render

    nonce = make_nonce()
    existing_text, _ = render.existing_facts_text(existing)
    prompt = load_prompt(
        "memory_extraction",
        existing_facts=existing_text,
        new_utterances=wrap_utterances(utterances, nonce),
        nonce=nonce,
    )

    try:
        from app.agent.llm import model

        response = await model.ainvoke(prompt)
        content = getattr(response, "content", response)
        raw = content if isinstance(content, str) else str(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 仲裁模型调用失败：user={user_id}，原因：{str(e)[:160]}")
        return JudgeResult(raw="", parsed=False, error=f"模型调用失败：{str(e)[:120]}")

    result = parse_decisions(raw)
    if not result.parsed:
        logger.warning(f"[LTM] 仲裁输出不可解析：user={user_id}，原因={result.error}")
    return result
