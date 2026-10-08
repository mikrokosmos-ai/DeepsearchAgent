"""
长期记忆的容量治理：受限合并 + 容量淘汰
"""

from typing import Any, Dict, List, Optional, Sequence

from app.core.logger import logger
from app.core.memory.config import memory_config
from app.core.memory.long_term import render, repository
from app.core.memory.long_term.models import (
    DECISION_CLEAR,
    DECISION_RETRACT,
    DECISION_SUPERSEDE,
    SOURCE_FLUSH,
    Decision,
    MemoryItem,
)

MAX_GROUPS = 20


def plan_eviction(
    items: Sequence[MemoryItem], *, target_chars: int, floor_chars: int, overhead_chars: int
) -> List[int]:
    """
    算出要淘汰的 id（按「FLUSH 殿后、其余最旧先淘」排序，且不越过停手水位）

    纯函数，便于验证脚本直接断言排序与水位两条规则。
    """
    total = overhead_chars + sum(len(item.content or "") for item in items)
    if total <= target_chars:
        return []

    ordered = sorted(items, key=lambda item: (item.source_kind == SOURCE_FLUSH, item.id or 0))
    evicted: List[int] = []
    for item in ordered:
        if total <= target_chars:
            break
        remaining = total - len(item.content or "")
        if floor_chars > 0 and remaining < floor_chars:
            # 再淘就跌破停手水位了 —— 宁可留着超限，也不掏空记忆
            break
        evicted.append(int(item.id))
        total = remaining
    return evicted


def _validate_groups(groups: Any, items: Sequence[MemoryItem]) -> List[Decision]:
    """
    校验合并计划：指向不存在条目、单组不足 2 条、同一 id 出现在多组 —— 整组丢弃并记日志

    宁可少合并，也不要把坏计划写进库（合并是**不可逆**的：旧条目会被软失效）。
    """
    valid_ids = {item.id for item in items}
    seen_ids: set = set()
    decisions: List[Decision] = []
    rejected = 0

    for group in groups or []:
        if not isinstance(group, dict):
            rejected += 1
            continue
        ids = group.get("ids")
        content = str(group.get("content") or "").strip()
        if not isinstance(ids, list) or len(ids) < 2 or not content:
            rejected += 1
            logger.warning(f"[Memory] 受限合并：分组形态不合法，已丢弃（ids={ids}）")
            continue
        try:
            target_ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            rejected += 1
            logger.warning(f"[Memory] 受限合并：分组含非整数 id，已丢弃（ids={ids}）")
            continue
        missing = [x for x in target_ids if x not in valid_ids]
        # 两种重复都要拦：组内同一个 id 出现两次、以及同一 id 出现在多个组里
        repeated_in_group = len(target_ids) != len(set(target_ids))
        duplicated = [x for x in target_ids if x in seen_ids]
        if missing or repeated_in_group or duplicated:
            rejected += 1
            logger.warning(
                f"[Memory] 受限合并：分组含不存在或重复条目，已丢弃"
                f"（缺失={missing} 组内重复={repeated_in_group} 跨组重复={duplicated}）"
            )
            continue
        seen_ids.update(target_ids)
        decisions.append(
            Decision(action=DECISION_SUPERSEDE, content=content, target_ids=target_ids)
        )

    if rejected:
        logger.warning(f"[Memory] 受限合并：共丢弃 {rejected} 个不合法分组")
    return decisions


def parse_merge_plan(raw: str) -> List[Dict[str, Any]]:
    """从模型输出里取 groups（纯函数，验证脚本可直接调用）"""
    import json
    import re

    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        payload = json.loads(text[start : end + 1])
    except Exception:  # noqa: BLE001
        return []
    groups = payload.get("groups") if isinstance(payload, dict) else None
    return groups if isinstance(groups, list) else []


async def _plan_merge(items: Sequence[MemoryItem]) -> List[Dict[str, Any]]:
    """调模型产出合并计划；调用或解析失败一律返回空计划（本治理周期只做淘汰）"""
    from app.prompts.loader import load_prompt

    existing_text, _ = render.existing_facts_text(list(items))
    prompt = load_prompt(
        "memory_consolidation",
        existing_facts=existing_text,
        target_chars=memory_config.consolidation_floor_chars,
    )
    try:
        from app.agent.llm import model

        response = await model.ainvoke(prompt)
        content = getattr(response, "content", response)
        raw = content if isinstance(content, str) else str(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 受限合并调用模型失败，本轮跳过合并：{str(e)[:160]}")
        return []

    groups = parse_merge_plan(raw)
    return groups[:MAX_GROUPS]


async def maybe_consolidate(user_id: str) -> Dict[str, Any]:
    """
    块超上限才治理：受限合并 → 仍超则容量淘汰

    在抽取提交后调用，不阻塞主链路；任何异常都转成结果字典，不上抛。
    """
    if not memory_config.consolidation_enabled:
        return {"status": "disabled", "reason": "容量治理未启用"}
    if not user_id:
        return {"status": "skipped", "reason": "缺少用户标识"}

    try:
        return await _consolidate(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 容量治理异常（不影响抽取结果）：{str(e)[:200]}")
        return {"status": "error", "reason": str(e)[:120]}


async def _consolidate(user_id: str) -> Dict[str, Any]:
    items = repository.list_active_items(user_id)
    if not items:
        return {"status": "noop", "reason": "无可治理的记忆"}

    block = render.render_block(items)
    if not render.is_overflow(block):
        return {"status": "noop", "reason": "未超上限，无需治理"}

    overhead = len(render.BLOCK_HEADER) + 1
    merged_groups = 0
    merged_away = 0

    # ---- 受限合并 ----
    control = repository.get_control_row(user_id)
    if control is not None:
        groups = await _plan_merge(items)
        decisions = _validate_groups(groups, items)
        if decisions:
            status = repository.apply_consolidation(
                user_id,
                expected_revision=control.revision,
                decisions=decisions,
                item_max_chars=memory_config.long_term_item_max_chars,
            )
            if status:
                merged_groups = len(decisions)
                merged_away = sum(len(d.target_ids) for d in decisions)
                logger.info(
                    f"[Memory] 受限合并：user={user_id}，合并 {merged_groups} 组，"
                    f"并掉 {merged_away} 条"
                )
                items = repository.list_active_items(user_id)

    # ---- 容量淘汰 ----
    evicted: List[int] = []
    total = overhead + sum(len(item.content or "") for item in items)
    if total > memory_config.long_term_max_chars:
        evicted = plan_eviction(
            items,
            target_chars=memory_config.long_term_max_chars,
            floor_chars=memory_config.consolidation_floor_chars,
            overhead_chars=overhead,
        )
        if evicted:
            control = repository.get_control_row(user_id)
            if control is not None:
                status = repository.apply_consolidation(
                    user_id,
                    expected_revision=control.revision,
                    decisions=[
                        Decision(action=DECISION_RETRACT, target_ids=evicted),
                    ],
                    item_max_chars=memory_config.long_term_item_max_chars,
                )
                if status:
                    sources = {item.id: item.source_kind for item in items}
                    logger.info(
                        f"[Memory] 容量淘汰：user={user_id}，淘汰 {len(evicted)} 条"
                        f"（id={sorted(evicted)}，下限={memory_config.consolidation_floor_chars}，"
                        f"来源={[sources.get(i) for i in sorted(evicted)]}）"
                    )
                else:
                    evicted = []

    if merged_groups or evicted:
        render.invalidate(user_id)

    remaining = repository.list_active_items(user_id)
    return {
        "status": "consolidated" if (merged_groups or evicted) else "noop",
        "merged_groups": merged_groups,
        "merged_away": merged_away,
        "evicted": len(evicted),
        "remaining_chars": len(render.render_block(remaining)),
    }
