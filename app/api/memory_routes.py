"""
长期记忆管理接口（查 / 删单条 / 清空）
"""

from fastapi import APIRouter, HTTPException, Query

from app.core.logger import logger
from app.core.memory.config import memory_config
from app.core.memory.long_term import pipeline, render, repository

router = APIRouter(prefix="/api/memory", tags=["memory"])


@router.get("")
def list_memory(user_id: str = Query(..., min_length=1)) -> dict:
    """查该用户生效中的长期事实（附带当前注入块的字符数，便于观察容量）"""
    facts = pipeline.current_facts(user_id)
    block = render.get_block(user_id)
    return {
        "user_id": user_id,
        "count": len(facts),
        "items": facts,
        "block_chars": render.block_chars(block),
        "block_limit": memory_config.long_term_max_chars,
    }


@router.delete("/{item_id}")
def delete_memory_item(item_id: int, user_id: str = Query(..., min_length=1)) -> dict:
    """删单条（「忘掉某句话」）；删完失效注入块缓存，下一次注入即不再含该条"""
    if not repository.delete_item(user_id, item_id):
        raise HTTPException(status_code=404, detail="未找到该条记忆")
    render.invalidate(user_id)
    logger.info(f"[LTM] 管理接口删除单条记忆：user={user_id} id={item_id}")
    return {"ok": True, "deleted": item_id}


@router.delete("")
def clear_memory(user_id: str = Query(..., min_length=1)) -> dict:
    """清空该用户全部事实（用户明确要求「忘掉全部」时用）"""
    removed = repository.clear_items(user_id)
    render.invalidate(user_id)
    logger.warning(f"[LTM] 管理接口清空长期记忆：user={user_id}，删除 {removed} 条")
    return {"ok": True, "removed": removed}
