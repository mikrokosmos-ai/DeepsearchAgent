"""
长期记忆仓储（MySQL 独立库）
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import mysql.connector
from mysql.connector import Error as MySQLError

from app.core.logger import logger
from app.core.memory.long_term.models import (
    DECISION_ADD,
    DECISION_CLEAR,
    DECISION_NOOP,
    DECISION_RETRACT,
    DECISION_SUPERSEDE,
    SETTLED_STATUSES,
    STATUS_CONFLICT,
    STATUS_DROPPED,
    STATUS_NOOP,
    STATUS_PROCESSING,
    STATUS_WRITTEN,
    ControlRow,
    Decision,
    MemoryItem,
    SourceRange,
)
from app.rag.conf.memory_db_config import memory_db_config

TABLE_ITEMS = "t_agent_memory"
TABLE_BATCHES = "t_agent_memory_extraction"
TABLE_CONTROL = "t_agent_memory_control"


def _now() -> datetime:
    return datetime.now()


def _available() -> bool:
    """配置是否齐全（缺配置时静默跳过，不打日志刷屏）"""
    return memory_db_config.configured


@contextmanager
def _cursor(dictionary: bool = False):
    """开一条独立连接并交出游标；调用方负责 commit / rollback"""
    conn = mysql.connector.connect(**memory_db_config.connect_kwargs())
    try:
        cur = conn.cursor(dictionary=dictionary)
        try:
            yield conn, cur
        finally:
            cur.close()
    finally:
        conn.close()


def ping() -> bool:
    """探针：长期记忆库是否可用（启动期与验证脚本用）"""
    if not _available():
        return False
    try:
        with _cursor() as (_conn, cur):
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 长期记忆库不可用：{str(e)[:160]}")
        return False


# =============================================================================
# 控制面
# =============================================================================
def ensure_control_row(user_id: str) -> Optional[ControlRow]:
    """
    取控制行，不存在则创建

    创建时刻即抽取下界：冷启动不回灌上线前的历史消息（否则上线瞬间会产出一批巨量素材）。
    """
    row = get_control_row(user_id)
    if row is not None:
        return row
    if not _available():
        return None

    now = _now()
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                f"INSERT IGNORE INTO {TABLE_CONTROL} (user_id, revision, create_time, update_time) "
                "VALUES (%s, 0, %s, %s)",
                (user_id, now, now),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 控制行创建失败：user={user_id}，原因：{str(e)[:160]}")
        return None
    return get_control_row(user_id)


def get_control_row(user_id: str) -> Optional[ControlRow]:
    if not _available():
        return None
    try:
        with _cursor(dictionary=True) as (_conn, cur):
            cur.execute(
                f"SELECT user_id, revision, create_time FROM {TABLE_CONTROL} WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 控制行读取失败：user={user_id}，原因：{str(e)[:160]}")
        return None
    if not row:
        return None
    return ControlRow(
        user_id=row["user_id"], revision=int(row["revision"]), create_time=row["create_time"]
    )


# =============================================================================
# 台账（水位 + 审计）
# =============================================================================
def current_watermark(user_id: str) -> Optional[str]:
    """当前水位 = 该用户最后一条「已结算」批次的 to_message_id；从未抽取过则 None"""
    if not _available():
        return None
    placeholders = ", ".join(["%s"] * len(SETTLED_STATUSES))
    try:
        with _cursor(dictionary=True) as (_conn, cur):
            cur.execute(
                f"SELECT to_message_id FROM {TABLE_BATCHES} "
                f"WHERE user_id = %s AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (user_id, *SETTLED_STATUSES),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 水位读取失败：user={user_id}，原因：{str(e)[:160]}")
        return None
    return row["to_message_id"] if row else None


def claim_batch(user_id: str, source: SourceRange) -> Optional[int]:
    """
    抢处理权：插一行 PROCESSING

    :return: 批次 id；抢不到（该用户已有在处理的批次）或库不可用返回 None
    """
    if not _available():
        return None
    now = _now()
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                f"INSERT INTO {TABLE_BATCHES} "
                "(user_id, from_message_id, to_message_id, from_time, to_time, status, "
                " attempt_count, create_time, update_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)",
                (
                    user_id,
                    source.from_message_id,
                    source.to_message_id,
                    source.from_time,
                    source.to_time,
                    STATUS_PROCESSING,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
    except mysql.connector.IntegrityError:
        # 唯一索引拦住：另一个进程/协程正在处理同一用户 → 正常的 BUSY，不是错误
        logger.info(f"[LTM] 该用户已有在处理的抽取批次，本次跳过：user={user_id}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 抢处理权失败：user={user_id}，原因：{str(e)[:160]}")
        return None


def settle_batch(batch_id: int, status: str, decision_text: str = "") -> bool:
    """结算台账行（非提交路径使用：DROPPED / CONFLICT 等）"""
    if not _available():
        return False
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                f"UPDATE {TABLE_BATCHES} SET status = %s, decision = %s, update_time = %s "
                "WHERE id = %s",
                (status, (decision_text or None), _now(), batch_id),
            )
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 台账结算失败：batch={batch_id}，原因：{str(e)[:160]}")
        return False


def recycle_stale(timeout_s: int) -> int:
    """
    僵尸回收：把超时仍停留在 PROCESSING 的批次判为 DROPPED

    超时判定的依据是 `update_time`。回收后该批不写入任何事实，但会推水位 ——
    否则进程崩溃留下的 PROCESSING 行会让这个用户的水位永久卡死。
    """
    if not _available():
        return 0
    deadline = _now() - timedelta(seconds=timeout_s)
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                f"UPDATE {TABLE_BATCHES} SET status = %s, update_time = %s "
                "WHERE status = %s AND update_time < %s",
                (STATUS_DROPPED, _now(), STATUS_PROCESSING, deadline),
            )
            conn.commit()
            return int(cur.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 僵尸批次回收失败：{str(e)[:160]}")
        return 0


# =============================================================================
# 事实
# =============================================================================
def _acceptable(content: str, item_max_chars: int) -> bool:
    """单条事实的准入校验：非空且不超业务上限。超长直接拒绝，不截断 —— 截断会造出半句话的事实"""
    if not content:
        return False
    if item_max_chars > 0 and len(content) > item_max_chars:
        logger.warning(
            f"[LTM] 事实超长被拒：{len(content)} > {item_max_chars} 字符：{content[:60]}"
        )
        return False
    return True


def _insert_item(cur, user_id: str, content: str, source: SourceRange, now: datetime) -> int:
    cur.execute(
        f"INSERT INTO {TABLE_ITEMS} "
        "(user_id, content, source_session_id, source_from, source_to, invalid_at, create_time) "
        "VALUES (%s, %s, %s, %s, %s, NULL, %s)",
        (user_id, content, source.session_id, source.from_message_id, source.to_message_id, now),
    )
    return int(cur.lastrowid)


def _row_to_item(row: Dict[str, Any]) -> MemoryItem:
    return MemoryItem(
        id=int(row["id"]),
        user_id=row["user_id"],
        content=row["content"] or "",
        source_session_id=row.get("source_session_id"),
        source_from=row.get("source_from"),
        source_to=row.get("source_to"),
        superseded_by=int(row["superseded_by"]) if row.get("superseded_by") else None,
        invalid_at=row.get("invalid_at"),
        create_time=row.get("create_time"),
    )


def list_active_items(user_id: str) -> List[MemoryItem]:
    """生效中的事实（注入渲染与仲裁输入都用它），按 id 升序"""
    return list_items(user_id, include_invalid=False)


def list_items(user_id: str, *, include_invalid: bool = False) -> List[MemoryItem]:
    if not _available():
        return []
    where = "user_id = %s" if include_invalid else "user_id = %s AND invalid_at IS NULL"
    try:
        with _cursor(dictionary=True) as (_conn, cur):
            cur.execute(
                f"SELECT id, user_id, content, source_session_id, source_from, source_to, "
                f"superseded_by, invalid_at, create_time FROM {TABLE_ITEMS} "
                f"WHERE {where} ORDER BY id ASC",
                (user_id,),
            )
            rows = cur.fetchall() or []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 事实读取失败：user={user_id}，原因：{str(e)[:160]}")
        return []
    return [_row_to_item(row) for row in rows]


def delete_item(user_id: str, item_id: int) -> bool:
    """物理删除单条（管理接口专用；业务路径一律走软失效）"""
    if not _available():
        return False
    try:
        with _cursor() as (conn, cur):
            cur.execute(
                f"DELETE FROM {TABLE_ITEMS} WHERE id = %s AND user_id = %s", (item_id, user_id)
            )
            conn.commit()
            return int(cur.rowcount or 0) > 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 删除事实失败：user={user_id} id={item_id}，原因：{str(e)[:160]}")
        return False


def clear_items(user_id: str) -> int:
    """清空该用户全部事实（管理接口专用），返回删除行数"""
    if not _available():
        return 0
    try:
        with _cursor() as (conn, cur):
            cur.execute(f"DELETE FROM {TABLE_ITEMS} WHERE user_id = %s", (user_id,))
            conn.commit()
            return int(cur.rowcount or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 清空事实失败：user={user_id}，原因：{str(e)[:160]}")
        return 0


# =============================================================================
# 提交（短事务 + 双校验）
# =============================================================================
def commit_decisions(
    user_id: str,
    batch_id: int,
    *,
    expected_revision: int,
    expected_watermark: Optional[str],
    source: SourceRange,
    decisions: Iterable[Decision],
    item_max_chars: int,
) -> Optional[str]:
    """
    在短事务里应用仲裁决策

    双校验缺一不可：`NOOP` 批不推版本号，所以只靠 `revision` 拦不住重复写入，
    必须再加水位这一道。

    :return: 最终写入台账的状态（WRITTEN / NOOP / CONFLICT）；库不可用或异常返回 None
    """
    if not _available():
        return None

    placeholders = ", ".join(["%s"] * len(SETTLED_STATUSES))
    applied = 0
    rejected = 0
    now = _now()
    final_status = STATUS_NOOP

    try:
        with _cursor(dictionary=True) as (conn, cur):
            conn.start_transaction()

            cur.execute(
                f"SELECT revision FROM {TABLE_CONTROL} WHERE user_id = %s FOR UPDATE", (user_id,)
            )
            control = cur.fetchone()
            if control is None or int(control["revision"]) != int(expected_revision):
                conn.rollback()
                logger.info(
                    f"[LTM] 提交冲突（版本号不一致）：user={user_id}，"
                    f"期望={expected_revision}，实际={control['revision'] if control else None}"
                )
                return STATUS_CONFLICT

            cur.execute(
                f"SELECT to_message_id FROM {TABLE_BATCHES} "
                f"WHERE user_id = %s AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (user_id, *SETTLED_STATUSES),
            )
            tail = cur.fetchone()
            actual_watermark = tail["to_message_id"] if tail else None
            if actual_watermark != expected_watermark:
                conn.rollback()
                logger.info(
                    f"[LTM] 提交冲突（水位不一致）：user={user_id}，"
                    f"期望={expected_watermark}，实际={actual_watermark}"
                )
                return STATUS_CONFLICT

            for decision in decisions:
                action = (decision.action or "").upper()

                if action == DECISION_NOOP:
                    continue

                if action == DECISION_CLEAR:
                    cur.execute(
                        f"UPDATE {TABLE_ITEMS} SET invalid_at = %s WHERE user_id = %s "
                        "AND invalid_at IS NULL",
                        (now, user_id),
                    )
                    applied += 1
                    continue

                if action in (DECISION_SUPERSEDE, DECISION_RETRACT):
                    target_ids = [int(x) for x in (decision.target_ids or []) if str(x).isdigit()]
                    if not target_ids:
                        rejected += 1
                        logger.warning(f"[LTM] 决策缺少目标条目，已丢弃：action={action}")
                        continue

                    if action == DECISION_RETRACT:
                        cur.execute(
                            f"UPDATE {TABLE_ITEMS} SET invalid_at = %s, superseded_by = NULL "
                            f"WHERE user_id = %s AND id = %s AND invalid_at IS NULL",
                            (now, user_id, target_ids[0]),
                        )
                        applied += 1 if cur.rowcount else 0
                        rejected += 0 if cur.rowcount else 1
                        continue

                    # SUPERSEDE：先插新行，再把旧行指向它
                    content = (decision.content or "").strip()
                    if not _acceptable(content, item_max_chars):
                        rejected += 1
                        continue
                    new_id = _insert_item(cur, user_id, content, source, now)
                    cur.execute(
                        f"UPDATE {TABLE_ITEMS} SET invalid_at = %s, superseded_by = %s "
                        f"WHERE user_id = %s AND id = %s AND invalid_at IS NULL",
                        (now, new_id, user_id, target_ids[0]),
                    )
                    applied += 1
                    continue

                if action == DECISION_ADD:
                    content = (decision.content or "").strip()
                    if not _acceptable(content, item_max_chars):
                        rejected += 1
                        continue
                    _insert_item(cur, user_id, content, source, now)
                    applied += 1
                    continue

                rejected += 1
                logger.warning(f"[LTM] 未知决策类型，已丢弃：action={decision.action!r}")

            if rejected:
                logger.warning(
                    f"[LTM] 本批有 {rejected} 条决策被丢弃（目标不存在 / 内容超长 / 类型未知）"
                )

            cur.execute(
                f"UPDATE {TABLE_BATCHES} SET status = %s, decision = %s, update_time = %s "
                "WHERE id = %s",
                (STATUS_WRITTEN if applied else STATUS_NOOP, None, now, batch_id),
            )
            final_status = STATUS_WRITTEN if applied else STATUS_NOOP

            if applied:
                # 只有真的改了记忆集才推版本号：NOOP 批不推，这正是「双校验」里水位那道存在的理由
                cur.execute(
                    f"UPDATE {TABLE_CONTROL} SET revision = revision + 1, update_time = %s "
                    "WHERE user_id = %s",
                    (now, user_id),
                )

            conn.commit()
    except MySQLError as e:
        logger.warning(f"[LTM] 提交失败（数据库错误）：user={user_id}，原因：{str(e)[:200]}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[LTM] 提交失败：user={user_id}，原因：{str(e)[:200]}")
        return None

    return final_status
