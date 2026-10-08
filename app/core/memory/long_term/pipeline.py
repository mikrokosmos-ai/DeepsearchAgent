"""
长期记忆抽取管道

一次抽取的完整生命周期：

    选素材 → 抢处理权 → 事务外仲裁 → 短事务双校验提交 → 结算台账 → 失效注入块缓存
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.core.memory.config import memory_config
from app.core.memory.long_term import consolidation, judge, render, repository
from app.core.memory.long_term.models import (
    SETTLED_STATUSES,
    SOURCE_BATCH,
    SOURCE_FLUSH,
    STATUS_CONFLICT,
    STATUS_DROPPED,
    STATUS_WRITTEN,
    ControlRow,
    SourceRange,
)

# 素材投影：只要判断与抽取必需的四列，别把整条文档拖出来
_MATERIAL_PROJECTION = {"_id": 1, "text": 1, "ts": 1, "session_id": 1}


def _fetch_materials(
    user_id: str, control: ControlRow, watermark: Optional[str]
) -> Optional[List[Dict[str, Any]]]:
    """
    取本批素材（按 _id 升序）

    :return: 素材列表；查询失败返回 None（与「空列表」区分开，后者是正常的无素材）
    """
    from bson import ObjectId

    from app.core.memory.conversation_repo import LAYER_AGENT
    from app.rag.clients.mongo_client import get_history_mongo_tool

    query: Dict[str, Any] = {
        "user_id": user_id,
        # 只取用户原话：rag 层的「用户消息」是子智能体派发的子问题，不是用户说的话
        "layer": LAYER_AGENT,
        "role": "user",
        # 下界口径：控制行创建时刻**向下取整到秒**。不直接用毫秒是因为 MySQL DATETIME(3)
        # 会对毫秒四舍五入，而同毫秒内落库的第一条消息会被判定为「早于下界」而永久漏抽；
        # 放宽到整秒最多多抽 1 秒内的旧消息，代价可忽略。
        "ts": {"$gte": float(int(control.create_time.timestamp()))},
    }
    if watermark:
        try:
            query["_id"] = {"$gt": ObjectId(watermark)}
        except Exception as e:  # noqa: BLE001  水位损坏时必须报错退出，不能当成「无水印」否则会重复抽取
            logger.warning(f"[LTM] 水位无法解析为 ObjectId，本轮放弃：{watermark!r}，{str(e)[:80]}")
            return None

    try:
        cursor = (
            get_history_mongo_tool()
            .message.find(query, _MATERIAL_PROJECTION)
            .sort([("_id", 1)])
            .limit(memory_config.extract_batch_max)
        )
        return list(cursor)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 素材读取失败：user={user_id}，原因：{str(e)[:160]}")
        return None


async def run_extraction(user_id: str, *, force: bool = False) -> Dict[str, Any]:
    """
    跑一批抽取

    :param force: 跳过「待处理消息 ≥ 门槛」的限制（整理工具走这条；后台批不跳）
    :return: 可读结果字典，`reason` 直接用于工具文案
    """
    if not memory_config.long_term_enabled:
        return {"ok": False, "status": "disabled", "reason": "长期记忆未启用"}
    if not memory_config.extract_enabled:
        return {"ok": False, "status": "disabled", "reason": "记忆抽取已关闭"}
    if not user_id:
        # user_id 缺失时长期记忆退化为「不可用」，而不是抛错 —— 接口层把它设计成可选项
        return {"ok": False, "status": "no_user", "reason": "缺少用户标识，无法整理长期记忆"}
    if not repository.ping():
        return {"ok": False, "status": "unavailable", "reason": "长期记忆存储不可用"}

    # 先回收僵尸：进程崩溃留下的 PROCESSING 行会让该用户的水位永久卡死
    recycled = repository.recycle_stale(memory_config.extract_zombie_timeout_s)
    if recycled:
        logger.warning(f"[LTM] 回收僵尸抽取批次 {recycled} 条")

    control = repository.ensure_control_row(user_id)
    if control is None:
        return {"ok": False, "status": "unavailable", "reason": "长期记忆控制面不可用"}

    watermark = repository.current_watermark(user_id)
    materials = _fetch_materials(user_id, control, watermark)
    if materials is None:
        return {"ok": False, "status": "material_error", "reason": "素材读取失败"}
    if not materials:
        return {"ok": True, "status": "noop", "count": 0, "reason": "没有待整理的新消息"}
    if not force and len(materials) < memory_config.background_extract_min_messages:
        return {
            "ok": True,
            "status": "skipped",
            "count": len(materials),
            "reason": f"待整理消息仅 {len(materials)} 条，未达门槛",
        }

    source = SourceRange(
        from_message_id=str(materials[0]["_id"]),
        to_message_id=str(materials[-1]["_id"]),
        from_time=_as_dt(materials[0].get("ts")),
        to_time=_as_dt(materials[-1].get("ts")),
        session_id=materials[-1].get("session_id"),
    )

    batch_id = repository.claim_batch(user_id, source)
    if batch_id is None:
        # 抢不到处理权 = 另一个批次在处理，这是正常的并发结果，不是错误
        return {"ok": True, "status": "busy", "reason": "上一批记忆整理仍在进行中"}

    existing = repository.list_active_items(user_id)
    result = await judge.judge(user_id, existing, [str(m.get("text") or "") for m in materials])

    if not result.parsed:
        repository.settle_batch(batch_id, STATUS_DROPPED, result.error)
        return {
            "ok": False,
            "status": "dropped",
            "count": len(materials),
            "reason": f"记忆整理未产出可用结果（{result.error}）",
        }

    status = repository.commit_decisions(
        user_id,
        batch_id,
        expected_revision=control.revision,
        expected_watermark=watermark,
        source=source,
        decisions=result.decisions,
        item_max_chars=memory_config.long_term_item_max_chars,
        # 来源决定容量淘汰时的次序：用户主动要求记住的，最后才淘汰
        source_kind=SOURCE_FLUSH if force else SOURCE_BATCH,
    )

    if status is None:
        repository.settle_batch(batch_id, STATUS_DROPPED, "提交失败")
        return {"ok": False, "status": "dropped", "reason": "记忆整理提交失败"}
    if status == STATUS_CONFLICT:
        # 冲突批不推水位（下一轮会重新取同一区间重试），但必须释放处理权
        repository.settle_batch(batch_id, STATUS_CONFLICT, "双校验未通过")
        return {"ok": True, "status": "conflict", "reason": "记忆集已被其他批次更新，本批作废"}

    written = status == STATUS_WRITTEN
    if written:
        # 只有真改了记忆集才失效注入块缓存；NOOP 批不改内容，白失效会多一次回源
        render.invalidate(user_id)

    total_chars = len(render.render_block(repository.list_active_items(user_id)))
    if total_chars > memory_config.long_term_max_chars:
        logger.warning(
            f"[LTM] 长期记忆总量越界告警：user={user_id}，{total_chars} > "
            f"{memory_config.long_term_max_chars} 字符（一期只告警，治理见阶段 5）"
        )

    # 抽取是唯一会写事实库的路径，容量治理挂在它的收尾：
    # 治理失败不影响抽取结论（maybe_consolidate 内部已把异常转成结果字典）
    if written:
        await consolidation.maybe_consolidate(user_id)

    return {
        "ok": True,
        "status": "written" if written else "noop",
        "count": len(materials),
        "reason": f"已整理 {len(materials)} 条消息" + ("并更新了记忆" if written else "，记忆无变化"),
    }


def _as_dt(value: Any) -> Optional[datetime]:
    """把 `messages.ts`（float 秒）还原成 naive datetime（与写入时的本地时区一致）"""
    try:
        return datetime.fromtimestamp(float(value))
    except (TypeError, ValueError):
        return None


def current_facts(user_id: str) -> List[Dict[str, Any]]:
    """生效事实的只读视图（管理接口用）"""
    return [
        {
            "id": item.id,
            "content": item.content,
            "create_time": item.create_time.isoformat() if item.create_time else None,
            "source_session_id": item.source_session_id,
        }
        for item in repository.list_active_items(user_id)
    ]
