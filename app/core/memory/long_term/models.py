"""
长期记忆的数据模型

只描述形状与状态取值，不含任何访问逻辑（访问在 repository.py）。
状态与决策类型集中在这里定义，是为了让管道、仓储、验证脚本共用同一套字面量 ——
散落的字符串字面量是这类「多状态机」最容易出现口径分裂的地方。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# ---- 台账状态：一次抽取批次的结局 ----
STATUS_PROCESSING = "PROCESSING"  # 已抢到处理权，正在仲裁
STATUS_WRITTEN = "WRITTEN"  # 应用了至少一条决策
STATUS_NOOP = "NOOP"  # 无变更（但仍要推水位）
STATUS_DROPPED = "DROPPED"  # 重试到上限，放弃并推水位（坏批次不许堵住水位）
STATUS_CONFLICT = "CONFLICT"  # 双校验未通过，本批作废

# 已结算（可据此推算水位）的状态集合
SETTLED_STATUSES = (STATUS_WRITTEN, STATUS_NOOP, STATUS_DROPPED)

# ---- 事实来源：容量淘汰排序要用（FLUSH 殿后） ----
SOURCE_FLUSH = "FLUSH"  # 用户通过整理工具主动要求记住的
SOURCE_BATCH = "BATCH"  # 后台批抽出来的

# ---- 仲裁决策类型 ----
DECISION_ADD = "ADD"  # 新增事实
DECISION_SUPERSEDE = "SUPERSEDE"  # 新事实取代旧事实（旧行 superseded_by 指向新行）
DECISION_RETRACT = "RETRACT"  # 撤回（旧行 invalid_at 非空且 superseded_by 留空）
DECISION_CLEAR = "CLEAR"  # 清空该用户全部事实
DECISION_NOOP = "NOOP"  # 本批不影响记忆

VALID_DECISIONS = (
    DECISION_ADD,
    DECISION_SUPERSEDE,
    DECISION_RETRACT,
    DECISION_CLEAR,
    DECISION_NOOP,
)


@dataclass
class MemoryItem:
    """一条用户事实；失效用软删除（invalid_at / superseded_by），不物理删"""

    id: Optional[int]
    user_id: str
    content: str
    source_kind: str = SOURCE_BATCH
    source_session_id: Optional[str] = None
    source_from: Optional[str] = None
    source_to: Optional[str] = None
    superseded_by: Optional[int] = None
    invalid_at: Optional[datetime] = None  # None = 生效
    create_time: Optional[datetime] = None


@dataclass
class SourceRange:
    """
    一批素材的消息区间

    """

    from_message_id: str
    to_message_id: str
    from_time: Optional[datetime] = None
    to_time: Optional[datetime] = None
    session_id: Optional[str] = None  # 素材所属会话，写入事实表便于排查


@dataclass
class ControlRow:
    """控制面：一用户一行"""

    user_id: str
    revision: int
    create_time: datetime  # 兼抽取下界


@dataclass
class Decision:
    """仲裁输出的单条决策"""

    action: str
    content: str = ""
    target_ids: List[int] = field(default_factory=list)
    reason: str = ""


@dataclass
class JudgeResult:
    """仲裁整体结果"""

    decisions: List[Decision] = field(default_factory=list)
    raw: str = ""
    parsed: bool = False
    error: str = ""
