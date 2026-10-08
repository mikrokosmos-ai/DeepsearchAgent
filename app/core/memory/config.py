"""
记忆阈值单点配置与派生

派生表（比例是契约，写死在下方常量里，不随环境变量变化）：

    | 派生量              | 比例                  | 备注                          |
    | 裁剪门              | 50%                   | 低于它不动手（无损手段优先）    |
    | 压缩门              | 80%                   | 到它才做有损摘要                |
    | 压缩保留段          | 20%                   | 切点之后至少留这么多原文         |
    | 摘要正文上限        | 10%，夹 [1500, 6000]   |                               |
    | 长期记忆块上限      | 0.5%，夹 [1500, 6000]  | 全量注入，故同时是记忆总量硬上界 |
    | 会话历史读取预算    | 20%                   | 按预算取最近若干轮，替代固定 10 条 |
    | 合并停手水位        | 75% × 块上限           | 兼任淘汰硬下限                  |
    | 后台抽取门槛        | 3 条                  | flush 工具不受此门槛挡          |
    | 保留工具循环        | 2 个                  | 保护窗口                        |
    | 摘要当前代缓存 TTL   | 7 天（固定）           | 过期会触发一次重算 |
    | 单条事实上限        | 1/3 × 块上限           | 保证块里至少能装 3 条 |
    | 单批抽取素材上限    | 40 条                  | 与参考项目一致 |
    | 僵尸批次判定        | 10 分钟                | 回收会推水位，不能设太短 |

"""

import os
from dataclasses import dataclass
from typing import Optional

from app.core.paths import PROJECT_ROOT  # noqa: F401  导入即完成 .env 加载

# 默认「会话上下文工程预算」（字符）。取 120000 是给「人设 + 工具 schema + 输出预留」
# 留出余量后的保守值，而不是任何模型窗口的字符换算值（见模块头警告）。
DEFAULT_CONTEXT_BUDGET_CHARS = 120000

# ---- 派生比例（契约，不随环境变量变化） ----
TRIM_GATE_RATIO = 0.50
COMPACT_GATE_RATIO = 0.80
COMPACT_KEEP_RATIO = 0.20
SUMMARY_MAX_RATIO = 0.10
LONG_TERM_MAX_RATIO = 0.005
CONSOLIDATION_FLOOR_RATIO = 0.75
# 会话历史读取预算占比：固定 10 条会随消息变长而超窗，故改成按字符预算取最近若干轮
HISTORY_BUDGET_RATIO = 0.20

# ---- 摘要 / 长期记忆块的 clamp 区间 ----
SUMMARY_MIN_CHARS = 1500
SUMMARY_MAX_LIMIT_CHARS = 6000
LONG_TERM_MIN_CHARS = 1500
LONG_TERM_MAX_LIMIT_CHARS = 6000

# ---- 后台抽取门槛与工具循环保护数 ----
DEFAULT_BACKGROUND_EXTRACT_MIN_MESSAGES = 3
DEFAULT_PROTECT_TOOL_LOOPS = 2

# ---- checkpointer 相关默认值 ----
# 命名规范：dsa:<域>:<对象>，各域互不重叠，便于运维按前缀清库
DEFAULT_CKPT_KEY_PREFIX = "dsa:ckpt:"
# 默认 7 天：会话记忆是热层，长期留存由权威库（L1）负责，热层允许自然过期
DEFAULT_CKPT_TTL_S = 7 * 24 * 3600

# ---- 会话消息热窗口 ----
# key 前缀与 checkpointer 分域，避免两类键混在同一命名空间
DEFAULT_CONV_KEY_PREFIX = "dsa:conv:"
# 默认 1 小时：热窗口只服务「同一会话连续几轮」，比 L0 的 TTL 短得多；
# 过期只影响一次回源，不影响正确性（Mongo 是权威）
DEFAULT_CONV_TTL_S = 3600

# ---- 摘要当前代缓存 ----
# 与 checkpointer / 热窗口分域；默认 7 天—— 摘要是被压缩掉原文的替代物，
# 过期不只是多一次回源，而是触发一次「重新调模型压缩」，故留得比热窗口长得多
DEFAULT_SUMMARY_KEY_PREFIX = "dsa:summary:"
DEFAULT_SUMMARY_TTL_S = 7 * 24 * 3600

# ---- 工具结果裁剪----
# 白名单：只裁这些工具的历史结果。两者返回体量最大，且旧轮次的结论已被后续轮次吸收；
# 不裁 generate_markdown / convert_md_to_pdf / flush_memory —— 它们本来的返回就很短。
DEFAULT_TRIM_TOOL_WHITELIST = ("task", "read_file_content")

# ---- 长期记忆----
# 注入块缓存与摘要/热窗口分域
DEFAULT_LTM_KEY_PREFIX = "dsa:ltm:"
DEFAULT_LTM_TTL_S = 7 * 24 * 3600
# 单条事实上限 = 1/3 × 块上限：块上限 1500 时单条 500，避免一条独占整个块
LONG_TERM_ITEM_MAX_RATIO = 1 / 3
# 单批素材上限与僵尸判定（固定量，不随预算变化）
DEFAULT_EXTRACT_BATCH_MAX = 40
DEFAULT_EXTRACT_ZOMBIE_TIMEOUT_S = 600

# 派生量字段名清单：验证脚本按它打印/比对，避免"改了派生表但脚本没跟上"
DERIVED_FIELD_NAMES = (
    "context_budget_chars",
    "trim_gate_chars",
    "compact_gate_chars",
    "compact_keep_chars",
    "summary_max_chars",
    "long_term_max_chars",
    "history_budget_chars",
    "consolidation_floor_chars",
    "background_extract_min_messages",
    "protect_tool_loops",
    "ckpt_key_prefix",
    "ckpt_ttl_s",
    "conv_key_prefix",
    "conv_ttl_s",
    "conv_cache_enabled",
    "compact_enabled",
    "summary_key_prefix",
    "summary_ttl_s",
    "long_term_enabled",
    "extract_enabled",
    "long_term_item_max_chars",
    "extract_batch_max",
    "extract_zombie_timeout_s",
    "ltm_key_prefix",
    "ltm_ttl_s",
    "ltm_cache_enabled",
    "trim_enabled",
    "trim_tool_whitelist",
    "consolidation_enabled",
)


def _env_int(name: str, default: int) -> int:
    """读取整型环境变量；缺失 / 空白 / 非法一律回退默认（不抛）"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: tuple) -> tuple:
    """读取逗号分隔的列表型环境变量；缺失 / 空白一律回退默认（不抛）"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    items = tuple(item.strip() for item in str(raw).split(",") if item.strip())
    return items or default


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量：仅 "true"/"1"/"yes"/"on" 视为真，其余回退默认语义"""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "on")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool  # 记忆层总开关；关闭时 checkpointer 直接用内存实现
    context_budget_chars: int  # 唯一业务数字：会话上下文工程预算
    trim_gate_chars: int  # 工具结果裁剪门
    compact_gate_chars: int  # 摘要压缩门
    compact_keep_chars: int  # 压缩保留段下限
    summary_max_chars: int  # 摘要正文上限
    long_term_max_chars: int  # 长期记忆块/总量上限
    history_budget_chars: int  # 会话历史读取预算（替代固定 10 条）
    consolidation_floor_chars: int  # 合并停手水位兼淘汰硬下限
    background_extract_min_messages: int  # 后台抽取门槛
    protect_tool_loops: int  # 工具循环保留数
    ckpt_key_prefix: str  # checkpointer key 前缀
    ckpt_ttl_s: int  # checkpointer key TTL，秒；<=0 表示不过期
    ckpt_refresh_on_read: bool  # 读时续期：让活跃会话的热数据不因读多写少而过期
    conv_key_prefix: str  # 会话消息热窗口 key 前缀
    conv_ttl_s: int  # 热窗口 key TTL，秒；<=0 表示不过期
    conv_cache_enabled: bool  # 热窗口总开关；关闭时全部回源 Mongo
    compact_enabled: bool  # 摘要压缩总开关；关闭时回退到底座默认摘要
    summary_key_prefix: str  # 摘要当前代缓存 key 前缀
    summary_ttl_s: int  # 摘要当前代缓存 TTL，秒；<=0 表示不过期
    long_term_enabled: bool  # 长期记忆总开关（注入 + 抽取 + 整理工具）
    extract_enabled: bool  # 抽取开关：允许「只注入不抽取」
    long_term_item_max_chars: int  # 单条事实上限（超长拒收，不截断）
    extract_batch_max: int  # 单批素材条数上限
    extract_zombie_timeout_s: int  # PROCESSING 批次判僵尸的秒数
    ltm_key_prefix: str  # 注入块缓存 key 前缀
    ltm_ttl_s: int  # 注入块缓存 TTL，秒；<=0 表示不过期
    ltm_cache_enabled: bool  # 注入块缓存开关
    trim_enabled: bool  # 工具结果裁剪总开关
    trim_tool_whitelist: tuple  # 可裁剪的工具名（白名单外的历史结果一律不动）
    consolidation_enabled: bool  # 容量治理总开关（受限合并 + 淘汰）


def _build() -> MemoryConfig:
    """按当前环境变量重建一份配置（供 import 期单例与验证脚本差分复用）"""
    budget = _env_int("MEMORY_CONTEXT_BUDGET_CHARS", DEFAULT_CONTEXT_BUDGET_CHARS)
    # 非正预算会让所有派生量坍缩为 0，等于关掉记忆链路 —— 视为非法并回退默认
    if budget <= 0:
        budget = DEFAULT_CONTEXT_BUDGET_CHARS

    long_term_max = _clamp(
        int(budget * LONG_TERM_MAX_RATIO), LONG_TERM_MIN_CHARS, LONG_TERM_MAX_LIMIT_CHARS
    )
    prefix = (os.getenv("MEMORY_CKPT_PREFIX") or "").strip() or DEFAULT_CKPT_KEY_PREFIX

    return MemoryConfig(
        enabled=_env_bool("MEMORY_ENABLE", True),
        context_budget_chars=budget,
        trim_gate_chars=int(budget * TRIM_GATE_RATIO),
        compact_gate_chars=int(budget * COMPACT_GATE_RATIO),
        compact_keep_chars=int(budget * COMPACT_KEEP_RATIO),
        summary_max_chars=_clamp(
            int(budget * SUMMARY_MAX_RATIO), SUMMARY_MIN_CHARS, SUMMARY_MAX_LIMIT_CHARS
        ),
        long_term_max_chars=long_term_max,
        history_budget_chars=int(budget * HISTORY_BUDGET_RATIO),
        consolidation_floor_chars=int(long_term_max * CONSOLIDATION_FLOOR_RATIO),
        background_extract_min_messages=DEFAULT_BACKGROUND_EXTRACT_MIN_MESSAGES,
        protect_tool_loops=DEFAULT_PROTECT_TOOL_LOOPS,
        ckpt_key_prefix=prefix,
        ckpt_ttl_s=_env_int("MEMORY_CKPT_TTL_S", DEFAULT_CKPT_TTL_S),
        ckpt_refresh_on_read=_env_bool("MEMORY_CKPT_REFRESH_ON_READ", True),
        conv_key_prefix=(os.getenv("MEMORY_CONV_PREFIX") or "").strip() or DEFAULT_CONV_KEY_PREFIX,
        conv_ttl_s=_env_int("MEMORY_CONV_TTL_S", DEFAULT_CONV_TTL_S),
        conv_cache_enabled=_env_bool("MEMORY_CONV_CACHE_ENABLE", True),
        compact_enabled=_env_bool("MEMORY_COMPACT_ENABLE", True),
        summary_key_prefix=(os.getenv("MEMORY_SUMMARY_PREFIX") or "").strip()
        or DEFAULT_SUMMARY_KEY_PREFIX,
        summary_ttl_s=_env_int("MEMORY_SUMMARY_TTL_S", DEFAULT_SUMMARY_TTL_S),
        long_term_enabled=_env_bool("MEMORY_LONG_TERM_ENABLE", True),
        extract_enabled=_env_bool("MEMORY_EXTRACT_ENABLE", True),
        long_term_item_max_chars=int(long_term_max * LONG_TERM_ITEM_MAX_RATIO),
        extract_batch_max=DEFAULT_EXTRACT_BATCH_MAX,
        extract_zombie_timeout_s=DEFAULT_EXTRACT_ZOMBIE_TIMEOUT_S,
        ltm_key_prefix=(os.getenv("MEMORY_LTM_PREFIX") or "").strip()
        or DEFAULT_LTM_KEY_PREFIX,
        ltm_ttl_s=_env_int("MEMORY_LTM_TTL_S", DEFAULT_LTM_TTL_S),
        ltm_cache_enabled=_env_bool("MEMORY_LTM_CACHE_ENABLE", True),
        trim_enabled=_env_bool("MEMORY_TRIM_ENABLE", True),
        trim_tool_whitelist=_env_list("MEMORY_TRIM_TOOLS", DEFAULT_TRIM_TOOL_WHITELIST),
        consolidation_enabled=_env_bool("MEMORY_CONSOLIDATION_ENABLE", True),
    )


# 模块级单例：进程内所有消费者读同一份派生结果，避免各自解析环境变量出现口径分裂
memory_config = _build()


def build_memory_config() -> MemoryConfig:
    """按当前环境重建配置（验证脚本用于证明"改一个数字，派生量随之变化"）"""
    return _build()


def describe(config: Optional[MemoryConfig] = None) -> str:
    """把派生阈值拍成一行稳定文本，供日志与验证脚本比对（字段顺序固定）"""
    cfg = config or memory_config
    return "、".join(f"{name}={getattr(cfg, name)}" for name in DERIVED_FIELD_NAMES)
